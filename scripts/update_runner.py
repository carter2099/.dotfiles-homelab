#!/usr/bin/env python3
"""
Homelab update agent — deterministic Python orchestrator.
Zero LLM in the loop. Scheduled via update-check.timer.

Replaces: ~/scripts/run_update_check.sh
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

HOME = Path.home()
RUN_DIR_BASE = HOME / "digests" / "updates"
ARCHIVE_DIR = RUN_DIR_BASE
TEMPLATE_PATH = RUN_DIR_BASE / "template.html"
RUNS_LOG = RUN_DIR_BASE / ".runs.log"
K3S = "/usr/local/bin/k3s"
GH_API = "https://api.github.com/repos/open-webui/open-webui/releases/latest"
OPENWEBUI_COMPOSE = HOME / "open-webui" / "docker-compose.yml"
DIGEST_SCRIPT = HOME / "scripts" / "send_digest.py"

# Packages we auto-apply (docker + cloudflared)
AUTO_PKGS = [
    "docker-ce",
    "docker-ce-cli",
    "containerd.io",
    "docker-buildx-plugin",
    "docker-compose-plugin",
    "cloudflared",
]

# Endpoints to validate
ENDPOINTS = {
    "open-webui": "http://127.0.0.1:48100",
    "blog": "http://127.0.0.1:33099",
    "delta_neutral": "http://127.0.0.1:43080",
    "pi-web": "http://127.0.0.1:8504",
    "llm-proxy": "http://127.0.0.1:8081/health",
}


# ── helpers ──────────────────────────────────────────────────────────


def run(cmd, **kwargs):
    """Run a command, return CompletedProcess. Raises on non-zero exit."""
    kwargs.setdefault("check", True)
    kwargs.setdefault("text", True)
    kwargs.setdefault("timeout", 120)
    return subprocess.run(cmd, **kwargs)


def run_ok(cmd, **kwargs):
    """Run a command, return True if exit 0, False otherwise."""
    kwargs.setdefault("text", True)
    kwargs.setdefault("timeout", 120)
    try:
        subprocess.run(cmd, check=True, **kwargs)
        return True
    except subprocess.CalledProcessError:
        return False


def run_capture(cmd, **kwargs):
    """Run a command, capture stdout, return stripped string (or '' on failure)."""
    kwargs.setdefault("text", True)
    kwargs.setdefault("timeout", 120)
    try:
        cp = subprocess.run(cmd, capture_output=True, check=True, **kwargs)
        return cp.stdout.strip()
    except subprocess.CalledProcessError:
        return ""


def run_capture_ok(cmd, **kwargs):
    """Run a command, return (stdout, stderr, exit_code). Never raises."""
    kwargs.setdefault("text", True)
    kwargs.setdefault("timeout", 120)
    try:
        cp = subprocess.run(cmd, capture_output=True, **kwargs)
        return cp.stdout.strip(), cp.stderr.strip(), cp.returncode
    except Exception as e:
        return "", str(e), -1


def apt_installed_version(pkg):
    """Parse 'apt-cache policy <pkg>' to get the Installed version."""
    out = run_capture(["apt-cache", "policy", pkg])
    m = re.search(r"Installed:\s+(.+)$", out, re.MULTILINE)
    if m:
        return m.group(1).strip()
    return None


def apt_upgradable():
    """Return dict of {package: current_version -> new_version} from apt list --upgradable."""
    out = run_capture(["apt", "list", "--upgradable"], env={**os.environ, "LANG": "C"})
    result = {}
    for line in out.splitlines():
        m = re.match(
            r"^(\S+)/\S+\s+(\S+)\s+\S+\s+\[upgradable from:\s+(.+)\]", line
        )
        if m:
            result[m.group(1)] = f"{m.group(3)} → {m.group(2)}"
    return result


def docker_image_age_days(image_pattern):
    """Return days since a docker image matching pattern was created, or None."""
    out = run_capture(
        [
            "docker",
            "images",
            "--format",
            "{{.Repository}}:{{.Tag}}\t{{.CreatedAt}}",
        ]
    )
    for line in out.splitlines():
        if image_pattern in line:
            parts = line.split("\t")
            if len(parts) >= 2:
                ts_str = parts[1]
                try:
                    # Docker --format CreatedAt gives e.g. "2026-06-20 12:34:56 -0400 EDT"
                    # Parse the date part
                    created = datetime.strptime(
                        ts_str.split(" ")[0], "%Y-%m-%d"
                    )
                    return (datetime.now() - created).days
                except (ValueError, IndexError):
                    return None
    return None


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, default=str) + "\n")


def read_json(path):
    return json.loads(path.read_text())


def prev_workday(today):
    """Return yesterday's date (simple — just -1 day)."""
    return today - timedelta(days=1)


def parse_previous_summary(md_path):
    """Parse previous day's .md summary into lines by section."""
    if not md_path.exists():
        return {}
    text = md_path.read_text()
    sections = {}
    current = None
    for line in text.splitlines():
        m = re.match(r"^##\s+(.+)$", line)
        if m:
            current = m.group(1).strip()
            sections[current] = []
        elif current:
            sections[current].append(line)
    return sections


# ── phase 0: setup ───────────────────────────────────────────────────


def phase_setup(args):
    """Create run dir, load previous summary, determine delta."""
    today = datetime.now().strftime("%Y-%m-%d")
    run_dir = RUN_DIR_BASE / today
    run_dir.mkdir(parents=True, exist_ok=True)

    prev_date = prev_workday(datetime.now()).strftime("%Y-%m-%d")
    prev_md = RUN_DIR_BASE / f"{prev_date}.md"
    prev_summary = parse_previous_summary(prev_md)

    data = {
        "date": today,
        "run_dir": str(run_dir),
        "prev_date": prev_date,
        "prev_summary_exists": prev_md.exists(),
        "dry_run": args.dry_run,
        "resume": args.resume,
    }
    artifact = run_dir / "00-setup.json"
    write_json(artifact, data)
    print(f"[phase 0] setup → {artifact}")
    return data


# ── phase 1: apply ───────────────────────────────────────────────────


def phase_apply_apt_upgrade():
    """Run apt update + apt upgrade -y. Returns structured result."""
    print("  [1a] apt update + upgrade")
    try:
        update = run(["sudo", "apt", "update"], capture_output=True, text=True)
        upgrade = run(
            ["sudo", "apt", "upgrade", "-y"], capture_output=True, text=True
        )
        # Parse upgrade output for count
        stdout = upgrade.stdout
        upgraded = 0
        m = re.search(r"(\d+)\s+upgraded", stdout)
        if m:
            upgraded = int(m.group(1))
        return {
            "step": "apt_upgrade",
            "status": "ok",
            "upgraded_count": upgraded,
            "output_tail": "\n".join(stdout.strip().splitlines()[-20:]),
        }
    except subprocess.CalledProcessError as e:
        return {
            "step": "apt_upgrade",
            "status": "failed",
            "error": str(e),
            "output": e.stdout if e.stdout else "",
        }


def phase_apply_auto_pkgs():
    """Auto-apply docker-* and cloudflared upgrades with pre-version capture."""
    results = []
    for pkg in AUTO_PKGS:
        print(f"  [1b] auto-apply {pkg}")
        pre_ver = apt_installed_version(pkg)
        try:
            cp = run(
                ["sudo", "apt", "install", "--only-upgrade", pkg, "-y"],
                capture_output=True,
                text=True,
            )
            post_ver = apt_installed_version(pkg)
            results.append(
                {
                    "step": f"auto_{pkg}",
                    "status": "ok" if post_ver != pre_ver else "skipped",
                    "pre_version": pre_ver,
                    "post_version": post_ver,
                }
            )
        except subprocess.CalledProcessError as e:
            results.append(
                {
                    "step": f"auto_{pkg}",
                    "status": "failed",
                    "pre_version": pre_ver,
                    "error": str(e),
                    "output": e.stdout.strip() if e.stdout else "",
                }
            )
    return results


def phase_apply_docker_pause():
    """Pause 10s for Docker daemon restart after docker-* upgrades."""
    print("  [1c] docker daemon restart pause (10s)")
    time.sleep(10)
    return {"step": "docker_pause", "status": "ok"}


def phase_assert_docker_daemon():
    """After a docker-* upgrade, assert the sole daemon is the apt one
    (data root /var/lib/docker). Guards against a second daemon (e.g. a snap)
    creeping back in and stealing /var/run/docker.sock — the exact incident
    that motivated the apt-only standardization (2026-07-10)."""
    print("  [1c2] assert docker daemon root == /var/lib/docker")
    try:
        root = run(
            ["docker", "info", "--format", "{{.DockerRootDir}}"],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
    except subprocess.CalledProcessError as e:
        return {"step": "docker_daemon_assert", "status": "failed",
                "error": f"docker info failed: {e}"}
    except subprocess.TimeoutExpired:
        return {"step": "docker_daemon_assert", "status": "failed",
                "error": "docker info timed out"}
    if root != "/var/lib/docker":
        return {"step": "docker_daemon_assert", "status": "failed",
                "error": f"unexpected DockerRootDir: {root!r} "
                          f"(expected '/var/lib/docker' — a second daemon may be installed)"}
    return {"step": "docker_daemon_assert", "status": "ok", "root": root}


def phase_apply_cloudflared_restart():
    """Restart cloudflared after upgrade."""
    print("  [1d] restart cloudflared")
    try:
        run(["sudo", "systemctl", "restart", "cloudflared"], capture_output=True, text=True)
        time.sleep(5)
        return {"step": "cloudflared_restart", "status": "ok"}
    except subprocess.CalledProcessError as e:
        return {"step": "cloudflared_restart", "status": "failed", "error": str(e)}


def phase_apply_k3s_rollouts():
    """Rollout restart freshrss + uptime-kuma, wait for status."""
    results = []
    env = {**os.environ, "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"}
    for name, ns, timeout_s in [
        ("freshrss", "freshrss", 120),
        ("uptime-kuma", "default", 120),
    ]:
        print(f"  [1e] k3s rollout restart {name}/{ns}")
        try:
            run(
                [K3S, "kubectl", "rollout", "restart", f"deploy/{name}", "-n", ns],
                env=env,
                capture_output=True,
                text=True,
            )
            run(
                [
                    K3S,
                    "kubectl",
                    "rollout",
                    "status",
                    f"deploy/{name}",
                    "-n",
                    ns,
                    f"--timeout={timeout_s}s",
                ],
                env=env,
                capture_output=True,
                text=True,
            )
            results.append({"step": f"k3s_{name}", "status": "ok"})
        except subprocess.CalledProcessError as e:
            results.append(
                {
                    "step": f"k3s_{name}",
                    "status": "failed",
                    "error": str(e),
                }
            )
    return results


def phase_apply_openwebui():
    """Check open-webui GitHub releases for a newer stable tag, bump if found."""
    print("  [1f] open-webui stable-tag check")
    import urllib.request
    import urllib.error

    if not OPENWEBUI_COMPOSE.exists():
        return {
            "step": "openwebui",
            "status": "skipped",
            "reason": f"compose file not found: {OPENWEBUI_COMPOSE}",
        }

    compose_text = OPENWEBUI_COMPOSE.read_text()
    current_m = re.search(r"ghcr\.io/open-webui/open-webui:([^\s\"']+)", compose_text)
    current_tag = current_m.group(1) if current_m else None
    if not current_tag:
        return {
            "step": "openwebui",
            "status": "skipped",
            "reason": "could not parse current tag from compose file",
        }

    # Fetch latest release tag from GitHub
    latest_tag = None
    try:
        req = urllib.request.Request(
            GH_API, headers={"Accept": "application/vnd.github+json"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            release = json.loads(resp.read().decode())
            latest_tag = release.get("tag_name", "").lstrip("v")
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
        return {
            "step": "openwebui",
            "status": "error",
            "reason": f"GitHub API unreachable: {e}",
            "current_tag": current_tag,
        }

    if not latest_tag:
        return {
            "step": "openwebui",
            "status": "error",
            "reason": "no tag_name in GitHub release",
            "current_tag": current_tag,
        }

    # Compare — strip leading 'v' from both for safety
    cur_clean = current_tag.lstrip("v")
    lat_clean = latest_tag.lstrip("v")

    if cur_clean == lat_clean:
        return {
            "step": "openwebui",
            "status": "current",
            "current_tag": current_tag,
            "latest_tag": latest_tag,
        }

    # Bump the tag
    print(f"    bumping open-webui: {current_tag} → {latest_tag}")
    new_compose = compose_text.replace(
        f"ghcr.io/open-webui/open-webui:{current_tag}",
        f"ghcr.io/open-webui/open-webui:{latest_tag}",
    )
    OPENWEBUI_COMPOSE.write_text(new_compose)

    try:
        run(
            ["docker", "compose", "-f", str(OPENWEBUI_COMPOSE), "pull"],
            cwd=OPENWEBUI_COMPOSE.parent,
            capture_output=True,
            text=True,
        )
        run(
            ["docker", "compose", "-f", str(OPENWEBUI_COMPOSE), "up", "-d"],
            cwd=OPENWEBUI_COMPOSE.parent,
            capture_output=True,
            text=True,
        )
        # Wait for healthy
        healthy = False
        for _ in range(30):
            time.sleep(1)
            status = run_capture(
                ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"]
            )
            for line in status.splitlines():
                if "open-webui" in line and "healthy" in line.lower():
                    healthy = True
                    break
            if healthy:
                break
        return {
            "step": "openwebui",
            "status": "bumped",
            "current_tag": current_tag,
            "latest_tag": latest_tag,
            "healthy": healthy,
        }
    except subprocess.CalledProcessError as e:
        return {
            "step": "openwebui",
            "status": "failed",
            "current_tag": current_tag,
            "latest_tag": latest_tag,
            "error": str(e),
        }


def phase_apply(run_dir, dry_run=False):
    """Phase 1: apply safe updates. Skip if --dry-run."""
    if dry_run:
        print("[phase 1] DRY RUN — skipping all mutations")
        data = {"dry_run": True, "steps": []}
        artifact = run_dir / "01-applied.json"
        write_json(artifact, data)
        return data

    print("[phase 1] applying safe updates")
    steps = []

    # 1a: apt upgrade
    result = phase_apply_apt_upgrade()
    steps.append(result)
    if result["status"] == "failed":
        print(f"  FAILED: apt upgrade — {result.get('error')}")
        data = {"steps": steps}
        artifact = run_dir / "01-applied.json"
        write_json(artifact, data)
        return data

    # 1b: auto-apply docker + cloudflared
    auto_results = phase_apply_auto_pkgs()
    steps.extend(auto_results)

    # Check for failures — stop on first failure per safety rules
    for r in auto_results:
        if r["status"] == "failed":
            print(f"  FAILED: {r['step']} — {r.get('error')}")
            data = {"steps": steps}
            artifact = run_dir / "01-applied.json"
            write_json(artifact, data)
            return data

    # 1c: docker daemon pause (only if docker packages were actually upgraded)
    docker_upgraded = any(
        s["step"].startswith("auto_docker") and s["status"] == "ok"
        for s in auto_results
    )
    if docker_upgraded:
        steps.append(phase_apply_docker_pause())
        steps.append(phase_assert_docker_daemon())

    # 1d: cloudflared restart (only if actually upgraded)
    cloudflared_upgraded = any(
        s["step"] == "auto_cloudflared" and s["status"] == "ok"
        for s in auto_results
    )
    if cloudflared_upgraded:
        steps.append(phase_apply_cloudflared_restart())

    # 1e: k3s rollouts
    k3s_results = phase_apply_k3s_rollouts()
    steps.extend(k3s_results)

    # 1f: open-webui
    owu_result = phase_apply_openwebui()
    steps.append(owu_result)

    data = {"steps": steps}
    artifact = run_dir / "01-applied.json"
    write_json(artifact, data)
    print(f"[phase 1] done → {artifact}")
    print(f"  applied {sum(1 for s in steps if s['status'] == 'ok')} steps, "
          f"{sum(1 for s in steps if s['status'] == 'bumped')} bumped, "
          f"{sum(1 for s in steps if s['status'] == 'skipped')} skipped, "
          f"{sum(1 for s in steps if s['status'] == 'failed')} failed")
    return data


# ── phase 2: validate ────────────────────────────────────────────────


def phase_validate(run_dir):
    """Phase 2: run all validation checks."""
    print("[phase 2] validating services")
    checks = []

    # Docker containers
    out = run_capture(
        ["docker", "ps", "-a", "--format", "{{.Names}} {{.Status}}"]
    )
    checks.append({"name": "docker_containers", "output": out, "status": "ok"})

    # k3s pods
    env = {**os.environ, "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"}
    bad_pods = run_capture(
        [K3S, "kubectl", "get", "pods", "-A", "--no-headers"],
        env=env,
    )
    bad_lines = [
        l for l in bad_pods.splitlines()
        if not re.search(r"\b(Running|Completed)\b", l)
    ]
    checks.append(
        {
            "name": "k3s_pods",
            "status": "ok" if not bad_lines else "warning",
            "bad_pods": bad_lines,
            "output": bad_pods if bad_lines else "",
        }
    )

    # Endpoint curls
    for name, url in ENDPOINTS.items():
        code = run_capture(
            ["curl", "-so", "/dev/null", "-w", "%{http_code}", url],
        )
        healthy = code.startswith("2") or code.startswith("3")
        checks.append(
            {
                "name": f"endpoint_{name}",
                "url": url,
                "http_code": code,
                "status": "ok" if healthy else "fail",
            }
        )


    # LLM proxy X-Fallback header
    fallback = run_capture(
        [
            "curl",
            "-sI",
            "http://127.0.0.1:8081/health",
        ]
    )
    fallback_active = "X-Fallback: true" in fallback
    checks.append(
        {
            "name": "llm_fallback",
            "status": "warning" if fallback_active else "ok",
            "fallback_active": fallback_active,
        }
    )

    data = {"checks": checks}
    artifact = run_dir / "02-validation.json"
    write_json(artifact, data)
    print(f"[phase 2] done → {artifact}")
    return data


# ── phase 3: audit ───────────────────────────────────────────────────


def phase_audit(run_dir):
    """Phase 3: full audit — what still needs attention?"""
    print("[phase 3] auditing system")
    needs_attention = []
    behind_safe = []

    # apt upgradable
    upgradable = apt_upgradable()
    for pkg, ver in upgradable.items():
        # These should now be empty since we auto-apply them
        if any(pkg.startswith(p) for p in AUTO_PKGS):
            needs_attention.append(
                {
                    "item": f"{pkg} ({ver})",
                    "reason": "Failed to auto-apply — investigate",
                    "category": "needs_attention",
                }
            )
        else:
            needs_attention.append(
                {
                    "item": f"{pkg} ({ver})",
                    "reason": "New upgradable package — not in auto-apply scope",
                    "category": "needs_attention",
                }
            )

    # snap refresh list
    snap_out = run_capture(["snap", "refresh", "--list"])
    if snap_out and "All snaps up to date" not in snap_out:
        for line in snap_out.splitlines():
            if line.strip():
                behind_safe.append(
                    {
                        "item": line.strip(),
                        "reason": "Snap refresh available — snapd handles this automatically",
                        "category": "behind_safe",
                    }
                )

    # Docker image ages
    for pattern, label, deploy_cmd in [
        ("blog-web", "blog-web:latest", "release.sh"),
        ("delta_neutral-web", "delta_neutral-web:latest", "release.sh"),
    ]:
        age = docker_image_age_days(pattern)
        if age is not None:
            if age > 14:
                needs_attention.append(
                    {
                        "item": f"{label} — built {age} days ago",
                        "reason": f"Stale custom image (>14 days) — deploy via {deploy_cmd}",
                        "category": "needs_attention",
                    }
                )
            else:
                behind_safe.append(
                    {
                        "item": f"{label} — built {age} days ago",
                        "reason": f"Custom image age ({age}d) within window",
                        "category": "behind_safe",
                    }
                )

    # k3s infrastructure images
    env = {**os.environ, "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"}
    k3s_wide = run_capture(
        [K3S, "kubectl", "get", "deploy,ds", "-A", "-o", "wide"],
        env=env,
    )
    for line in k3s_wide.splitlines():
        if re.search(r"traefik|coredns|metrics", line):
            behind_safe.append(
                {
                    "item": line.strip(),
                    "reason": "k3s-managed infrastructure",
                    "category": "behind_safe",
                }
            )

    # Runtimes
    for cmd, label in [
        (["go", "version"], "Go"),
        (["rbenv", "versions"], "Ruby (rbenv)"),
        (["node", "--version"], "Node.js"),
        ([K3S, "--version"], "k3s"),
        (["nvim", "--version"], "Neovim"),
    ]:
        out = run_capture_ok(cmd)[0]
        if out:
            behind_safe.append(
                {
                    "item": f"{label}: {out.splitlines()[0].strip() if out else 'unknown'}",
                    "reason": "Runtime — manual upgrade only",
                    "category": "behind_safe",
                }
            )

    # dependabot-webhook
    dw_out = run_capture(
        ["git", "fetch", "origin"],
        cwd=HOME / "dev" / "dependabot-webhook",
    )
    dw_status = run_capture(
        ["git", "status", "-sb"],
        cwd=HOME / "dev" / "dependabot-webhook",
    )
    if "behind" in dw_status.lower():
        behind_safe.append(
            {
                "item": f"dependabot-webhook: {dw_status}",
                "reason": "Behind origin — manual deploy needed",
                "category": "behind_safe",
            }
        )
    else:
        behind_safe.append(
            {
                "item": "dependabot-webhook: up to date with origin/main",
                "reason": "Current",
                "category": "behind_safe",
            }
        )

    # npm outdated
    npm_out = run_capture_ok(["npm", "outdated", "-g"])[0]
    for line in npm_out.splitlines():
        if line.strip() and "Package" not in line:
            behind_safe.append(
                {
                    "item": f"npm global: {line.strip()}",
                    "reason": "npm global — manual only (fragile peer-dep trees)",
                    "category": "behind_safe",
                }
            )

    # Reboot required
    if (Path("/var/run/reboot-required")).exists():
        kernel_out = run_capture(["uname", "-r"])
        needs_attention.append(
            {
                "item": f"Reboot required — running kernel {kernel_out}",
                "reason": "Kernel/microcode update pending — schedule reboot",
                "category": "needs_attention",
            }
        )

    data = {
        "needs_attention": needs_attention,
        "behind_safe": behind_safe,
    }

    if not needs_attention:
        needs_attention.append(
            {
                "item": "Nothing needs attention",
                "reason": "All systems current",
                "category": "needs_attention",
            }
        )

    artifact = run_dir / "03-audit.json"
    write_json(artifact, data)
    print(f"[phase 3] done → {artifact}")
    return data


# ── phase 4: open-webui stable-tag check ─────────────────────────────


def phase_openwebui_check(run_dir):
    """Phase 4: record open-webui tag status (no-op if already bumped in phase 1)."""
    print("[phase 4] open-webui tag check")
    applied = read_json(run_dir / "01-applied.json") if (run_dir / "01-applied.json").exists() else {}
    for step in applied.get("steps", []):
        if step.get("step") == "openwebui":
            data = step
            break
    else:
        # Phase 1 didn't run (dry-run), do a fresh check
        import urllib.request
        import urllib.error

        current_tag = "unknown"
        if OPENWEBUI_COMPOSE.exists():
            compose_text = OPENWEBUI_COMPOSE.read_text()
            m = re.search(r"ghcr\.io/open-webui/open-webui:([^\s\"']+)", compose_text)
            if m:
                current_tag = m.group(1)

        latest_tag = None
        try:
            req = urllib.request.Request(
                GH_API, headers={"Accept": "application/vnd.github+json"}
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                release = json.loads(resp.read().decode())
                latest_tag = release.get("tag_name", "").lstrip("v")
        except Exception:
            pass

        data = {
            "step": "openwebui",
            "status": "dry_run",
            "current_tag": current_tag,
            "latest_tag": latest_tag,
        }

    artifact = run_dir / "04-openwebui.json"
    write_json(artifact, data)
    print(f"[phase 4] done → {artifact}")
    return data


# ── phase 5: heartbeat ───────────────────────────────────────────────


def phase_heartbeat(run_dir):
    """Phase 5: status heartbeat block."""
    print("[phase 5] heartbeat checks")

    env = {**os.environ, "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"}

    # Failed systemd units
    failed_user = run_capture(
        ["systemctl", "--user", "--failed", "--no-legend"], env=env
    )
    failed_system = run_capture(
        ["systemctl", "--failed", "--no-legend"]
    )

    # LLM stack health
    llm_health = run_capture(
        ["curl", "-s", "http://127.0.0.1:8081/health"]
    )
    fallback_headers = run_capture(
        ["curl", "-sI", "http://127.0.0.1:8081/health"]
    )
    falling_back = "X-Fallback: true" in fallback_headers

    # Backup recency
    backup_ts = run_capture(
        [
            "systemctl",
            "--user",
            "show",
            "homelab-backup",
            "-p",
            "ExecMainStartTimestamp",
        ],
        env=env,
    )
    backup_ts = backup_ts.replace("ExecMainStartTimestamp=", "").strip()

    # k3s node conditions
    nodes = run_capture(
        [K3S, "kubectl", "get", "nodes", "-o", "wide"], env=env
    )

    data = {
        "failed_units": {
            "user": failed_user.splitlines() if failed_user else [],
            "system": failed_system.splitlines() if failed_system else [],
        },
        "llm_stack": {
            "health": llm_health,
            "falling_back": falling_back,
        },
        "backup": {
            "last_run": backup_ts,
        },
        "k3s_nodes": nodes.splitlines() if nodes else [],
    }

    artifact = run_dir / "05-heartbeat.json"
    write_json(artifact, data)
    print(f"[phase 5] done → {artifact}")
    return data


# ── phase 6: write HTML ──────────────────────────────────────────────


def _render_status_icon(status):
    if status == "ok":
        return "✅"
    if status == "fail":
        return "🔴"
    if status == "warning":
        return "⚠️"
    return "ℹ️"


def _html_step(step):
    """Render a single Phase 1 step as an HTML line."""
    if step.get("dry_run"):
        return '<p style="margin:0 0 4px; color:#888; font-size:13px;">Dry run — no mutations applied.</p>'

    name = step.get("step", "")
    status = step.get("status", "")
    icon = _render_status_icon(status)

    if name == "apt_upgrade":
        n = step.get("upgraded_count", 0)
        return f'<p style="margin:0 0 4px; color:#555; font-size:13px;">{icon} <strong>apt upgrade:</strong> {n} packages upgraded</p>'

    if name.startswith("auto_"):
        pkg = name.replace("auto_", "")
        pre = step.get("pre_version", "?")
        post = step.get("post_version", "?")
        if status == "ok":
            return f'<p style="margin:0 0 4px; color:#555; font-size:13px;">{icon} <strong>{pkg}:</strong> {pre} → {post}</p>'
        elif status == "skipped":
            return f'<p style="margin:0 0 4px; color:#888; font-size:13px;">{icon} <strong>{pkg}:</strong> already current ({pre})</p>'
        else:
            err = step.get("error", "")
            return f'<p style="margin:0 0 4px; color:#c62828; font-size:13px;">{icon} <strong>{pkg}:</strong> FAILED — {err}</p>'

    if name.startswith("k3s_"):
        svc = name.replace("k3s_", "")
        if status == "ok":
            return f'<p style="margin:0 0 4px; color:#555; font-size:13px;">{icon} <strong>{svc}:</strong> rollout restart completed</p>'
        else:
            return f'<p style="margin:0 0 4px; color:#c62828; font-size:13px;">{icon} <strong>{svc}:</strong> rollout FAILED</p>'

    if name == "openwebui":
        if status == "bumped":
            return f'<p style="margin:0 0 4px; color:#555; font-size:13px;">{icon} <strong>open-webui:</strong> {step.get("current_tag")} → {step.get("latest_tag")}</p>'
        elif status == "current":
            return f'<p style="margin:0 0 4px; color:#555; font-size:13px;">{icon} <strong>open-webui:</strong> current at {step.get("current_tag")}</p>'
        else:
            return f'<p style="margin:0 0 4px; color:#888; font-size:13px;">{icon} <strong>open-webui:</strong> {step.get("status", "skipped")}</p>'

    if name == "docker_pause":
        return ""

    if name == "cloudflared_restart":
        if status == "ok":
            return f'<p style="margin:0 0 4px; color:#555; font-size:13px;">{icon} <strong>cloudflared:</strong> restarted</p>'
        return ""

    return f'<p style="margin:0 0 4px; color:#555; font-size:13px;">{icon} {name}: {status}</p>'


def _html_needs_attention(items):
    if not items:
        return '<p style="margin:0; color:#555; font-size:13px;">Nothing needs attention.</p>'
    lines = []
    for it in items:
        item = it.get("item", "")
        reason = it.get("reason", "")
        if "nothing needs attention" in item.lower():
            lines.append(
                f'<p style="margin:0 0 4px; color:#2e7d32; font-size:13px;">✅ {item}</p>'
            )
        else:
            lines.append(
                f'<p style="margin:0 0 4px; color:#555; font-size:13px;">'
                f'<strong>{item}</strong>'
                f'</p>'
                f'<p style="margin:0 0 10px; color:#888; font-size:12px;">{reason}</p>'
            )
    return "\n".join(lines)


def _html_behind_safe(items):
    if not items:
        return '<p style="margin:0; color:#555; font-size:13px;">Nothing behind.</p>'
    lines = []
    for it in items:
        item = it.get("item", "")
        reason = it.get("reason", "")
        lines.append(
            f'<p style="margin:0 0 2px; color:#555; font-size:13px;">'
            f'<strong>{item}</strong>'
            f'</p>'
            f'<p style="margin:0 0 8px; color:#888; font-size:12px;">{reason}</p>'
        )
    return "\n".join(lines)


def _html_validation(validation_data):
    checks = validation_data.get("checks", [])
    lines = []
    for c in checks:
        name = c.get("name", "")
        if name.startswith("endpoint_"):
            svc = name.replace("endpoint_", "")
            code = c.get("http_code", "?")
            status = c.get("status", "")
            icon = "✅" if status == "ok" else "🔴"
            color = "#2e7d32" if status == "ok" else "#c62828"
            lines.append(
                f'<p style="margin:0 0 4px; color:{color}; font-size:13px;">'
                f'{icon} {svc} ({c.get("url","")}) — <strong>{code}</strong>'
                f"</p>"
            )
        elif name == "docker_containers":
            lines.append(
                f'<p style="margin:0 0 4px; color:#555; font-size:13px;">'
                f"Docker containers: all reporting"
                f"</p>"
            )
        elif name == "k3s_pods":
            bad = c.get("bad_pods", [])
            if bad:
                lines.append(
                    f'<p style="margin:0 0 4px; color:#c62828; font-size:13px;">'
                    f"⚠️ k3s pods: {len(bad)} not Running/Completed"
                    f"</p>"
                )
            else:
                lines.append(
                    f'<p style="margin:0 0 4px; color:#2e7d32; font-size:13px;">'
                    f"✅ k3s pods: all healthy"
                    f"</p>"
                )
        elif name == "llm_fallback":
            fb = c.get("fallback_active", False)
            if fb:
                lines.append(
                    f'<p style="margin:0 0 4px; color:#f57f17; font-size:13px;">'
                    f"⚠️ LLM stack: cloud fallback active (gaming rig unavailable)"
                    f"</p>"
                )
            else:
                lines.append(
                    f'<p style="margin:0 0 4px; color:#2e7d32; font-size:13px;">'
                    f"✅ LLM stack: operating locally"
                    f"</p>"
                )
    return "\n".join(lines)


def _html_heartbeat(hb_data):
    lines = []

    # Failed units
    user_failed = hb_data.get("failed_units", {}).get("user", [])
    sys_failed = hb_data.get("failed_units", {}).get("system", [])
    total_failed = len([x for x in user_failed if x]) + len([x for x in sys_failed if x])
    if total_failed == 0:
        lines.append(
            f'<p style="margin:0 0 4px; color:#2e7d32; font-size:13px;">✅ All systemd units healthy (user + system)</p>'
        )
    else:
        for u in user_failed:
            if u.strip():
                lines.append(
                    f'<p style="margin:0 0 4px; color:#c62828; font-size:13px;">🔴 Failed user unit: {u.strip()}</p>'
                )
        for u in sys_failed:
            if u.strip():
                lines.append(
                    f'<p style="margin:0 0 4px; color:#c62828; font-size:13px;">🔴 Failed system unit: {u.strip()}</p>'
                )

    # LLM stack
    fb = hb_data.get("llm_stack", {}).get("falling_back", False)
    health = hb_data.get("llm_stack", {}).get("health", "")
    if fb:
        lines.append(
            f'<p style="margin:0 0 4px; color:#f57f17; font-size:13px;">⚠️ LLM stack: cloud fallback active — gaming rig may be down</p>'
        )
    else:
        lines.append(
            f'<p style="margin:0 0 4px; color:#2e7d32; font-size:13px;">✅ LLM stack: local ({health[:60] if health else "ok"})</p>'
        )

    # Backup recency
    backup_ts = hb_data.get("backup", {}).get("last_run", "")
    if backup_ts:
        lines.append(
            f'<p style="margin:0 0 4px; color:#555; font-size:13px;">📦 Last backup: {backup_ts}</p>'
        )
    else:
        lines.append(
            f'<p style="margin:0 0 4px; color:#c62828; font-size:13px;">🔴 Backup: status unknown — investigate</p>'
        )

    # k3s nodes
    nodes = hb_data.get("k3s_nodes", [])
    if nodes:
        node_text = nodes[0].strip() if nodes else "unknown"
        lines.append(
            f'<p style="margin:0 0 4px; color:#555; font-size:13px;">🖥️ k3s node: {node_text}</p>'
        )

    return "\n".join(lines)


def _html_system_health():
    """Gather system health stats."""
    rows = []

    # Disk
    df = run_capture(["df", "-h", "/"])
    if df:
        parts = df.splitlines()[-1].split()
        if len(parts) >= 5:
            rows.append(
                f'<tr><td style="padding:4px 0; color:#555; font-size:13px;">'
                f"<strong>Disk usage (/):</strong> {parts[4]} used ({parts[2]} / {parts[1]})"
                f"</td></tr>"
            )

    # Memory
    free = run_capture(["free", "-h"])
    if free:
        mem_line = [l for l in free.splitlines() if "Mem:" in l]
        if mem_line:
            parts = mem_line[0].split()
            if len(parts) >= 7:
                rows.append(
                    f'<tr><td style="padding:4px 0; color:#555; font-size:13px;">'
                    f"<strong>Memory:</strong> {parts[6]} available ({parts[2]} used / {parts[1]} total)"
                    f"</td></tr>"
                )

    # Uptime
    uptime = run_capture(["uptime"])
    if uptime:
        rows.append(
            f'<tr><td style="padding:4px 0; color:#555; font-size:13px;">'
            f"<strong>Uptime:</strong> {uptime.strip()}"
            f"</td></tr>"
        )

    # Reboot required
    reboot = (Path("/var/run/reboot-required")).exists()
    if reboot:
        kernel = run_capture(["uname", "-r"])
        rows.append(
            f'<tr><td style="padding:4px 0; color:#c62828; font-size:13px;">'
            f"<strong>Reboot required:</strong> ⚠️ YES — kernel update pending (current: {kernel})"
            f"</td></tr>"
        )
    else:
        rows.append(
            f'<tr><td style="padding:4px 0; color:#2e7d32; font-size:13px;">'
            f"<strong>Reboot required:</strong> No"
            f"</td></tr>"
        )

    # Docker container count
    ps_out = run_capture(["docker", "ps", "-a", "--format", "{{.Status}}"])
    if ps_out:
        total = len(ps_out.splitlines())
        running = len([l for l in ps_out.splitlines() if l.lower().startswith("up")])
        rows.append(
            f'<tr><td style="padding:4px 0; color:#555; font-size:13px;">'
            f"<strong>Docker containers:</strong> {running} running, {total - running} not running"
            f"</td></tr>"
        )

    return "\n".join(rows)


def _html_rollback(rollback_data):
    if not rollback_data or not rollback_data.get("triggered"):
        return ""
    status = rollback_data.get("status", "unknown")
    reverted = rollback_data.get("reverted", [])
    lines = []
    lines.append(
        '<tr><td style="padding:0 32px;"><hr style="border:none; border-top:1px solid #e8e8ee; margin:8px 0;"></td></tr>'
    )
    if status == "healthy":
        lines.append(
            '<tr><td style="padding:16px 32px 8px;">'
            '<h2 style="margin:0; color:#e65100; font-size:15px; font-weight:700;">🔄 Auto-Rollback Triggered</h2>'
            "</td></tr>"
        )
        lines.append(
            '<tr><td style="padding:8px 32px;">'
            f'<p style="margin:0 0 8px; color:#e65100; font-size:13px;">Auto-applied updates caused a validation failure (pi-web unhealthy). '
            f"Reverted to pre-update versions and re-validated — services are now healthy.</p>"
        )
    else:
        lines.append(
            '<tr><td style="padding:16px 32px 8px;">'
            '<h2 style="margin:0; color:#c62828; font-size:15px; font-weight:700;">🔴 ROLLBACK FAILED</h2>'
            "</td></tr>"
        )
        lines.append(
            '<tr><td style="padding:8px 32px;">'
            f'<p style="margin:0 0 8px; color:#c62828; font-size:13px;">Auto-applied updates caused a validation failure and rollback did not restore health. '
            f"pi-web may be down — SSH in and investigate.</p>"
        )

    for r in reverted:
        lines.append(
            f'<p style="margin:0 0 2px; color:#555; font-size:12px;">Reverted: {r}</p>'
        )
    lines.append("</td></tr>")
    return "\n".join(lines)


def phase_render(run_dir, setup_data):
    """Phase 6: render HTML report from all phase artifacts."""
    print("[phase 6] rendering HTML report")

    date_str = setup_data["date"]
    dry_run = setup_data.get("dry_run", False)
    engine = "update_runner.py (deterministic)" if not dry_run else "update_runner.py --dry-run"

    # Load all phase data
    applied = read_json(run_dir / "01-applied.json") if (run_dir / "01-applied.json").exists() else {"steps": []}
    validation = read_json(run_dir / "02-validation.json") if (run_dir / "02-validation.json").exists() else {"checks": []}
    audit = read_json(run_dir / "03-audit.json") if (run_dir / "03-audit.json").exists() else {"needs_attention": [], "behind_safe": []}
    heartbeat = read_json(run_dir / "05-heartbeat.json") if (run_dir / "05-heartbeat.json").exists() else {}
    rollback = read_json(run_dir / "07-rollback.json") if (run_dir / "07-rollback.json").exists() else None

    # Build sections
    auto_html = "\n".join(_html_step(s) for s in applied.get("steps", []))
    if not auto_html.strip():
        auto_html = '<p style="margin:0; color:#888; font-size:13px;">No auto-apply steps executed.</p>'

    status_summary = "All systems operational."
    for c in validation.get("checks", []):
        if c.get("status") == "fail":
            status_summary = "⚠️ Some validation checks failed — see details below."
            break

    template = TEMPLATE_PATH.read_text()
    html = (
        template.replace("{{DATE}}", date_str)
        .replace("{{DURATION}}", "~1-2 min")
        .replace("{{STATUS_SUMMARY}}", status_summary)
        .replace("{{AUTO_APPLIED}}", auto_html)
        .replace("{{NEEDS_ATTENTION}}", _html_needs_attention(audit.get("needs_attention", [])))
        .replace("{{BEHIND_SAFE}}", _html_behind_safe(audit.get("behind_safe", [])))
        .replace("{{SYSTEM_HEALTH}}", _html_system_health())
        .replace("{{HEARTBEAT}}", _html_heartbeat(heartbeat))
        .replace("{{VALIDATION}}", _html_validation(validation))
        .replace("{{ROLLBACK}}", _html_rollback(rollback))
        .replace(
            "{{NEW_SINCE_YESTERDAY}}",
            "",  # Phase 0 delta — deferred: read prev summary + flag new items
        )
        .replace("{{TIMESTAMP}}", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
        .replace("{{ENGINE}}", engine)
    )

    artifact = run_dir / "06-report.html"
    artifact.write_text(html)
    print(f"[phase 6] done → {artifact}")
    return html


# ── phase 7: rollback ─────────────────────────────────────────────────


def phase_rollback(run_dir, dry_run=False):
    """Phase 7: auto-rollback if pi-web is unhealthy after Phase 1 auto-apply."""
    if dry_run:
        print("[phase 7] DRY RUN — skipping rollback")
        artifact = run_dir / "07-rollback.json"
        write_json(artifact, {"triggered": False, "dry_run": True})
        return

    validation = read_json(run_dir / "02-validation.json") if (run_dir / "02-validation.json").exists() else None
    applied = read_json(run_dir / "01-applied.json") if (run_dir / "01-applied.json").exists() else None

    if not validation or not applied:
        print("[phase 7] skipped — no validation or applied data")
        return

    # Check if pi-web is unhealthy
    pi_web_ok = True
    for c in validation.get("checks", []):
        if c.get("name") == "endpoint_pi-web" and c.get("status") != "ok":
            pi_web_ok = False

    if pi_web_ok:
        print("[phase 7] skipped — pi-web healthy")
        return

    # Check if anything was actually auto-applied in Phase 1 (status=="ok", not "skipped")
    auto_steps = [s for s in applied.get("steps", [])
                  if s.get("step", "").startswith("auto_") and s.get("status") == "ok"]
    owu_step = [s for s in applied.get("steps", [])
                if s.get("step") == "openwebui" and s.get("status") == "bumped"]

    if not auto_steps and not owu_step:
        print("[phase 7] skipped — no packages were actually upgraded (all were already current)")
        artifact = run_dir / "07-rollback.json"
        write_json(artifact, {"triggered": False, "reason": "no_mutations", "validation_failed": True})
        return

    print("[phase 7] ROLLBACK TRIGGERED — pi-web unhealthy after auto-apply")

    reverted = []
    rollback_ok = True

    # Rollback apt packages
    for s in auto_steps:
        pkg = s["step"].replace("auto_", "")
        pre_ver = s.get("pre_version")
        if pre_ver and s.get("status") == "ok":
            print(f"  reverting {pkg} → {pre_ver}")
            try:
                run(
                    [
                        "sudo",
                        "apt",
                        "install",
                        f"{pkg}={pre_ver}",
                        "--allow-downgrades",
                        "-y",
                    ],
                    capture_output=True,
                    text=True,
                )
                reverted.append(f"{pkg} → {pre_ver}")
            except subprocess.CalledProcessError as e:
                rollback_ok = False
                reverted.append(f"{pkg} FAILED: {e}")

    # Rollback open-webui
    for s in owu_step:
        old_tag = s.get("current_tag")
        new_tag = s.get("latest_tag")
        if old_tag and new_tag and OPENWEBUI_COMPOSE.exists():
            print(f"  reverting open-webui: {new_tag} → {old_tag}")
            compose_text = OPENWEBUI_COMPOSE.read_text()
            new_compose = compose_text.replace(
                f"ghcr.io/open-webui/open-webui:{new_tag}",
                f"ghcr.io/open-webui/open-webui:{old_tag}",
            )
            OPENWEBUI_COMPOSE.write_text(new_compose)
            try:
                run(
                    ["docker", "compose", "-f", str(OPENWEBUI_COMPOSE), "pull"],
                    cwd=OPENWEBUI_COMPOSE.parent,
                    capture_output=True,
                    text=True,
                )
                run(
                    ["docker", "compose", "-f", str(OPENWEBUI_COMPOSE), "up", "-d"],
                    cwd=OPENWEBUI_COMPOSE.parent,
                    capture_output=True,
                    text=True,
                )
                reverted.append(f"open-webui {new_tag} → {old_tag}")
            except subprocess.CalledProcessError as e:
                rollback_ok = False
                reverted.append(f"open-webui FAILED: {e}")

    # Only restart docker + cloudflared if we actually reverted something
    if reverted:
        try:
            run(["sudo", "systemctl", "restart", "docker"], capture_output=True, text=True)
        except subprocess.CalledProcessError:
            pass
        time.sleep(30)

        try:
            run(["sudo", "systemctl", "restart", "cloudflared"], capture_output=True, text=True)
        except subprocess.CalledProcessError:
            pass
        time.sleep(10)

    # Re-validate
    re_validation = phase_validate(run_dir)
    # Save as 02b
    artifact_b = run_dir / "02b-validation.json"
    write_json(artifact_b, re_validation)

    # Check if healthy now
    pi_web_now = True
    for c in re_validation.get("checks", []):
        if c.get("name") == "endpoint_pi-web" and c.get("status") != "ok":
            pi_web_now = False

    if pi_web_now:
        status = "healthy"
    else:
        status = "failed"

    data = {
        "triggered": True,
        "status": status,
        "reverted": reverted,
        "re_validation_healthy": pi_web_now,
    }

    # If still unhealthy, gather diagnostic context
    if not pi_web_now:
        diag = {}
        diag["containers"] = run_capture(
            ["docker", "ps", "-a", "--format", "{{.Names}} {{.Status}} {{.Image}}"]
        )
        diag["docker_journal"] = run_capture(
            [
                "sudo",
                "journalctl",
                "-u",
                "docker",
                "--since",
                "10 min ago",
                "--no-pager",
                "-n",
                "50",
            ]
        )
        data["diagnostics"] = diag

    artifact = run_dir / "07-rollback.json"
    write_json(artifact, data)
    print(f"[phase 7] done → {artifact} (status: {status})")
    return data


# ── phase 8: send + archive ──────────────────────────────────────────


def phase_send_archive(run_dir, dry_run=False):
    """Phase 8: send email and archive report."""
    print("[phase 8] send + archive")
    report_path = run_dir / "06-report.html"
    if not report_path.exists():
        print("  no report to send")
        return

    date_str = run_dir.name
    subject = f"Homelab Update Report — {date_str}"

    if dry_run:
        print(f"  DRY RUN — would send: {subject}")
        print(f"  DRY RUN — would archive: {ARCHIVE_DIR / f'{date_str}.html'}")
        report_copy = ARCHIVE_DIR / f"{date_str}.html"
        report_copy.write_text(report_path.read_text())
        print(f"  archived report to {report_copy}")
        return

    # Send email
    try:
        run(
            [
                "python3",
                str(DIGEST_SCRIPT),
                "--subject",
                subject,
                "--body-file",
                str(report_path),
                "--to",
                "carter2099@pm.me",
            ],
            timeout=60,
        )
        print(f"  sent: {subject}")
    except subprocess.CalledProcessError as e:
        print(f"  SEND FAILED: {e}")

    # Archive
    archive_path = ARCHIVE_DIR / f"{date_str}.html"
    archive_path.write_text(report_path.read_text())
    print(f"  archived: {archive_path}")

    # Log
    with open(RUNS_LOG, "a") as f:
        f.write(
            f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} "
            f"update-check duration=Xs rollback=see-07-rollback.json\n"
        )

    # Prune old files
    cutoff = datetime.now() - timedelta(days=30)
    for f in ARCHIVE_DIR.glob("*.html"):
        if f.name.endswith(".html") and len(f.name) > 10:  # dated format
            try:
                file_date = datetime.strptime(f.stem, "%Y-%m-%d")
                if file_date < cutoff:
                    f.unlink()
                    print(f"  pruned: {f.name}")
            except ValueError:
                pass
    for f in ARCHIVE_DIR.glob("*.md"):
        if f.name.endswith(".md") and len(f.name) > 10:
            try:
                file_date = datetime.strptime(f.stem, "%Y-%m-%d")
                if file_date < cutoff:
                    f.unlink()
                    print(f"  pruned: {f.name}")
            except ValueError:
                pass


# ── phase 9: summary ──────────────────────────────────────────────────


def phase_summary(run_dir):
    """Phase 9: write machine-readable .md summary for next-day delta detection."""
    print("[phase 9] writing summary .md")
    date_str = run_dir.name

    audit = read_json(run_dir / "03-audit.json") if (run_dir / "03-audit.json").exists() else {}
    applied = read_json(run_dir / "01-applied.json") if (run_dir / "01-applied.json").exists() else {}
    validation = read_json(run_dir / "02-validation.json") if (run_dir / "02-validation.json").exists() else {}
    heartbeat = read_json(run_dir / "05-heartbeat.json") if (run_dir / "05-heartbeat.json").exists() else {}

    lines = [
        f"# Homelab Update Report — {date_str}",
        f"**Engine:** update_runner.py (deterministic) | **Sent to:** carter2099@pm.me",
        "",
        "## Auto-Applied",
    ]

    for s in applied.get("steps", []):
        if s.get("dry_run"):
            lines.append("- Dry run — no mutations applied")
            break
        name = s.get("step", "")
        status = s.get("status", "")
        if name == "apt_upgrade":
            lines.append(f"- apt upgrade: {s.get('upgraded_count', 0)} packages")
        elif name.startswith("auto_"):
            pkg = name.replace("auto_", "")
            if status == "ok":
                lines.append(f"- {pkg} — {s.get('pre_version')} → {s.get('post_version')}")
            elif status == "skipped":
                lines.append(f"- {pkg} — already current ({s.get('pre_version')})")
            else:
                lines.append(f"- {pkg} — FAILED: {s.get('error', 'unknown')}")
        elif name.startswith("k3s_"):
            svc = name.replace("k3s_", "")
            lines.append(f"- {svc} — rollout restart {'successful' if status == 'ok' else 'FAILED'}")
        elif name == "openwebui":
            if status == "bumped":
                lines.append(f"- open-webui — {s.get('current_tag')} → {s.get('latest_tag')}")
            elif status == "current":
                lines.append(f"- open-webui — current at {s.get('current_tag')}")
            else:
                lines.append(f"- open-webui — {status}")

    lines.append("")
    lines.append("## Needs Attention")
    for it in audit.get("needs_attention", []):
        lines.append(f"- {it.get('item')} — {it.get('reason')}")

    lines.append("")
    lines.append("## Behind but Safe")
    for it in audit.get("behind_safe", []):
        lines.append(f"- {it.get('item')} — {it.get('reason')}")

    lines.append("")
    lines.append("## System Health")

    # Re-add system health stats
    df = run_capture(["df", "-h", "/"])
    if df:
        parts = df.splitlines()[-1].split()
        if len(parts) >= 5:
            lines.append(f"- Disk: {parts[4]} used on / ({parts[2]} / {parts[1]})")
    free = run_capture(["free", "-h"])
    if free:
        mem_line = [l for l in free.splitlines() if "Mem:" in l]
        if mem_line:
            parts = mem_line[0].split()
            if len(parts) >= 7:
                lines.append(f"- Memory: {parts[6]} available ({parts[2]} used / {parts[1]} total)")
    uptime = run_capture(["uptime"])
    lines.append(f"- Uptime: {uptime.strip() if uptime else 'unknown'}")
    reboot = (Path("/var/run/reboot-required")).exists()
    lines.append(f"- Reboot needed: {'YES' if reboot else 'no'}")

    # Endpoint summary
    ep_ok = all(
        c.get("status") == "ok"
        for c in validation.get("checks", [])
        if c.get("name", "").startswith("endpoint_")
    )
    lines.append(f"- Endpoints: {'all passed' if ep_ok else 'SOME FAILED'}")

    lines.append("")
    lines.append("## Heartbeat")
    hb = heartbeat
    user_failed = hb.get("failed_units", {}).get("user", [])
    sys_failed = hb.get("failed_units", {}).get("system", [])
    total_failed = len([x for x in user_failed if x]) + len([x for x in sys_failed if x])
    lines.append(f"- Failed units: {total_failed}")
    lines.append(f"- LLM fallback: {'yes' if hb.get('llm_stack', {}).get('falling_back') else 'no'}")
    lines.append(f"- Last backup: {hb.get('backup', {}).get('last_run', 'unknown')}")

    md_content = "\n".join(lines) + "\n"
    artifact = ARCHIVE_DIR / f"{date_str}.md"
    artifact.write_text(md_content)
    print(f"[phase 9] done → {artifact}")


# ── main ──────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Homelab update agent — deterministic Python orchestrator"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip mutations and email; still audit, render, and archive",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip phases whose output artifact already exists",
    )
    args = parser.parse_args()

    start_ts = time.time()

    # Phase 0
    setup = phase_setup(args)
    run_dir = Path(setup["run_dir"])

    def should_run(artifact_name):
        if not args.resume:
            return True
        return not (run_dir / artifact_name).exists()

    # Phase 1 — always runs (mutations), unless dry-run
    phase_apply(run_dir, dry_run=args.dry_run)

    # Phase 2 — validate
    if should_run("02-validation.json"):
        phase_validate(run_dir)
    else:
        print("[phase 2] skipped (resume)")

    # Phase 7 — rollback (conditional, runs between 1/2 and 6)
    phase_rollback(run_dir, dry_run=args.dry_run)

    # Phase 3 — audit
    if should_run("03-audit.json"):
        phase_audit(run_dir)
    else:
        print("[phase 3] skipped (resume)")

    # Phase 4 — open-webui tag check
    if should_run("04-openwebui.json"):
        phase_openwebui_check(run_dir)
    else:
        print("[phase 4] skipped (resume)")

    # Phase 5 — heartbeat
    if should_run("05-heartbeat.json"):
        phase_heartbeat(run_dir)
    else:
        print("[phase 5] skipped (resume)")

    # Phase 6 — render
    phase_render(run_dir, setup)

    # Phase 8 — send + archive
    phase_send_archive(run_dir, dry_run=args.dry_run)

    # Phase 9 — summary
    phase_summary(run_dir)

    elapsed = time.time() - start_ts
    print(f"\nDone in {elapsed:.0f}s")


if __name__ == "__main__":
    main()
