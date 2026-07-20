#!/usr/bin/env python3
"""
Homelab Steward — nightly deterministic Python orchestrator.
Replaces update-check + agents-md-audit. Adds work queue + kimi-k3 executor.

Scheduled via homelab-steward.timer. Every phase writes a numbered artifact;
skip-if-exists resume; failures become email badges, never sys.exit mid-run.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── constants ────────────────────────────────────────────────────────

HOME = Path.home()
RUN_DIR_BASE = HOME / "digests" / "steward"
TEMPLATE_PATH = RUN_DIR_BASE / "template.html"
RUNS_LOG = RUN_DIR_BASE / ".runs.jsonl"
K3S = "/usr/local/bin/k3s"
GH_API = "https://api.github.com/repos/open-webui/open-webui/releases/latest"
OPENWEBUI_COMPOSE = HOME / "open-webui" / "docker-compose.yml"
DIGEST_SCRIPT = HOME / "scripts" / "send_digest.py"
SESSION_DIR = HOME / ".omp" / "agent" / "sessions-automated"
IDEAS_DIR = HOME / "ideas"
PLANS_DIR = HOME / "plans"
AUTO_PKGS = [
    "docker-ce", "docker-ce-cli", "containerd.io", "docker-buildx-plugin",
    "docker-compose-plugin", "cloudflared",
]
ENDPOINTS = {
    "open-webui": "http://127.0.0.1:48100",
    "blog": "http://127.0.0.1:33099",
    "delta_neutral": "http://127.0.0.1:43080",
    "pi-web": "http://127.0.0.1:8504",
    "llm-proxy": "http://127.0.0.1:8081/health",
}
STEWARD_MODEL = "opencode-go/deepseek-v4-pro"
PROXY_HEALTH = "http://localhost:8082/health"
GUARD_ROLLING_DEFER = 90
GUARD_SKIP = 90
GUARD_RESTRICT = 70
EXECUTOR_MONTHLY_CAP = 4
MAX_WORKERS = 3
EXECUTOR_TIMEOUT = 2700
EXECUTOR_MODE = "execute"
PENDING_PATH = HOME / "agent-state" / "pending.md"

# ── default template ─────────────────────────────────────────────────

DEFAULT_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="margin:0; padding:0; background-color:#f4f4f7; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
<table role="presentation" width="100%" style="background-color:#f4f4f7; padding:24px 0;">
<tr><td align="center">
<table role="presentation" width="600" style="max-width:600px; width:100%; background-color:#ffffff; border-radius:8px; overflow:hidden; box-shadow:0 1px 3px rgba(0,0,0,0.08);">
<tr><td style="background-color:#1a1a2e; padding:28px 32px;">
<h1 style="margin:0; color:#ffffff; font-size:22px; font-weight:600;">Homelab Steward</h1>
<p style="margin:6px 0 0; color:#a0a0b8; font-size:14px;">{{DATE}}</p></td></tr>
<tr><td style="padding:24px 32px 16px;">
<p style="margin:0; color:#444; font-size:15px; line-height:1.6;">{{TLDR}}</p></td></tr>
{{TROUBLESHOOT}}
<tr><td style="padding:16px 32px 8px;"><h2 style="margin:0; color:#2e7d32; font-size:15px; font-weight:700;">Updates Applied</h2></td></tr>
<tr><td style="padding:8px 32px 16px;">{{UPDATES}}</td></tr>
<tr><td style="padding:0 32px;"><hr style="border:none; border-top:1px solid #e8e8ee; margin:8px 0;"></td></tr>
<tr><td style="padding:16px 32px 8px;"><h2 style="margin:0; color:#1565c0; font-size:15px; font-weight:700;">Validation</h2></td></tr>
<tr><td style="padding:8px 32px 16px;">{{VALIDATION}}</td></tr>
<tr><td style="padding:0 32px;"><hr style="border:none; border-top:1px solid #e8e8ee; margin:8px 0;"></td></tr>
<tr><td style="padding:16px 32px 8px;"><h2 style="margin:0; color:#1a1a2e; font-size:15px; font-weight:700;">Heartbeat</h2></td></tr>
<tr><td style="padding:8px 32px 16px;">{{HEARTBEAT}}</td></tr>
<tr><td style="padding:0 32px;"><hr style="border:none; border-top:1px solid #e8e8ee; margin:8px 0;"></td></tr>
<tr><td style="padding:16px 32px 8px;"><h2 style="margin:0; color:#6a1b9a; font-size:15px; font-weight:700;">Audit</h2></td></tr>
<tr><td style="padding:8px 32px 16px;">{{AUDIT}}</td></tr>
<tr><td style="padding:0 32px;"><hr style="border:none; border-top:1px solid #e8e8ee; margin:8px 0;"></td></tr>
<tr><td style="padding:16px 32px 8px;"><h2 style="margin:0; color:#e65100; font-size:15px; font-weight:700;">Work Queue</h2></td></tr>
<tr><td style="padding:8px 32px 16px;">{{QUEUE}}</td></tr>
<tr><td style="padding:0 32px;"><hr style="border:none; border-top:1px solid #e8e8ee; margin:8px 0;"></td></tr>
<tr><td style="padding:16px 32px 8px;"><h2 style="margin:0; color:#00838f; font-size:15px; font-weight:700;">Executor</h2></td></tr>
<tr><td style="padding:8px 32px 16px;">{{EXECUTOR}}</td></tr>
<tr><td style="padding:0 32px;"><hr style="border:none; border-top:1px solid #e8e8ee; margin:8px 0;"></td></tr>
<tr><td style="padding:16px 32px 8px;"><h2 style="margin:0; color:#555; font-size:15px; font-weight:700;">Budget</h2></td></tr>
<tr><td style="padding:8px 32px 16px;">{{BUDGET}}</td></tr>
<tr><td style="padding:24px 32px; background-color:#f8f8fb; border-top:1px solid #e8e8ee;">
<p style="margin:0; color:#999; font-size:12px; text-align:center;">{{FOOTER}}</p></td></tr>
</table></td></tr></table></body></html>"""

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


def user_env():
    """Return env dict with XDG_RUNTIME_DIR set for systemctl --user."""
    return {**os.environ, "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"}


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, default=str) + "\n")


def read_json(path):
    return json.loads(path.read_text())


def prev_workday(today):
    """Return yesterday's date."""
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
        m = re.match(r"^(\S+)/\S+\s+(\S+)\s+\S+\s+\[upgradable from:\s+(.+)\]", line)
        if m:
            result[m.group(1)] = f"{m.group(3)} -> {m.group(2)}"
    return result


def _date_context():
    """Return a date-context string for LLM prompts."""
    now = datetime.now(timezone.utc)
    return (
        f"Today is {now.strftime('%Y-%m-%d')} "
        f"({now.strftime('%A')}). "
        f"The current time is {now.strftime('%H:%M')} UTC."
    )


def _call_omp_p(prompt, model=STEWARD_MODEL, timeout=600, append_system=None):
    """Call omp -p (headless text mode). Returns stdout."""
    cmd = [
        "omp", "-p", "--model", model,
        "--session-dir", str(SESSION_DIR),
        "--allow-home",
    ]
    if append_system:
        cmd.extend(["--append-system-prompt", append_system])

    result = subprocess.run(
        cmd,
        input=prompt,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "HOME": str(HOME)},
    )
    if result.returncode != 0 and not result.stdout.strip():
        raise RuntimeError(
            f"omp -p failed (rc={result.returncode}): {result.stderr[:500]}"
        )
    return result.stdout


def _extract_json(text, label="output"):
    """Extract JSON from LLM output. Tries markdown fences first, then raw JSON."""
    m = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass
    for pattern in [r"\{[\s\S]*\}", r"\[[\s\S]*\]"]:
        m = re.search(pattern, text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                continue
    text_stripped = text.strip()
    if text_stripped.startswith("{") or text_stripped.startswith("["):
        try:
            return json.loads(text_stripped)
        except json.JSONDecodeError:
            pass
    raise ValueError(
        f"Could not extract JSON from {label}. Raw text (first 500 chars):\n{text[:500]}"
    )


def extract_from_ndjson(stdout):
    """Parse pi --mode json NDJSON output.
    Returns (accumulated_assistant_text, stats_dict).
    Real shape (verified live 2026-07-20):
      message_update -> {"assistantMessageEvent": {"type": "text_delta", "delta": "..."}}
      message_end    -> {"message": {"usage": {"input":N,"output":N,"cost":{"total":F}}}}
    """
    accumulated = []
    stats = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    for line in stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        typ = obj.get("type", "")
        if typ == "message_update":
            ev = obj.get("assistantMessageEvent", {})
            if ev.get("type") == "text_delta":
                accumulated.append(ev.get("delta", ""))
        elif typ == "message_end":
            usage = obj.get("message", {}).get("usage", {})
            if usage:
                stats["input_tokens"] += usage.get("input", 0)
                stats["output_tokens"] += usage.get("output", 0)
                cost = usage.get("cost", {})
                if isinstance(cost, dict):
                    stats["cost_usd"] += cost.get("total", 0.0)
    return "".join(accumulated), stats


def _call_omp_p_json(prompt, timeout=EXECUTOR_TIMEOUT, extra_args=None):
    """Call omp -p in --mode json. Returns (accumulated_text, stats, packet, raw_stdout)."""
    cmd = [
        "omp", "-p", "--model", STEWARD_MODEL, "--mode", "json",
        "--session-dir", str(SESSION_DIR),
        "--allow-home",
    ]
    if extra_args:
        cmd.extend(extra_args)

    result = subprocess.run(
        cmd,
        input=prompt,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "HOME": str(HOME)},
    )
    if result.returncode != 0 and not result.stdout.strip():
        raise RuntimeError(
            f"omp -p json failed (rc={result.returncode}): {result.stderr[:500]}"
        )

    text, stats = extract_from_ndjson(result.stdout)
    try:
        packet = _extract_json(text, "executor packet")
    except ValueError:
        packet = {"raw_text": text[:2000]}
    return text, stats, packet, result.stdout


def _evidence_hash(evidence):
    """Return a deterministic SHA256 hash of an evidence dict for delta comparison."""
    raw = json.dumps(evidence, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def _load_prev_artifact(run_dir, prev_date_str, name):
    """Load a named artifact from the previous run dir, if it exists."""
    prev_dir = RUN_DIR_BASE / prev_date_str
    path = prev_dir / name
    if path.exists():
        try:
            return read_json(path)
        except (json.JSONDecodeError, OSError):
            return None
    return None



def _reboot_if_needed(run_dir, phase_label, dry_run=False):
    """Check /var/run/reboot-required. If present and not dry-run, write pending.md and reboot.

    Returns True if a reboot was triggered (caller should exit after this).
    """
    REBOOT_FLAG = Path("/var/run/reboot-required")
    if not REBOOT_FLAG.exists():
        return False

    if dry_run:
        print(f"  [reboot] DRY RUN — /var/run/reboot-required exists (would reboot)")
        return False

    print(f"  [reboot] /var/run/reboot-required detected — writing pending.md and rebooting")

    # Write pending.md with full context for boot-time resume
    PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    pending_content = f"""# Pending Task — {now_ts}
**Reason:** Kernel update requires reboot after steward {phase_label}
**Action:** Run `python3 ~/scripts/steward_runner.py --resume` to continue
**Run dir:** {run_dir}
**Completed phases:** through {phase_label}
**Context:** The homelab steward was mid-run when a kernel update (or other
/var/run/reboot-required trigger) was detected. On resume, the steward will
pick up from the next phase in {run_dir}.
"""
    PENDING_PATH.write_text(pending_content)
    print(f"  [reboot] wrote {PENDING_PATH}")

    try:
        run(["sudo", "systemctl", "reboot"], capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        print(f"  [reboot] reboot command failed: {e}")
        return False

    # If we get here, reboot was accepted — but Python may continue briefly.
    # The caller should still exit.
    return True

# ── P0: setup ────────────────────────────────────────────────────────


def phase_0_setup(args):
    """Create run dir, budget guard, dependabot check, prev-summary delta."""
    today = datetime.now()
    date_str = today.strftime("%Y-%m-%d")
    run_dir = RUN_DIR_BASE / date_str
    run_dir.mkdir(parents=True, exist_ok=True)

    prev_date = prev_workday(today)
    prev_date_str = prev_date.strftime("%Y-%m-%d")
    prev_md = RUN_DIR_BASE / f"{prev_date_str}" / "summary.md"
    prev_summary = parse_previous_summary(prev_md)

    # Budget guard — snapshot proxy health
    budget = {"rolling_pct": 0, "weekly_pct": 0, "monthly_pct": 0,
              "guard_verdict": "full", "accounts": []}
    try:
        req = urllib.request.Request(PROXY_HEALTH)
        with urllib.request.urlopen(req, timeout=10) as resp:
            proxy_health = json.loads(resp.read().decode())
    except Exception as e:
        proxy_health = {"error": str(e)}
        budget["guard_verdict"] = "proxy_unreachable"

    if "accounts" in proxy_health:
        max_rolling = 0
        max_weekly = 0
        max_monthly = 0
        for acct in proxy_health["accounts"]:
            rp = acct.get("rolling", {}).get("pct", 0)
            wp = acct.get("weekly", {}).get("pct", 0)
            mp = acct.get("monthly", {}).get("pct", 0)
            max_rolling = max(max_rolling, rp)
            max_weekly = max(max_weekly, wp)
            max_monthly = max(max_monthly, mp)
            budget["accounts"].append({
                "tier": acct.get("tier", "unknown"),
                "rolling_pct": rp, "weekly_pct": wp, "monthly_pct": mp,
            })
        budget["rolling_pct"] = max_rolling
        budget["weekly_pct"] = max_weekly
        budget["monthly_pct"] = max_monthly

        if max_rolling >= GUARD_ROLLING_DEFER:
            budget["guard_verdict"] = "defer_all"
        elif max_weekly >= GUARD_SKIP or max_monthly >= GUARD_SKIP:
            budget["guard_verdict"] = "skip_agents"
        elif max_weekly >= GUARD_RESTRICT:
            budget["guard_verdict"] = "anomaly_only"
        else:
            budget["guard_verdict"] = "full"

    # Dependabot in-flight check
    dep_check = {"in_flight": False, "raw": ""}
    try:
        dep_out = run_capture(
            ["journalctl", "--user", "-u", "dependabot-webhook",
             "--since", "15 min ago", "--no-pager", "-q"],
            env=user_env(),
        )
        dep_check["raw"] = dep_out[:500]
        if dep_out.strip() and "processing" in dep_out.lower():
            dep_check["in_flight"] = True
    except Exception as e:
        dep_check["error"] = str(e)

    data = {
        "date": date_str,
        "run_dir": str(run_dir),
        "prev_date": prev_date_str,
        "prev_summary_exists": prev_md.exists(),
        "dry_run": args.dry_run,
        "resume": args.resume,
        "budget": budget,
        "dependabot": dep_check,
    }
    artifact = run_dir / "00-setup.json"
    write_json(artifact, data)
    print(f"[P0] setup -> {artifact}")
    print(f"  budget guard: {budget['guard_verdict']} "
          f"(rolling={budget['rolling_pct']}%, weekly={budget['weekly_pct']}%, "
          f"monthly={budget['monthly_pct']}%)")
    if dep_check["in_flight"]:
        print("  dependabot: IN FLIGHT — executor deferred")
    return data


# ── P1: update apply (ported from update_runner) ─────────────────────


def _p1_apt_upgrade():
    """Run apt update + apt upgrade -y."""
    print("  [1a] apt update + upgrade")
    try:
        run(["sudo", "apt", "update"], capture_output=True, text=True)
        upgrade = run(["sudo", "apt", "upgrade", "-y"], capture_output=True, text=True)
        stdout = upgrade.stdout
        upgraded = 0
        m = re.search(r"(\d+)\s+upgraded", stdout)
        if m:
            upgraded = int(m.group(1))
        return {"step": "apt_upgrade", "status": "ok", "upgraded_count": upgraded,
                "output_tail": "\n".join(stdout.strip().splitlines()[-20:])}
    except subprocess.CalledProcessError as e:
        return {"step": "apt_upgrade", "status": "failed", "error": str(e),
                "output": e.stdout if e.stdout else ""}


def _p1_auto_pkgs():
    """Auto-apply docker-* and cloudflared upgrades with pre-version capture."""
    results = []
    for pkg in AUTO_PKGS:
        print(f"  [1b] auto-apply {pkg}")
        pre_ver = apt_installed_version(pkg)
        try:
            run(["sudo", "apt", "install", "--only-upgrade", pkg, "-y"],
                capture_output=True, text=True)
            post_ver = apt_installed_version(pkg)
            results.append({
                "step": f"auto_{pkg}", "status": "ok" if post_ver != pre_ver else "skipped",
                "pre_version": pre_ver, "post_version": post_ver,
            })
        except subprocess.CalledProcessError as e:
            results.append({
                "step": f"auto_{pkg}", "status": "failed",
                "pre_version": pre_ver, "error": str(e),
                "output": e.stdout.strip() if e.stdout else "",
            })
    return results


def _p1_docker_assert():
    """Assert docker daemon root == /var/lib/docker."""
    print("  [1c] assert docker daemon root")
    try:
        root = run(["docker", "info", "--format", "{{.DockerRootDir}}"],
                   capture_output=True, text=True, timeout=30).stdout.strip()
    except subprocess.CalledProcessError as e:
        return {"step": "docker_daemon_assert", "status": "failed",
                "error": f"docker info failed: {e}"}
    if root != "/var/lib/docker":
        return {"step": "docker_daemon_assert", "status": "failed",
                "error": f"unexpected DockerRootDir: {root!r}"}
    return {"step": "docker_daemon_assert", "status": "ok", "root": root}


def _p1_k3s_rollouts():
    """Rollout restart freshrss."""
    results = []
    env = user_env()
    for name, ns, timeout_s in [
        ("freshrss", "freshrss", 120),
    ]:
        print(f"  [1e] k3s rollout restart {name}/{ns}")
        try:
            run([K3S, "kubectl", "rollout", "restart", f"deploy/{name}", "-n", ns],
                env=env, capture_output=True, text=True)
            run([K3S, "kubectl", "rollout", "status", f"deploy/{name}", "-n", ns,
                 f"--timeout={timeout_s}s"],
                env=env, capture_output=True, text=True)
            results.append({"step": f"k3s_{name}", "status": "ok"})
        except subprocess.CalledProcessError as e:
            stderr_tail = (e.stderr or "").strip()
            msg = str(e)
            if stderr_tail:
                msg += f" | stderr: {stderr_tail[-500:]}"
            results.append({"step": f"k3s_{name}", "status": "failed", "error": msg})
    return results


def _p1_openwebui():
    """Check open-webui GitHub releases for a newer stable tag, bump if found."""
    print("  [1f] open-webui tag check")
    if not OPENWEBUI_COMPOSE.exists():
        return {"step": "openwebui", "status": "skipped",
                "reason": f"compose file not found: {OPENWEBUI_COMPOSE}"}

    compose_text = OPENWEBUI_COMPOSE.read_text()
    current_m = re.search(r"ghcr\.io/open-webui/open-webui:([^\s\"']+)", compose_text)
    current_tag = current_m.group(1) if current_m else None
    if not current_tag:
        return {"step": "openwebui", "status": "skipped",
                "reason": "could not parse current tag from compose file"}

    latest_tag = None
    try:
        req = urllib.request.Request(GH_API, headers={"Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            release = json.loads(resp.read().decode())
            latest_tag = release.get("tag_name", "").lstrip("v")
    except Exception as e:
        return {"step": "openwebui", "status": "error",
                "reason": f"GitHub API unreachable: {e}", "current_tag": current_tag}

    if not latest_tag:
        return {"step": "openwebui", "status": "error",
                "reason": "no tag_name in GitHub release", "current_tag": current_tag}

    cur_clean = current_tag.lstrip("v")
    lat_clean = latest_tag.lstrip("v")
    if cur_clean == lat_clean:
        return {"step": "openwebui", "status": "current",
                "current_tag": current_tag, "latest_tag": latest_tag}

    print(f"    bumping open-webui: {current_tag} -> {latest_tag}")
    new_compose = compose_text.replace(
        f"ghcr.io/open-webui/open-webui:{current_tag}",
        f"ghcr.io/open-webui/open-webui:{latest_tag}",
    )
    OPENWEBUI_COMPOSE.write_text(new_compose)

    try:
        run(["docker", "compose", "-f", str(OPENWEBUI_COMPOSE), "pull"],
            cwd=OPENWEBUI_COMPOSE.parent, capture_output=True, text=True)
        run(["docker", "compose", "-f", str(OPENWEBUI_COMPOSE), "up", "-d"],
            cwd=OPENWEBUI_COMPOSE.parent, capture_output=True, text=True)
        healthy = False
        for _ in range(30):
            time.sleep(1)
            status = run_capture(["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"])
            for line in status.splitlines():
                if "open-webui" in line and "healthy" in line.lower():
                    healthy = True
                    break
            if healthy:
                break
        return {"step": "openwebui", "status": "bumped",
                "current_tag": current_tag, "latest_tag": latest_tag, "healthy": healthy}
    except subprocess.CalledProcessError as e:
        return {"step": "openwebui", "status": "failed",
                "current_tag": current_tag, "latest_tag": latest_tag, "error": str(e)}


def phase_1_apply(run_dir, dry_run=False):
    """Phase 1: apply safe updates. Skip if --dry-run."""
    if dry_run:
        print("[P1] DRY RUN — skipping all mutations")
        data = {"dry_run": True, "steps": []}
        write_json(run_dir / "01-applied.json", data)
        return data

    print("[P1] applying safe updates")
    steps = []

    # 1a: apt upgrade
    result = _p1_apt_upgrade()
    steps.append(result)
    if result["status"] == "failed":
        print(f"  FAILED: apt upgrade — {result.get('error')}")
        data = {"steps": steps}
        write_json(run_dir / "01-applied.json", data)
        return data

    # 1b: auto-apply docker + cloudflared
    auto_results = _p1_auto_pkgs()
    steps.extend(auto_results)
    for r in auto_results:
        if r["status"] == "failed":
            print(f"  FAILED: {r['step']} — {r.get('error')}")
            data = {"steps": steps}
            write_json(run_dir / "01-applied.json", data)
            return data

    # 1c: docker pause + assert if docker packages upgraded
    docker_upgraded = any(
        s["step"].startswith("auto_docker") and s["status"] == "ok"
        for s in auto_results
    )
    if docker_upgraded:
        print("  [1c] docker daemon restart pause (10s)")
        time.sleep(10)
        steps.append({"step": "docker_pause", "status": "ok"})
        steps.append(_p1_docker_assert())

    # 1d: cloudflared restart if upgraded
    cloudflared_upgraded = any(
        s["step"] == "auto_cloudflared" and s["status"] == "ok"
        for s in auto_results
    )
    if cloudflared_upgraded:
        print("  [1d] restart cloudflared")
        try:
            run(["sudo", "systemctl", "restart", "cloudflared"], capture_output=True, text=True)
            time.sleep(5)
            steps.append({"step": "cloudflared_restart", "status": "ok"})
        except subprocess.CalledProcessError as e:
            steps.append({"step": "cloudflared_restart", "status": "failed", "error": str(e)})

    # 1e: k3s rollouts
    steps.extend(_p1_k3s_rollouts())

    # 1f: open-webui
    steps.append(_p1_openwebui())

    data = {"steps": steps}
    write_json(run_dir / "01-applied.json", data)
    n_ok = sum(1 for s in steps if s["status"] == "ok")
    n_bumped = sum(1 for s in steps if s["status"] == "bumped")
    n_skipped = sum(1 for s in steps if s["status"] == "skipped")
    n_failed = sum(1 for s in steps if s["status"] == "failed")
    print(f"[P1] done -> {run_dir / '01-applied.json'}")
    print(f"  {n_ok} ok, {n_bumped} bumped, {n_skipped} skipped, {n_failed} failed")
    return data


# ── P2: validate (ported from update_runner) ─────────────────────────


def phase_2_validate(run_dir):
    """Phase 2: run all validation checks."""
    print("[P2] validating services")
    checks = []

    # Docker containers
    out = run_capture(["docker", "ps", "-a", "--format", "{{.Names}} {{.Status}}"])
    checks.append({"name": "docker_containers", "output": out, "status": "ok"})

    # k3s pods
    env = user_env()
    bad_pods = run_capture([K3S, "kubectl", "get", "pods", "-A", "--no-headers"], env=env)
    bad_lines = [
        l for l in bad_pods.splitlines()
        if not re.search(r"\b(Running|Completed)\b", l)
    ]
    checks.append({
        "name": "k3s_pods", "status": "ok" if not bad_lines else "warning",
        "bad_pods": bad_lines, "output": bad_pods if bad_lines else "",
    })

    # Endpoint curls
    for name, url in ENDPOINTS.items():
        code = run_capture(["curl", "-so", "/dev/null", "-w", "%{http_code}", url])
        healthy = code.startswith("2") or code.startswith("3")
        checks.append({
            "name": f"endpoint_{name}", "url": url,
            "http_code": code, "status": "ok" if healthy else "fail",
        })

    # LLM proxy X-Fallback header
    fallback = run_capture(["curl", "-sI", "http://127.0.0.1:8081/health"])
    fallback_active = "X-Fallback: true" in fallback
    checks.append({
        "name": "llm_fallback", "status": "warning" if fallback_active else "ok",
        "fallback_active": fallback_active,
    })

    data = {"checks": checks}
    write_json(run_dir / "02-validation.json", data)
    print(f"[P2] done -> {run_dir / '02-validation.json'}")
    return data


# ── P3: troubleshoot ─────────────────────────────────────────────────


def phase_3_troubleshoot(run_dir, dry_run=False):
    """Phase 3: spawn omp troubleshooting agent if pi-web is unhealthy after P1 auto-apply.

    Instead of deterministic rollback, we spawn a deepseek-v4-pro agent with full system
    access to diagnose and fix the issue on the new versions. If the agent can't fix it,
    the failure is reported — we stay on the new versions and let Carter handle it.
    """
    if dry_run:
        print("[P3] DRY RUN — skipping troubleshooting agent")
        write_json(run_dir / "03-troubleshoot.json", {"triggered": False, "dry_run": True})
        return

    validation_path = run_dir / "02-validation.json"
    applied_path = run_dir / "01-applied.json"
    if not validation_path.exists() or not applied_path.exists():
        print("[P3] skipped — no validation or applied data")
        return

    validation = read_json(validation_path)
    applied = read_json(applied_path)

    pi_web_ok = True
    for c in validation.get("checks", []):
        if c.get("name") == "endpoint_pi-web" and c.get("status") != "ok":
            pi_web_ok = False

    if pi_web_ok:
        print("[P3] skipped — pi-web healthy")
        write_json(run_dir / "03-troubleshoot.json", {"triggered": False, "reason": "healthy"})
        return

    auto_steps = [s for s in applied.get("steps", [])
                  if s.get("step", "").startswith("auto_") and s.get("status") == "ok"]
    owu_step = [s for s in applied.get("steps", [])
                if s.get("step") == "openwebui" and s.get("status") == "bumped"]

    if not auto_steps and not owu_step:
        print("[P3] skipped — no packages were actually upgraded")
        write_json(run_dir / "03-troubleshoot.json",
                   {"triggered": False, "reason": "no_mutations", "validation_failed": True})
        return

    print("[P3] TROUBLESHOOT — pi-web unhealthy after auto-apply, spawning diagnostic agent")

    # Gather full diagnostic context
    diag = {
        "applied_steps": applied.get("steps", []),
        "validation": {c["name"]: c.get("status", "?") for c in validation.get("checks", [])},
        "containers": run_capture(
            ["docker", "ps", "-a", "--format", "{{.Names}} {{.Status}} {{.Image}}"]),
        "docker_journal": run_capture(
            ["sudo", "journalctl", "-u", "docker", "--since", "30 min ago",
             "--no-pager", "-n", "80"]),
        "pi_web_journal": run_capture(
            ["journalctl", "--user", "-u", "pi-web", "--since", "30 min ago",
             "--no-pager", "-n", "50"], env=user_env()),
        "pi_web_sessiond_journal": run_capture(
            ["journalctl", "--user", "-u", "pi-web-sessiond", "--since", "30 min ago",
             "--no-pager", "-n", "50"], env=user_env()),
    }

    troubleshoot_prompt = f"""
You are a homelab troubleshooter. The nightly steward auto-applied updates and now
pi-web (the always-on remote agent at pi.carter2099.com) is DOWN or unhealthy.

Your job: diagnose WHY it's down and FIX IT so we stay on the new versions.
Rolling back is a LAST RESORT — prefer fixing forward.

WHAT CHANGED (P1 applied steps):
{json.dumps(diag["applied_steps"], indent=2)}

VALIDATION RESULTS:
{json.dumps(diag["validation"], indent=2)}

DIAGNOSTICS:
- Containers:
{diag["containers"]}
- Docker journal:
{diag["docker_journal"]}
- pi-web journal:
{diag["pi_web_journal"]}
- pi-web-sessiond journal:
{diag["pi_web_sessiond_journal"]}

RULES:
- You have full system access — use it.
- Common causes: orphaned docker-proxy holding a port (check ss -tlnp), docker daemon
  failed to restart after engine upgrade, cloudflared tunnel down, pi-web config mismatch,
  sessiond crash.
- Export XDG_RUNTIME_DIR=/run/user/$(id -u) before any systemctl --user commands.
- If the fix is restarting a service, do it. If it's killing a docker-proxy, do it.
- If you genuinely cannot fix it, say so clearly and explain why.

Return a fenced ```json packet:
{{"status": "fixed"|"partial"|"failed",
 "diagnosis": "root cause in one sentence",
 "actions_taken": ["action 1", "action 2"],
 "pi_web_healthy": true|false,
 "remaining_issues": ["..."]}}
"""

    agent_output = ""
    agent_packet = {}
    try:
        agent_output = _call_omp_p(troubleshoot_prompt, timeout=600)
        agent_packet = _extract_json(agent_output, "troubleshoot packet")
    except Exception as e:
        agent_packet = {"status": "agent-failed", "diagnosis": str(e),
                        "actions_taken": [], "pi_web_healthy": False,
                        "remaining_issues": []}

    # Re-validate after agent
    re_validation = phase_2_validate(run_dir)
    write_json(run_dir / "02b-validation.json", re_validation)

    pi_web_now = True
    for c in re_validation.get("checks", []):
        if c.get("name") == "endpoint_pi-web" and c.get("status") != "ok":
            pi_web_now = False

    data = {
        "triggered": True,
        "agent_status": agent_packet.get("status", "unknown"),
        "diagnosis": agent_packet.get("diagnosis", ""),
        "actions_taken": agent_packet.get("actions_taken", []),
        "pi_web_healthy": pi_web_now,
        "remaining_issues": agent_packet.get("remaining_issues", []),
        "agent_raw": agent_output[:4000],
        "re_validation_healthy": pi_web_now,
    }
    if not pi_web_now:
        data["final_diagnostics"] = {
            "containers": run_capture(
                ["docker", "ps", "-a", "--format", "{{.Names}} {{.Status}} {{.Image}}"]),
        }
    write_json(run_dir / "03-troubleshoot.json", data)
    print(f"[P3] done -> {run_dir / '03-troubleshoot.json'} "
          f"(agent: {agent_packet.get('status')}, pi-web: {'healthy' if pi_web_now else 'still down'})")
    return data
def phase_4_heartbeat(run_dir):
    """Phase 4: extended heartbeat block."""
    print("[P4] heartbeat checks")
    env = user_env()

    # Failed systemd units
    failed_user = run_capture(["systemctl", "--user", "--failed", "--no-legend"], env=env)
    failed_system = run_capture(["systemctl", "--failed", "--no-legend"])

    # LLM stack health
    llm_health = run_capture(["curl", "-s", "http://127.0.0.1:8081/health"])
    fallback_headers = run_capture(["curl", "-sI", "http://127.0.0.1:8081/health"])
    falling_back = "X-Fallback: true" in fallback_headers

    # Backup recency
    backup_ts = run_capture(
        ["systemctl", "--user", "show", "homelab-backup", "-p", "ExecMainStartTimestamp"],
        env=env,
    ).replace("ExecMainStartTimestamp=", "").strip()

    # k3s node conditions
    nodes = run_capture([K3S, "kubectl", "get", "nodes", "-o", "wide"], env=env)

    # Disk usage
    disk_df = run_capture(["df", "-h", "/"])
    docker_df = run_capture(["docker", "system", "df"])

    # Reboot required
    reboot_needed = (Path("/var/run/reboot-required")).exists()
    kernel_ver = run_capture(["uname", "-r"])

    # Snap refresh
    snap_list = run_capture(["snap", "refresh", "--list"])

    # TLS cert expiry for 3 hostnames
    tls_certs = {}
    for host in ["blog.carter2099.com", "chat.carter2099.com", "pi.carter2099.com"]:
        try:
            tls_out = run_capture(
                ["bash", "-c",
                 f"echo | openssl s_client -connect {host}:443 -servername {host} "
                 f"2>/dev/null | openssl x509 -noout -enddate"],
                timeout=15,
            )
            tls_certs[host] = tls_out.strip() if tls_out else "error"
        except Exception as e:
            tls_certs[host] = f"error: {e}"

    # User-unit inventory vs documented set
    documented_units = {
        "homelab-backup.service", "homelab-backup.timer",
        "digests-daily.service", "digests-daily.timer",
        "hyperliquid-sdk.service", "hyperliquid-sdk.timer",
        "homelab-steward.service", "homelab-steward.timer",
        "homelab-steward-notify.service",
        "opencode-go-proxy.service",
        "llm-proxy.service",
        "pi-web.service", "pi-web-sessiond.service",
        "dependabot-webhook.service",
        "homelab-backup-restore-drill.service", "homelab-backup-restore-drill.timer",
    }
    all_user_units = run_capture(
        ["systemctl", "--user", "list-units", "--all", "--no-legend"],
        env=env,
    )
    active_units = set()
    for line in all_user_units.splitlines():
        parts = line.split()
        if parts:
            name = parts[0]
            if name.endswith(".service") or name.endswith(".timer"):
                active_units.add(name)
    extra_units = active_units - documented_units
    missing_units = documented_units - active_units

    # Agent-state staleness (>14d flag)
    agent_state_stale = []
    agent_state_dir = HOME / "agent-state"
    if agent_state_dir.exists():
        cutoff = datetime.now() - timedelta(days=14)
        for f in agent_state_dir.iterdir():
            if f.is_file():
                mtime = datetime.fromtimestamp(f.stat().st_mtime)
                if mtime < cutoff:
                    agent_state_stale.append({"file": f.name, "mtime": mtime.isoformat()})

    # DDNS freshness
    ddns_fresh = "unknown"
    ddns_marker = HOME / "ddns" / "last-run"
    if ddns_marker.exists():
        ddns_fresh = run_capture(["cat", str(ddns_marker)])

    # Steward self-health: last runs.log entry
    steward_self = {"status": "ok", "last_entry": None, "warning": None}
    if RUNS_LOG.exists():
        try:
            lines = RUNS_LOG.read_text().strip().splitlines()
            if lines:
                last = json.loads(lines[-1])
                steward_self["last_entry"] = last
                last_ts = datetime.fromisoformat(last.get("ts", "2000-01-01T00:00:00"))
                if (datetime.now(timezone.utc) - last_ts) > timedelta(hours=36):
                    steward_self["warning"] = "Last steward run >36h ago"
                    steward_self["status"] = "warning"
        except Exception:
            steward_self["warning"] = "Could not parse runs.log"
            steward_self["status"] = "warning"
    else:
        steward_self["warning"] = "No previous steward runs"
        steward_self["status"] = "first_run"

    data = {
        "failed_units": {
            "user": failed_user.splitlines() if failed_user else [],
            "system": failed_system.splitlines() if failed_system else [],
        },
        "llm_stack": {"health": llm_health, "falling_back": falling_back},
        "backup": {"last_run": backup_ts},
        "k3s_nodes": nodes.splitlines() if nodes else [],
        "disk": {"df_root": disk_df, "docker_system_df": docker_df},
        "reboot": {"needed": reboot_needed, "kernel": kernel_ver},
        "snap": {"refresh_list": snap_list if snap_list and "All snaps up to date" not in snap_list else ""},
        "tls_certs": tls_certs,
        "units": {
            "active": sorted(active_units),
            "documented": sorted(documented_units),
            "extra": sorted(extra_units),
            "missing": sorted(missing_units),
        },
        "agent_state_stale": agent_state_stale,
        "ddns": ddns_fresh,
        "steward_self": steward_self,
    }
    write_json(run_dir / "04-heartbeat.json", data)
    print(f"[P4] done -> {run_dir / '04-heartbeat.json'}")
    return data


# ── P5: work queue ───────────────────────────────────────────────────


def _scan_md_files(directory, default_status="idea"):
    """Scan a directory for .md files, parse Status header + first heading + first paragraph."""
    results = []
    if not directory.exists():
        return results
    for f in sorted(directory.iterdir()):
        if not f.is_file() or not f.suffix == ".md":
            continue
        if f.name == "README.md":
            continue
        text = f.read_text()
        # Parse Status
        status_m = re.search(r"\*\*Status:\*\*\s*(.+)$", text, re.MULTILINE)
        # Normalize to first word, lowercase: "implementing (approved by …)" -> "implementing"
        status = (status_m.group(1).strip().split()[0].lower().rstrip(",")
                  if status_m else default_status)
        # Idea backlink (plans may declare one): **Idea:** `~/ideas/foo.md`
        idea_m = re.search(r"\*\*Idea:\*\*\s*`?([^`\s]+)", text)
        idea_link = idea_m.group(1) if idea_m else None
        # Parse Priority
        prio_m = re.search(r"\*\*Priority:\*\*\s*(\d+)$", text, re.MULTILINE)
        priority = int(prio_m.group(1)) if prio_m else 99
        # Parse Approved date
        approved_m = re.search(r"\*\*Approved:\*\*\s*(\S+)$", text, re.MULTILINE)
        approved = approved_m.group(1).strip() if approved_m else None
        # Parse urgent
        urgent_m = re.search(r"\*\*urgent:\*\*\s*(true|false)", text, re.MULTILINE)
        urgent = urgent_m.group(1).strip().lower() == "true" if urgent_m else False
        # Parse deploy
        deploy_m = re.search(r"\*\*deploy:\*\*\s*(true|false)", text, re.MULTILINE)
        deploy = deploy_m.group(1).strip().lower() == "true" if deploy_m else False
        # First heading
        heading_m = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
        heading = heading_m.group(1).strip() if heading_m else f.stem
        # First paragraph (after frontmatter/heading, non-empty, <=160 chars)
        # Strip YAML frontmatter if present
        body = text
        if body.startswith("---"):
            end = body.find("---", 3)
            if end != -1:
                body = body[end + 3:]
        # Skip the first heading line
        lines = body.splitlines()
        para = ""
        in_para = False
        for line in lines:
            stripped = line.strip()
            if not in_para and stripped and not stripped.startswith("#"):
                in_para = True
            if in_para:
                if not stripped:
                    break
                para += stripped + " "
        para = para.strip()[:160]
        mtime = datetime.fromtimestamp(f.stat().st_mtime)
        results.append({
            "file": f.name,
            "stem": f.stem,
            "status": status,
            "idea_link": idea_link,
            "priority": priority,
            "approved": approved,
            "urgent": urgent,
            "deploy": deploy,
            "heading": heading,
            "summary": para,
            "mtime": mtime.isoformat(),
            "age_days": (datetime.now() - mtime).days,
        })
    return results


def phase_5_work_queue(run_dir):
    """Phase 5: scan ideas/plans, consistency checks, pick executor candidate."""
    print("[P5] work queue scan")

    ideas = _scan_md_files(IDEAS_DIR, default_status="idea")
    plans = _scan_md_files(PLANS_DIR, default_status="draft")

    # Also scan done subdirectories
    ideas_done = _scan_md_files(IDEAS_DIR / "done", default_status="done")
    plans_done = _scan_md_files(PLANS_DIR / "done", default_status="done")

    # Buckets
    ideas_outstanding = [i for i in ideas if i["status"] not in ("done", "scrapped")]
    plans_draft = [p for p in plans if p["status"] == "draft"]
    plans_approved = [p for p in plans if p["status"] == "approved"]
    plans_implementing = [p for p in plans if p["status"] == "implementing"]
    plans_done_this_week = [
        p for p in plans_done
        if (datetime.now() - datetime.fromisoformat(p["mtime"])).days <= 7
    ]

    # Consistency checks (linkage via a plan's **Idea:** backlink, not filename)
    inconsistencies = []
    for idea in ideas_outstanding:
        # An idea still marked 'idea' while a plan links to it -> should be 'planned'
        linking = [p for p in plans
                   if p.get("idea_link") and idea["file"] in p["idea_link"]]
        if linking and idea["status"] == "idea":
            inconsistencies.append({
                "type": "idea_not_updated",
                "idea": idea["file"],
                "detail": f"Idea status is 'idea' but plan exists: {linking[0]['file']} (set idea to 'planned')",
            })

    for plan in plans_done:
        # Only flag plans that DECLARE an idea link; standalone plans are fine
        if plan.get("idea_link"):
            matching_idea = [i for i in ideas_done if i["file"] in plan["idea_link"]]
            if not matching_idea:
                inconsistencies.append({
                    "type": "plan_done_idea_not",
                    "plan": plan["file"],
                    "detail": f"Plan done but linked idea ({plan['idea_link']}) not in ideas/done/",
                })

    for plan in plans_implementing:
        lock_path = PLANS_DIR / ".steward-lock"
        if not lock_path.exists():
            age = (datetime.now() - datetime.fromisoformat(plan["mtime"])).days
            if age > 2:
                inconsistencies.append({
                    "type": "implementing_no_lock",
                    "plan": plan["file"],
                    "detail": f"Status implementing but no lock file exists; stale for {age} days",
                })

    # Pick executor candidate
    candidate = None
    monthly_used = 0
    if RUNS_LOG.exists():
        this_month = datetime.now().strftime("%Y-%m")
        for line in RUNS_LOG.read_text().strip().splitlines():
            try:
                entry = json.loads(line)
                if entry.get("executor") and entry.get("ts", "").startswith(this_month):
                    monthly_used += 1
            except json.JSONDecodeError:
                pass

    eligible = sorted(plans_approved, key=lambda p: (p["priority"], p["approved"] or "9999"))
    if eligible:
        candidate = eligible[0]

    data = {
        "ideas": {
            "outstanding": ideas_outstanding,
            "total_outstanding": len(ideas_outstanding),
        },
        "plans": {
            "draft": plans_draft,
            "approved": plans_approved,
            "implementing": plans_implementing,
            "done_this_week": plans_done_this_week,
        },
        "inconsistencies": inconsistencies,
        "executor_candidate": candidate,
        "executor_monthly_used": monthly_used,
        "executor_monthly_cap": EXECUTOR_MONTHLY_CAP,
    }
    write_json(run_dir / "05-queue.json", data)
    print(f"[P5] done -> {run_dir / '05-queue.json'}")
    print(f"  ideas outstanding: {len(ideas_outstanding)}")
    print(f"  plans: {len(plans_draft)} draft, {len(plans_approved)} approved, "
          f"{len(plans_implementing)} implementing, {len(plans_done_this_week)} done this week")
    print(f"  inconsistencies: {len(inconsistencies)}")
    if candidate:
        print(f"  executor candidate: {candidate['file']} (priority {candidate['priority']})")
    return data


# ── P6: executor ─────────────────────────────────────────────────────


def phase_6_executor(run_dir, setup_data, dry_run=False):
    """Phase 6: execute one approved plan via pi agent + post-impl review."""
    print("[P6] executor")
    queue_path = run_dir / "05-queue.json"
    if not queue_path.exists():
        print("  skipped — no queue data")
        data = {"executed": False, "reason": "no_queue_data"}
        write_json(run_dir / "06-executor.json", data)
        return data

    queue = read_json(queue_path)
    candidate = queue.get("executor_candidate")
    budget = setup_data.get("budget", {})

    # Guard checks
    if dry_run:
        print("  DRY RUN — skipping executor")
        data = {"executed": False, "reason": "dry_run"}
        write_json(run_dir / "06-executor.json", data)
        return data

    if not candidate:
        print("  skipped — no approved plan")
        data = {"executed": False, "reason": "no_approved_plan"}
        write_json(run_dir / "06-executor.json", data)
        return data

    if budget.get("guard_verdict") in ("defer_all", "proxy_unreachable"):
        print(f"  skipped — budget guard: {budget.get('guard_verdict')}")
        data = {"executed": False, "reason": f"budget_{budget.get('guard_verdict')}"}
        write_json(run_dir / "06-executor.json", data)
        return data

    if budget.get("guard_verdict") == "skip_agents" and not candidate.get("urgent"):
        print("  skipped — budget guard: skip_agents (plan not urgent)")
        data = {"executed": False, "reason": "budget_skip_agents"}
        write_json(run_dir / "06-executor.json", data)
        return data

    if setup_data.get("dependabot", {}).get("in_flight"):
        print("  skipped — dependabot in flight")
        data = {"executed": False, "reason": "dependabot_in_flight"}
        write_json(run_dir / "06-executor.json", data)
        return data

    monthly_used = queue.get("executor_monthly_used", 0)
    if monthly_used >= EXECUTOR_MONTHLY_CAP:
        print(f"  skipped — monthly cap reached ({monthly_used}/{EXECUTOR_MONTHLY_CAP})")
        data = {"executed": False, "reason": "monthly_cap"}
        write_json(run_dir / "06-executor.json", data)
        return data

    # Lock file check
    lock_path = PLANS_DIR / ".steward-lock"
    if lock_path.exists():
        lock_content = lock_path.read_text().strip()
        lock_age = (datetime.now() - datetime.fromtimestamp(lock_path.stat().st_mtime)).days
        if lock_age < 2:
            print(f"  skipped — lock exists ({lock_content}), {lock_age}d old")
            data = {"executed": False, "reason": f"lock_exists: {lock_content}"}
            write_json(run_dir / "06-executor.json", data)
            return data

    # Proceed with execution
    plan_file = PLANS_DIR / candidate["file"]
    if not plan_file.exists():
        print(f"  error — plan file not found: {plan_file}")
        data = {"executed": False, "reason": "plan_file_missing"}
        write_json(run_dir / "06-executor.json", data)
        return data

    plan_content = plan_file.read_text()

    # Write lock
    lock_path.write_text(f"{candidate['file']} {datetime.now().isoformat()}")

    # Update plan status to implementing
    new_plan = re.sub(r"\*\*Status:\*\*\s*approved", "**Status:** implementing", plan_content)
    plan_file.write_text(new_plan)

    # Determine execution mode
    is_preview = EXECUTOR_MODE == "preview"

    # Build prompt
    context_contract = f"""
You are executing a homelab plan unattended. Read the plan below carefully.

CRITICAL RULES:
- Work in ~/dev/<repo> clones, NEVER in prod deploy dirs (~/blog/, ~/delta_neutral/, etc.)
- Run the repo's test suite before committing
- Commit + push to main branch
- Deploy via release.sh ONLY if the plan says `deploy: true` (check the plan metadata)
- Use omp's task tool to fan out parallel implementation work
- End your response with a fenced ```json packet:
  {{"status": "success"|"partial"|"failed",
    "summary": "...",
    "commits": ["hash message"],
    "evidence": ["check1: result", ...],
    "acceptance_passed": true|false}}
"""
    if is_preview:
        context_contract += """
PREVIEW MODE: Confine all work to a fresh git worktree under /tmp/steward-preview/.
Do NOT push. Do NOT deploy. Collect `git diff` output as evidence.
"""
    else:
        context_contract += """
LIVE MODE: Commit and push to main. Deploy if deploy:true. Full execution.
"""

    full_prompt = context_contract + "\n\n--- PLAN ---\n\n" + plan_content

    extra_args = []
    try:
        raw_text, stats, packet, raw_ndjson = _call_omp_p_json(
        )
    except Exception as e:
        print(f"  EXECUTOR FAILED: {e}")
        # Restore plan status
        plan_file.write_text(plan_content)
        lock_path.unlink(missing_ok=True)
        data = {"executed": False, "reason": f"executor_error: {e}"}
        write_json(run_dir / "06-executor.json", data)
        return data

    # Save full NDJSON stream (tool-call audit trail)
    (run_dir / "06-executor.ndjson").write_text(raw_ndjson)

    executor_packet = packet
    exec_status = executor_packet.get("status", "unknown")
    commits = executor_packet.get("commits", [])

    # Post-implementation review (judge)
    print("  post-implementation review...")
    review_prompt = f"""
You are a skeptical reviewer. A plan was just executed by an automated agent.

PLAN:
{plan_content[:3000]}

EXECUTOR RESULT PACKET:
{json.dumps(executor_packet, indent=2)}

{'GIT DIFF/COMMITS:' + json.dumps(commits, indent=2) if commits else 'No commits listed.'}

Verify each acceptance criterion in the plan against the cited evidence.
Return a fenced ```json packet:
{{"verdict": "pass"|"fail",
 "findings": [{{"criterion": "...", "met": true|false, "evidence_cited": "...", "your_assessment": "..."}}]}}
"""
    try:
        review_text = _call_omp_p(review_prompt, timeout=300)
        review_packet = _extract_json(review_text, "review packet")
    except Exception as e:
        review_packet = {"verdict": "fail", "findings": [],
                         "review_error": str(e), "raw": review_text if 'review_text' in dir() else ""}

    # Deterministic probes
    probes = {}
    repo_match = re.search(r"~/dev/([a-zA-Z0-9_-]+)", plan_content)
    if repo_match:
        repo_name = repo_match.group(1)
        repo_path = HOME / "dev" / repo_name
        if repo_path.exists():
            probes["git_log"] = run_capture(
                ["git", "-C", str(repo_path), "log", "--oneline", "-3"])
            probes["git_status"] = run_capture(
                ["git", "-C", str(repo_path), "status", "-sb"])

    # Endpoint re-check
    ep_checks = {}
    for name, url in ENDPOINTS.items():
        code = run_capture(["curl", "-so", "/dev/null", "-w", "%{http_code}", url])
        ep_checks[name] = code
    probes["endpoints"] = ep_checks

    review_pass = review_packet.get("verdict") == "pass"
    probes_ok = all(code.startswith("2") or code.startswith("3") for code in ep_checks.values())

    if review_pass and probes_ok and exec_status == "success":
        # Mark done
        done_dir = PLANS_DIR / "done"
        done_dir.mkdir(exist_ok=True)
        done_plan = done_dir / candidate["file"]
        done_content = new_plan
        done_content = re.sub(
            r"\*\*Status:\*\*\s*implementing",
            f"**Status:** done  \n**Completed:** {datetime.now().strftime('%Y-%m-%d')}",
            done_content,
        )
        done_plan.write_text(done_content)
        plan_file.unlink()

        # Move the plan's linked idea (via its **Idea:** backlink, if declared)
        idea_link = candidate.get("idea_link") or ""
        idea_name = idea_link.rstrip("/").split("/")[-1] if idea_link else ""
        idea_file = IDEAS_DIR / idea_name if idea_name else None
        if idea_file and idea_file.exists():
            ideas_done_dir = IDEAS_DIR / "done"
            ideas_done_dir.mkdir(exist_ok=True)
            idea_content = idea_file.read_text()
            idea_content = re.sub(r"\*\*Status:\*\*\s*\S+", "**Status:** done", idea_content)
            (ideas_done_dir / idea_name).write_text(idea_content)
            idea_file.unlink()

        lock_path.unlink(missing_ok=True)
        final_status = "done"
    else:
        # Leave implementing, remove lock
        lock_path.unlink(missing_ok=True)
        final_status = "failed_review" if not review_pass else "failed_probes"

    data = {
        "executed": True,
        "plan": candidate["file"],
        "mode": EXECUTOR_MODE,
        "executor_packet": executor_packet,
        "review_packet": review_packet,
        "probes": probes,
        "status": final_status,
        "stats": stats,
    }
    write_json(run_dir / "06-executor.json", data)
    print(f"[P6] done -> {run_dir / '06-executor.json'} (status: {final_status})")
    return data


# ── P7: audit ────────────────────────────────────────────────────────


def _audit_collector_1_agents_md():
    """Collector: AGENTS.md truth-check evidence."""
    agents_path = HOME / "AGENTS.md"
    sha = hashlib.sha256(agents_path.read_bytes()).hexdigest() if agents_path.exists() else "missing"
    ip_addr = run_capture(["ip", "-4", "addr", "show", "enp3s0f0"])
    k_nodes = run_capture([K3S, "kubectl", "get", "nodes"], env=user_env())
    docker_ps = run_capture(["docker", "ps", "--format", "{{.Names}}\t{{.Image}}\t{{.Ports}}\t{{.Status}}"])
    ufw_rules = run_capture(["sudo", "grep", "-E", "cni0|flannel", "/etc/ufw/user.rules"])
    user_timers = run_capture(["systemctl", "--user", "list-timers", "--all"], env=user_env())
    return {
        "agents_md_sha256": sha,
        "ip_addr_enp3s0f0": ip_addr,
        "k_nodes": k_nodes,
        "docker_ps": docker_ps,
        "ufw_cni_flannel": ufw_rules,
        "user_timers": user_timers,
    }


def _audit_collector_2_versions():
    """Collector: current version strings."""
    return {
        "k3s": run_capture([K3S, "--version"]),
        "go": run_capture(["go", "version"]),
        "node": run_capture(["node", "-v"]),
        "rbenv": run_capture(["rbenv", "versions"]),
        "nvim": run_capture(["nvim", "--version"]),
        "npm_global": run_capture(["npm", "ls", "-g", "pi", "@jmfederico/pi-web"]),
        "docker_images": run_capture(
            ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}\t{{.CreatedAt}}"]),
        "llama_cpp": "not-collected — worker verifies read-only via `ssh gamingrig`",
    }


def _audit_collector_3_digest_quality():
    """Collector: digest quality metrics over trailing 7 days."""
    evidence = {"topics": {}, "placeholder_leakage": 0, "fallback_count": 0}
    topics = ["ai-tech", "agentic-platform", "ai-hardware", "gaming-digest", "world-digest"]
    now = datetime.now()
    for topic in topics:
        topic_dir = HOME / "digests" / topic
        tev = {"exists": topic_dir.exists(), "runs": []}
        if topic_dir.exists():
            for d in sorted(topic_dir.iterdir(), reverse=True):
                if not d.is_dir():
                    continue
                try:
                    d_date = datetime.strptime(d.name, "%Y-%m-%d")
                except ValueError:
                    continue
                if (now - d_date).days > 7:
                    continue
                artifacts = sorted([f.name for f in d.iterdir() if f.is_file()])
                html_files = [f for f in artifacts if f.endswith(".html")]
                placeholder_count = 0
                for hf in html_files:
                    html = (d / hf).read_text()
                    placeholder_count += len(re.findall(r"\{\{[A-Z_]+\}\}", html))
                tev["runs"].append({
                    "date": d.name,
                    "artifacts": artifacts,
                    "placeholder_leaks": placeholder_count,
                })
                evidence["placeholder_leakage"] += placeholder_count
        evidence["topics"][topic] = tev

    # llm-proxy fallback count in digest window
    fallback_log = run_capture(
        ["journalctl", "--user", "-u", "llm-proxy",
         "--since", "7 days ago", "--no-pager", "-q"],
        env=user_env(),
    )
    evidence["fallback_count"] = fallback_log.count("X-Fallback: true")

    # Per-topic durations from .digests.log
    digests_log = HOME / "digests" / ".digests.log"
    if digests_log.exists():
        evidence["durations"] = digests_log.read_text()[-2000:]
    return evidence


def _audit_collector_4_security():
    """Collector: security posture evidence."""
    # Read CF token for RDAP/API calls (collector only, never the agent)
    cf_token_path = HOME / ".config" / "cloudflare" / "api-token"
    cf_token = cf_token_path.read_text().strip() if cf_token_path.exists() else ""
    cf_account_id_path = HOME / ".config" / "cloudflare" / "account-id"
    cf_account_id = cf_account_id_path.read_text().strip() if cf_account_id_path.exists() else ""
    cf_tunnel_id_path = HOME / ".config" / "cloudflare" / "homelab-tunnel-id"
    cf_tunnel_id = cf_tunnel_id_path.read_text().strip() if cf_tunnel_id_path.exists() else ""

    # RDAP domain expiry
    rdap_expiry = ""
    try:
        req = urllib.request.Request(
            "https://rdap.verisign.com/com/v1/domain/carter2099.com",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            rdap_data = json.loads(resp.read().decode())
            for event in rdap_data.get("events", []):
                if event.get("eventAction") == "expiration":
                    rdap_expiry = event.get("eventDate", "")
    except Exception as e:
        rdap_expiry = f"error: {e}"

    # CF tunnel ingress via API
    cf_tunnel_ingress = ""
    if cf_token and cf_account_id and cf_tunnel_id:
        try:
            req = urllib.request.Request(
                f"https://api.cloudflare.com/client/v4/accounts/{cf_account_id}/cfd_tunnel/{cf_tunnel_id}/configurations",
                headers={"Authorization": f"Bearer {cf_token}", "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                cf_tunnel_ingress = resp.read().decode()[:3000]
        except Exception as e:
            cf_tunnel_ingress = f"error: {e}"

    return {
        "listeners": run_capture(["ss", "-tlnp"]),
        "ufw_status": run_capture(["sudo", "ufw", "status"]),
        "unattended_upgrades": run_capture(["systemctl", "is-active", "unattended-upgrades"]),
        "rdap_expiry": rdap_expiry,
        "cf_tunnel_ingress": cf_tunnel_ingress[:3000],
        "ssh_failures": run_capture(
            ["bash", "-c",
             "journalctl -u ssh --since '24 hours ago' 2>/dev/null | grep -c 'Failed password' || echo 0"]),
    }


def _audit_collector_5_config_drift():
    """Collector: config vs tracked drift."""
    k3s_diff = run_capture(
        ["diff", "/etc/rancher/k3s/config.yaml", str(HOME / "k3s" / "config.yaml")])
    dotfiles_status = run_capture(
        ["/usr/bin/git", "--git-dir", str(HOME / ".dotfiles-homelab"),
         "--work-tree", str(HOME), "status", "--short"])
    notes_status = run_capture(["git", "-C", str(HOME / "notes"), "status", "--short"])

    deploy_repos = {}
    for name, path in [
        ("blog", HOME / "blog" / "blog"),
        ("delta_neutral", HOME / "delta_neutral" / "delta_neutral"),
        ("homelab-backup", HOME / "homelab-backup"),
    ]:
        if path.exists():
            run_capture(["git", "-C", str(path), "fetch"], timeout=30)
            deploy_repos[name] = run_capture(["git", "-C", str(path), "status", "-sb"])

    return {
        "k3s_config_diff": k3s_diff,
        "dotfiles_status": dotfiles_status,
        "notes_status": notes_status,
        "deploy_repos": deploy_repos,
    }


def _audit_collector_6_notes_resources():
    """Collector: resource trends + OOM/exit-255 hunt."""
    oom_hunt = run_capture(
        ["journalctl", "--since", "24 hours ago", "--no-pager", "-q"])
    oom_count = oom_hunt.lower().count("out of memory") if oom_hunt else 0
    exit_255 = run_capture(
        ["docker", "ps", "-a", "--filter", "status=exited",
         "--format", "{{.Names}}\t{{.Status}}\t{{.Image}}"])

    # R2 size
    r2_list = run_capture(
        [str(HOME / "homelab-backup" / "homelab-backup"), "list"])

    return {
        "disk_df": run_capture(["df", "-h", "/"]),
        "docker_system_df": run_capture(["docker", "system", "df"]),
        "oom_count": oom_count,
        "exit_255_containers": exit_255,
        "r2_list_tail": "\n".join(r2_list.splitlines()[-20:]) if r2_list else "",
        "journal_size": run_capture(
            ["journalctl", "--disk-usage"]),
    }


def _audit_collector_7_agent_fleet():
    """Collector: other unattended agents' recent runs."""
    env = user_env()
    hyperliquid_log = run_capture(
        ["journalctl", "--user", "-u", "hyperliquid-sdk",
         "--since", "4 days ago", "--no-pager", "-n", "100"],
        env=env,
    )
    dependabot_errors = run_capture(
        ["journalctl", "--user", "-u", "dependabot-webhook",
         "--since", "24 hours ago", "-p", "err", "--no-pager"],
        env=env,
    )
    # Steward's own yesterday executor outcome
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    yesterday_exec = None
    yesterday_dir = RUN_DIR_BASE / yesterday
    exec_path = yesterday_dir / "06-executor.json"
    if exec_path.exists():
        try:
            yesterday_exec = read_json(exec_path)
        except Exception:
            pass

    return {
        "hyperliquid_sdk_recent": hyperliquid_log[:3000],
        "dependabot_errors": dependabot_errors[:2000],
        "steward_yesterday_executor": yesterday_exec,
    }


AUDIT_SECTIONS = [
    {
        "name": "agents-md-truth",
        "collector": _audit_collector_1_agents_md,
        "artifact": "07-audit-1-agents-md.json",
        "timeout": 600,
        "guidance": (
            "Truth-check /home/carter/AGENTS.md against the live host. READ the file first. "
            "Verify (1) pointer targets still resolve (paths, commands it cites) and (2) structural/"
            "semantic facts: IP roles (.100 DHCP/default, .92 k3s+blog/delta_neutral, .102 tbitt/stickies), "
            "enp3s0f0 as primary + wlp6s0 down, flannel-iface=enp3s0f0, the two-pattern k3s deployment model, "
            "service+timer names and schedules, ufw cni0/flannel.1 rules, sole docker daemon at /var/lib/docker, "
            "documented ports. Do NOT re-add intentionally-removed version pins. "
            "For every DRIFT propose an exact OLD_TEXT -> NEW_TEXT edit. Prefer UNVERIFIABLE over guessing — "
            "never run anything destructive."
        ),
    },
    {
        "name": "version-currency",
        "collector": _audit_collector_2_versions,
        "artifact": "07-audit-2-versions.json",
        "timeout": 600,
        "guidance": (
            "Compare current versions (in evidence) against latest upstream stable: k3s, Go, Node, Ruby (rbenv), "
            "neovim, pi + @jmfederico/pi-web (npm), docker images (searxng, freshrss, traefik, open-webui), "
            "llama.cpp on the gaming rig (verify read-only via `ssh gamingrig`). "
            "Report per component: current / latest / status (current | behind | behind-major). "
            "Checking upstream (GitHub releases, npm registry, go.dev) is allowed; mutations are not."
        ),
    },
    {
        "name": "digest-quality",
        "collector": _audit_collector_3_digest_quality,
        "artifact": "07-audit-3-digests.json",
        "timeout": 600,
        "guidance": (
            "Judge the quality of the 5 daily digests over the trailing 7 days using the collector metrics plus "
            "your own read of ~/digests/<topic>/<date>/ artifacts: run completeness per topic/day, story freshness, "
            "cross-day duplication vs summary.md files, template placeholder leakage, source diversity "
            "(unique domains), stories-in-flight.json hygiene (7d cool / 14d prune enforced), duration trends, "
            "llm-proxy fallback in the digest window. Sample up to 3 links per digest with curl -sI (read-only)."
        ),
    },
    {
        "name": "security-posture",
        "collector": _audit_collector_4_security,
        "artifact": "07-audit-4-security.json",
        "timeout": 600,
        "guidance": (
            "Judge the security posture from the evidence: listening sockets vs the documented set "
            "(loopback-only: pi-web 8504, open-webui 48100, searxng 8080, llm-proxy 8081; ufw-gated: 8082; "
            "LAN: blog 33099, delta 43080), ufw ruleset intact (cni0/flannel.1/docker bridges), unattended-upgrades "
            "active, carter2099.com RDAP expiry (>30d out = ok), CF tunnel ingress vs expected hostnames "
            "(pi, chat, hooks, opencode.carter2099.com), SSH failed-password volume. Flag anything unexpected."
        ),
    },
    {
        "name": "config-doc-drift",
        "collector": _audit_collector_5_config_drift,
        "artifact": "07-audit-5-config.json",
        "timeout": 600,
        "guidance": (
            "Judge drift significance from the evidence: k3s live config vs tracked copy must be identical; "
            "dotfiles repo should be clean except files an interactive session is actively editing; notes repo "
            "should be clean; deploy dirs (blog, delta_neutral, homelab-backup) should match origin/main "
            "(commit-before-deploy rule). Distinguish real drift from in-flight session work — when unsure, "
            "mark ATTENTION with reasoning rather than DRIFT."
        ),
    },
    {
        "name": "notes-resources",
        "collector": _audit_collector_6_notes_resources,
        "artifact": "07-audit-6-resources.json",
        "timeout": 600,
        "guidance": (
            "Interpret the resource evidence: disk / usage and growth, docker system df (reclaimable), journal "
            "size, R2 backup archive growth, OOM kills, exited containers (the known intermittent exit-255 "
            "pattern — flag repeats on the same container). Only report ATTENTION when a trend is actionable "
            "(e.g. disk >80%, steady week-over-week growth, recurring OOM on one service)."
        ),
    },
    {
        "name": "agent-fleet-review",
        "collector": _audit_collector_7_agent_fleet,
        "artifact": "07-audit-7-fleet.json",
        "timeout": 600,
        "guidance": (
            "Review the other unattended agents' recent runs from the evidence: hyperliquid-sdk (Mon/Thu timer — "
            "did it fire? outcome? errors?), dependabot-webhook (jobs, failures), and the steward's own prior "
            "executor outcome (if any — did yesterday's executor work hold up?). Also read recent session files in "
            "~/.pi/agent/sessions-automated if you need outcomes the journal lacks. Flag failed or silently-"
            "skipped runs."
        ),
    },
]

# Verdicts that prove a section actually ran its worker+judge — safe to cache.
_REAL_VERDICTS = {"PASS", "DRIFT", "ATTENTION", "UNVERIFIABLE"}


def _run_audit_agent_pair(section, evidence, current_hash):
    """Worker + judge for one audit section. Returns the section result dict."""
    section_name = section["name"]
    worker_prompt = f"""
You are a homelab audit agent for section '{section_name}'.

SECTION GUIDANCE:
{section["guidance"]}

Rules:
- Ground every claim in the collected evidence or in live read-only checks you run
  yourself (cite specific file:line, command output, etc.)
- You have read tools and bash — use them to verify, never to mutate.
- Return a fenced ```json packet:
{{"verdict": "PASS"|"DRIFT"|"ATTENTION"|"UNVERIFIABLE",
 "findings": [{{"claim": "...", "evidence": "...", "fix": "..."}}]}}

{_date_context()}

COLLECTED EVIDENCE:
{json.dumps(evidence, indent=2, default=str)[:8000]}
"""
    try:
        worker_text = _call_omp_p(worker_prompt, timeout=section["timeout"])
        worker_packet = _extract_json(worker_text, f"worker-{section_name}")
    except Exception as e:
        return {
            "name": section_name,
            "verdict": "worker-failed",
            "error": str(e),
            "evidence_hash": current_hash,
            "judge_rejected": [],
            "confirmed_findings": [],
        }

    judge_prompt = f"""
You are a skeptical judge reviewing a homelab audit agent's findings. Independently
re-verify each finding against ground truth — run the same read-only checks yourself
where needed. Keep only findings you can confirm.

SECTION: {section_name}

COLLECTED EVIDENCE:
{json.dumps(evidence, indent=2, default=str)[:6000]}

WORKER VERDICT + FINDINGS:
{json.dumps(worker_packet, indent=2)}

Return a fenced ```json packet:
{{"confirmed": [{{"claim": "...", "evidence": "..."}}],
 "rejected": [{{"claim": "...", "reason": "..."}}]}}
"""
    try:
        judge_text = _call_omp_p(judge_prompt, timeout=section["timeout"])
        judge_packet = _extract_json(judge_text, f"judge-{section_name}")
    except Exception as e:
        judge_packet = {
            "confirmed": worker_packet.get("findings", []),
            "rejected": [],
            "judge_error": str(e),
        }

    confirmed = judge_packet.get("confirmed", [])
    rejected = judge_packet.get("rejected", [])
    return {
        "name": section_name,
        "verdict": worker_packet.get("verdict", "UNVERIFIABLE"),
        "evidence_hash": current_hash,
        "worker_findings": worker_packet.get("findings", []),
        "judge_confirmed": confirmed,
        "judge_rejected": rejected,
    }


def phase_7_audit(run_dir, setup_data, dry_run=False):
    """Phase 7: audit sections — collector -> delta gate -> parallel worker+judge."""
    print("[P7] audit")
    budget = setup_data.get("budget", {})
    guard = budget.get("guard_verdict", "full")
    prev_date_str = setup_data.get("prev_date", "")

    all_results = []
    to_fire = []

    for section in AUDIT_SECTIONS:
        section_name = section["name"]
        artifact_name = section["artifact"]
        print(f"  [{section_name}] collector...")

        try:
            evidence = section["collector"]()
        except Exception as e:
            print(f"    collector FAILED: {e}")
            result = {"name": section_name, "verdict": "collector-failed",
                      "error": str(e), "judge_rejected": [], "confirmed_findings": []}
            write_json(run_dir / artifact_name, result)
            all_results.append(result)
            continue

        write_json(run_dir / f"{artifact_name}.evidence.json", evidence)
        current_hash = _evidence_hash(evidence)

        # Delta gate: cache only when yesterday produced a REAL verdict on identical evidence
        prev_artifact = _load_prev_artifact(run_dir, prev_date_str, artifact_name)
        if prev_artifact:
            prev_hash = prev_artifact.get("evidence_hash")
            prev_verdict = str(prev_artifact.get("verdict", ""))
            base_verdict = prev_verdict.removeprefix("cached-")
            if prev_hash == current_hash and base_verdict in _REAL_VERDICTS:
                print(f"    delta-gate: unchanged -> cached-{base_verdict}")
                result = {
                    "name": section_name,
                    "verdict": f"cached-{base_verdict}",
                    "evidence_hash": current_hash,
                    "worker_findings": prev_artifact.get("worker_findings", []),
                    "judge_confirmed": prev_artifact.get("judge_confirmed", []),
                    "judge_rejected": [],
                }
                write_json(run_dir / artifact_name, result)
                all_results.append(result)
                continue

        if guard in ("skip_agents", "proxy_unreachable"):
            print(f"    budget: {guard} -> skipped")
            result = {"name": section_name, "verdict": "skipped-budget",
                      "evidence_hash": current_hash, "judge_rejected": [],
                      "confirmed_findings": []}
            write_json(run_dir / artifact_name, result)
            all_results.append(result)
            continue

        if guard == "anomaly_only" and section_name not in ("security-posture", "agent-fleet-review"):
            print("    budget: anomaly_only, non-critical section -> skipped")
            result = {"name": section_name, "verdict": "skipped-budget",
                      "evidence_hash": current_hash, "judge_rejected": [],
                      "confirmed_findings": []}
            write_json(run_dir / artifact_name, result)
            all_results.append(result)
            continue

        if dry_run:
            print("    dry-run: collector only")
            result = {"name": section_name, "verdict": "dry-run-collector-only",
                      "evidence_hash": current_hash, "judge_rejected": [],
                      "confirmed_findings": []}
            write_json(run_dir / artifact_name, result)
            all_results.append(result)
            continue

        to_fire.append((section, evidence, current_hash, artifact_name))

    # Fan out worker+judge pairs in parallel (cloud model, staggered via pool)
    if to_fire:
        print(f"  fanning out {len(to_fire)} sections (max_workers={MAX_WORKERS})")
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {
                pool.submit(_run_audit_agent_pair, section, evidence, chash): (section, artifact_name)
                for (section, evidence, chash, artifact_name) in to_fire
            }
            for fut in as_completed(futures):
                section, artifact_name = futures[fut]
                try:
                    result = fut.result()
                except Exception as e:
                    result = {"name": section["name"], "verdict": "worker-failed",
                              "error": str(e), "judge_rejected": [],
                              "confirmed_findings": []}
                write_json(run_dir / artifact_name, result)
                all_results.append(result)
                print(f"    {section['name']}: {result['verdict']}, "
                      f"confirmed={len(result.get('judge_confirmed', []))}, "
                      f"rejected={len(result.get('judge_rejected', []))}")

    # Master artifact in canonical section order
    order = {s["name"]: i for i, s in enumerate(AUDIT_SECTIONS)}
    all_results.sort(key=lambda r: order.get(r["name"], 99))
    master = {"sections": all_results, "guard_verdict": guard}
    write_json(run_dir / "07-audit.json", master)
    print(f"[P7] done -> {run_dir / '07-audit.json'}")
    return master


# ── P8: render + send ────────────────────────────────────────────────


def _badge(verdict):
    """Return an HTML status badge for an audit verdict."""
    badges = {
        "PASS": '<span style="color:#2e7d32; font-weight:700;">PASS</span>',
        "DRIFT": '<span style="color:#c62828; font-weight:700;">DRIFT</span>',
        "ATTENTION": '<span style="color:#f57f17; font-weight:700;">ATTENTION</span>',
        "UNVERIFIABLE": '<span style="color:#888; font-weight:700;">UNVERIFIABLE</span>',
        "cached-PASS": '<span style="color:#aaa; font-weight:400;">cached-PASS</span>',
        "collector-failed": '<span style="color:#c62828; font-weight:700;">COLLECTOR FAILED</span>',
        "worker-failed": '<span style="color:#c62828; font-weight:700;">WORKER FAILED</span>',
        "skipped-budget": '<span style="color:#888;">skipped (budget)</span>',
        "dry-run-collector-only": '<span style="color:#888;">collector-only (dry-run)</span>',
    }
    if verdict.startswith("cached-"):
        base = verdict.removeprefix("cached-")
        color = {"PASS": "#2e7d32", "DRIFT": "#c62828",
                 "ATTENTION": "#f57f17"}.get(base, "#888")
        return f'<span style="color:{color}; font-weight:400;">cached-{base}</span>'
    return badges.get(verdict, f'<span style="color:#888;">{verdict}</span>')


def _html_updates(applied_data):
    """Render update steps as HTML."""
    steps = applied_data.get("steps", [])
    if not steps:
        if applied_data.get("dry_run"):
            return '<p style="margin:0; color:#888; font-size:13px;">Dry run — no mutations applied.</p>'
        return '<p style="margin:0; color:#888; font-size:13px;">No update steps executed.</p>'

    lines = []
    for s in steps:
        name = s.get("step", "")
        status = s.get("status", "")
        if name == "apt_upgrade":
            n = s.get("upgraded_count", 0)
            lines.append(f'<p style="margin:0 0 4px; color:#555; font-size:13px;">'
                         f'apt upgrade: {n} packages</p>')
        elif name.startswith("auto_"):
            pkg = name.replace("auto_", "")
            if status == "ok":
                lines.append(f'<p style="margin:0 0 4px; color:#555; font-size:13px;">'
                             f'{pkg}: {s.get("pre_version","?")} -> {s.get("post_version","?")}</p>')
            elif status == "skipped":
                lines.append(f'<p style="margin:0 0 4px; color:#888; font-size:13px;">'
                             f'{pkg}: already current ({s.get("pre_version","?")})</p>')
            else:
                lines.append(f'<p style="margin:0 0 4px; color:#c62828; font-size:13px;">'
                             f'{pkg}: FAILED — {s.get("error","")}</p>')
        elif name.startswith("k3s_"):
            svc = name.replace("k3s_", "")
            lines.append(f'<p style="margin:0 0 4px; color:#555; font-size:13px;">'
                         f'{svc}: rollout {"OK" if status=="ok" else "FAILED"}</p>')
        elif name == "openwebui":
            if status == "bumped":
                lines.append(f'<p style="margin:0 0 4px; color:#555; font-size:13px;">'
                             f'open-webui: {s.get("current_tag")} -> {s.get("latest_tag")}</p>')
            elif status == "current":
                lines.append(f'<p style="margin:0 0 4px; color:#555; font-size:13px;">'
                             f'open-webui: current at {s.get("current_tag")}</p>')
            else:
                lines.append(f'<p style="margin:0 0 4px; color:#888; font-size:13px;">'
                             f'open-webui: {status}</p>')
    return "\n".join(lines) if lines else '<p style="margin:0; color:#888; font-size:13px;">No updates.</p>'


def _html_validation(validation_data):
    """Render validation checks as HTML."""
    checks = validation_data.get("checks", [])
    lines = []
    for c in checks:
        name = c.get("name", "")
        if name.startswith("endpoint_"):
            svc = name.replace("endpoint_", "")
            code = c.get("http_code", "?")
            ok = c.get("status") == "ok"
            icon = "OK" if ok else "FAIL"
            color = "#2e7d32" if ok else "#c62828"
            lines.append(f'<p style="margin:0 0 4px; color:{color}; font-size:13px;">'
                         f'{icon} {svc} — HTTP {code}</p>')
        elif name == "k3s_pods":
            bad = c.get("bad_pods", [])
            if bad:
                lines.append(f'<p style="margin:0 0 4px; color:#c62828; font-size:13px;">'
                             f'k3s: {len(bad)} pods not Running/Completed</p>')
            else:
                lines.append(f'<p style="margin:0 0 4px; color:#2e7d32; font-size:13px;">'
                             f'k3s pods: all healthy</p>')
        elif name == "llm_fallback":
            fb = c.get("fallback_active", False)
            lines.append(f'<p style="margin:0 0 4px; color:#{"f57f17" if fb else "2e7d32"}; font-size:13px;">'
                         f'LLM: {"CLOUD FALLBACK" if fb else "local"}</p>')
    return "\n".join(lines)


def _html_heartbeat(hb_data):
    """Render heartbeat block as HTML."""
    lines = []
    # Failed units
    uf = hb_data.get("failed_units", {})
    user_f = uf.get("user", [])
    sys_f = uf.get("system", [])
    total_f = len([x for x in user_f if x]) + len([x for x in sys_f if x])
    if total_f == 0:
        lines.append('<p style="margin:0; color:#2e7d32; font-size:13px;">All units healthy</p>')
    else:
        for u in user_f:
            if u.strip():
                lines.append(f'<p style="margin:0; color:#c62828; font-size:13px;">FAILED user: {u.strip()}</p>')
        for u in sys_f:
            if u.strip():
                lines.append(f'<p style="margin:0; color:#c62828; font-size:13px;">FAILED system: {u.strip()}</p>')

    # LLM
    fb = hb_data.get("llm_stack", {}).get("falling_back", False)
    lines.append(f'<p style="margin:0; color:#{"f57f17" if fb else "2e7d32"}; font-size:13px;">'
                 f'LLM: {"CLOUD FALLBACK" if fb else "local"}</p>')

    # Backup
    bt = hb_data.get("backup", {}).get("last_run", "")
    lines.append(f'<p style="margin:0; color:#555; font-size:13px;">Backup: {bt}</p>')

    # k3s
    nodes = hb_data.get("k3s_nodes", [])
    if nodes:
        lines.append(f'<p style="margin:0; color:#555; font-size:13px;">k3s: {nodes[0].strip() if nodes else "unknown"}</p>')

    # Disk
    disk = hb_data.get("disk", {})
    if disk.get("df_root"):
        parts = disk["df_root"].splitlines()[-1].split()
        if len(parts) >= 5:
            lines.append(f'<p style="margin:0; color:#555; font-size:13px;">Disk /: {parts[4]} used ({parts[2]}/{parts[1]})</p>')

    # Reboot
    rb = hb_data.get("reboot", {})
    if rb.get("needed"):
        lines.append(f'<p style="margin:0; color:#c62828; font-size:13px;">Reboot needed (kernel: {rb.get("kernel","?")})</p>')
    else:
        lines.append('<p style="margin:0; color:#2e7d32; font-size:13px;">No reboot needed</p>')

    # TLS
    tls = hb_data.get("tls_certs", {})
    for host, expiry in tls.items():
        lines.append(f'<p style="margin:0; color:#555; font-size:12px;">TLS {host}: {expiry}</p>')

    # Extra units
    extra = hb_data.get("units", {}).get("extra", [])
    if extra:
        lines.append(f'<p style="margin:0; color:#f57f17; font-size:13px;">Extra units: {", ".join(extra)}</p>')

    # Agent state stale
    stale = hb_data.get("agent_state_stale", [])
    if stale:
        names = [s["file"] for s in stale]
        lines.append(f'<p style="margin:0; color:#f57f17; font-size:13px;">Stale agent-state: {", ".join(names)}</p>')

    return "\n".join(lines)


def _html_audit(audit_data):
    """Render audit sections as HTML."""
    sections = audit_data.get("sections", [])
    if not sections:
        return '<p style="margin:0; color:#888; font-size:13px;">No audit results.</p>'

    lines = []
    for sec in sections:
        name = sec.get("name", "unknown")
        verdict = sec.get("verdict", "UNKNOWN")
        badge = _badge(verdict)
        lines.append(f'<p style="margin:0 0 2px; font-size:13px;"><strong>{name}:</strong> {badge}</p>')
        confirmed = sec.get("judge_confirmed", []) or sec.get("confirmed_findings", [])
        for finding in confirmed:
            claim = finding.get("claim", finding.get("evidence", ""))
            lines.append(f'<p style="margin:0 0 2px 16px; color:#555; font-size:12px;">- {claim[:200]}</p>')
        rejected = sec.get("judge_rejected", [])
        for r in rejected:
            claim = r.get("claim", "")
            reason = r.get("reason", "")
            lines.append(
                f'<p style="margin:0 0 2px 16px; color:#888; font-size:12px; text-decoration:line-through;">'
                f'- {claim[:120]}</p>'
                f'<p style="margin:0 0 4px 16px; color:#888; font-size:11px;">Judge rejected: {reason[:200]}</p>'
            )
    return "\n".join(lines)


def _html_queue(queue_data):
    """Render work queue as HTML."""
    lines = []
    ideas = queue_data.get("ideas", {})
    plans = queue_data.get("plans", {})
    inconsistencies = queue_data.get("inconsistencies", [])

    lines.append(f'<p style="margin:0; color:#555; font-size:13px;">'
                 f'Ideas outstanding: {ideas.get("total_outstanding", 0)}</p>')
    for idea in ideas.get("outstanding", [])[:10]:
        lines.append(f'<p style="margin:0 0 2px 12px; color:#888; font-size:12px;">'
                     f'- {idea["file"]} ({idea["age_days"]}d): {idea["heading"][:80]}</p>')

    for plan in plans.get("draft", []):
        lines.append(f'<p style="margin:0 0 2px 12px; color:#1565c0; font-size:12px;">'
                     f'DRAFT: {plan["file"]} — {plan["heading"][:80]}</p>')

    for plan in plans.get("approved", []):
        lines.append(f'<p style="margin:0 0 2px 12px; color:#2e7d32; font-size:12px;">'
                     f'APPROVED: {plan["file"]} (priority {plan["priority"]})</p>')

    for plan in plans.get("implementing", []):
        lines.append(f'<p style="margin:0 0 2px 12px; color:#f57f17; font-size:12px;">'
                     f'IMPLEMENTING: {plan["file"]} ({plan["age_days"]}d)</p>')

    for plan in plans.get("done_this_week", []):
        lines.append(f'<p style="margin:0 0 2px 12px; color:#888; font-size:12px;">'
                     f'Done: {plan["file"]}</p>')

    if inconsistencies:
        lines.append('<p style="margin:8px 0 0; color:#c62828; font-size:13px;"><strong>Inconsistencies:</strong></p>')
        for inc in inconsistencies:
            lines.append(f'<p style="margin:0 0 2px 12px; color:#c62828; font-size:12px;">'
                         f'- {inc["type"]}: {inc["detail"][:200]}</p>')

    candidate = queue_data.get("executor_candidate")
    cap = queue_data.get("executor_monthly_cap", 4)
    used = queue_data.get("executor_monthly_used", 0)
    if candidate:
        lines.append(f'<p style="margin:8px 0 0; color:#00838f; font-size:13px;">'
                     f'Next executor candidate: {candidate["file"]} '
                     f'(monthly {used}/{cap})</p>')

    return "\n".join(lines) if lines else '<p style="margin:0; color:#888; font-size:13px;">Queue empty.</p>'


def _html_executor(exec_data):
    """Render executor result as HTML."""
    if not exec_data.get("executed"):
        reason = exec_data.get("reason", "no plan")
        return f'<p style="margin:0; color:#888; font-size:13px;">Idle — {reason}</p>'

    status = exec_data.get("status", "unknown")
    plan = exec_data.get("plan", "?")
    packet = exec_data.get("executor_packet", {})
    review = exec_data.get("review_packet", {})

    lines = []
    color = "#2e7d32" if status == "done" else "#c62828"
    lines.append(f'<p style="margin:0 0 4px; color:{color}; font-size:13px;">'
                 f'Plan: {plan} — {status}</p>')
    lines.append(f'<p style="margin:0 0 4px; color:#555; font-size:13px;">'
                 f'Summary: {packet.get("summary", "N/A")[:300]}</p>')

    commits = packet.get("commits", [])
    for c in commits[:5]:
        lines.append(f'<p style="margin:0 0 2px 12px; color:#555; font-size:12px;">- {c[:120]}</p>')

    if review:
        rev_verdict = review.get("verdict", "?")
        lines.append(f'<p style="margin:4px 0 0; color:#{"2e7d32" if rev_verdict=="pass" else "#c62828"}; font-size:13px;">'
                     f'Review: {rev_verdict}</p>')

    return "\n".join(lines)


def _html_budget(budget_data):
    """Render budget guard summary."""
    lines = []
    lines.append(f'<p style="margin:0; color:#555; font-size:13px;">'
                 f'Guard: {budget_data.get("guard_verdict", "?")} '
                 f'(rolling={budget_data.get("rolling_pct",0)}%, '
                 f'weekly={budget_data.get("weekly_pct",0)}%, '
                 f'monthly={budget_data.get("monthly_pct",0)}%)</p>')
    for acct in budget_data.get("accounts", []):
        lines.append(f'<p style="margin:0 0 2px 12px; color:#888; font-size:12px;">'
                     f'{acct.get("tier","?")}: rolling={acct.get("rolling_pct",0)}% '
                     f'weekly={acct.get("weekly_pct",0)}% monthly={acct.get("monthly_pct",0)}%</p>')
    return "\n".join(lines)


def phase_8_render_send(run_dir, setup_data, dry_run=False):
    """Phase 8: render HTML from all artifacts and send email."""
    print("[P8] render + send")

    date_str = setup_data["date"]
    budget = setup_data.get("budget", {})

    # Load all phase data
    applied = read_json(run_dir / "01-applied.json") if (run_dir / "01-applied.json").exists() else {"steps": []}
    validation = read_json(run_dir / "02-validation.json") if (run_dir / "02-validation.json").exists() else {"checks": []}
    troubleshoot = read_json(run_dir / "03-troubleshoot.json") if (run_dir / "03-troubleshoot.json").exists() else None
    heartbeat = read_json(run_dir / "04-heartbeat.json") if (run_dir / "04-heartbeat.json").exists() else {}
    queue = read_json(run_dir / "05-queue.json") if (run_dir / "05-queue.json").exists() else {}
    executor = read_json(run_dir / "06-executor.json") if (run_dir / "06-executor.json").exists() else {}
    audit = read_json(run_dir / "07-audit.json") if (run_dir / "07-audit.json").exists() else {"sections": []}

    # Phase failures anywhere in the pipeline (each artifact records phase_failed)
    phase_failures = []
    for art in sorted(run_dir.glob("0*.json")):
        try:
            if read_json(art).get("phase_failed"):
                phase_failures.append(art.name)
        except Exception:
            pass

    # Build TLDR
    n_applied = sum(1 for s in applied.get("steps", []) if s.get("status") in ("ok", "bumped"))
    n_failed_apply = sum(1 for s in applied.get("steps", []) if s.get("status") == "failed")
    n_audit_drift = sum(1 for s in audit.get("sections", []) if s.get("verdict") in ("DRIFT", "ATTENTION"))
    n_ideas = queue.get("ideas", {}).get("total_outstanding", 0)
    n_plans_approved = len(queue.get("plans", {}).get("approved", []))
    exec_status = "idle"
    if executor.get("executed"):
        exec_status = executor.get("status", "done")

    tldr_parts = [f"{n_applied} updates applied"]
    if n_failed_apply:
        tldr_parts.append(f"{n_failed_apply} failed")
    if n_audit_drift:
        tldr_parts.append(f"{n_audit_drift} audit items need attention")
    else:
        tldr_parts.append("audit clean")
    tldr_parts.append(f"{n_ideas} ideas, {n_plans_approved} plans approved")
    tldr_parts.append(f"executor: {exec_status}")
    tldr = " · ".join(tldr_parts) + "."
    if phase_failures:
        tldr += (f'<br><span style="color:#c62828; font-weight:700;">'
                 f'⚠ Phase failures: {", ".join(phase_failures)}</span>')

    # Troubleshoot section
    troubleshoot_html = ""
    if troubleshoot and troubleshoot.get("triggered"):
        ts_status = troubleshoot.get("agent_status", "unknown")
        pi_web_ok = troubleshoot.get("pi_web_healthy", False)
        diagnosis = troubleshoot.get("diagnosis", "")
        actions = troubleshoot.get("actions_taken", [])
        if pi_web_ok:
            badge = '<span style="color:#2e7d32; font-weight:700;">FIXED</span>'
            color = "#2e7d32"
        elif ts_status == "fixed":
            badge = '<span style="color:#2e7d32; font-weight:700;">FIXED</span>'
            color = "#2e7d32"
        elif ts_status == "partial":
            badge = '<span style="color:#e65100; font-weight:700;">PARTIAL</span>'
            color = "#e65100"
        else:
            badge = '<span style="color:#c62828; font-weight:700;">FAILED</span>'
            color = "#c62828"
        actions_html = "".join(f"<li>{a}</li>" for a in actions)
        troubleshoot_html = (
            '<tr><td style="padding:16px 32px 8px;">'
            f'<h2 style="margin:0; color:{color}; font-size:15px; font-weight:700;">'
            f'Troubleshooting Agent {badge}</h2>'
            '</td></tr>'
            '<tr><td style="padding:8px 32px 16px;">'
            f'<p style="margin:0 0 8px; color:#444; font-size:13px;"><strong>Diagnosis:</strong> {diagnosis}</p>'
            f'<p style="margin:0 0 4px; color:#666; font-size:12px;">Actions taken:</p>'
            f'<ul style="margin:0; padding-left:20px; color:#555; font-size:12px;">{actions_html}</ul>' if actions else ''
            '</td></tr>'
            '<tr><td style="padding:0 32px;"><hr style="border:none; border-top:1px solid #e8e8ee; margin:8px 0;"></td></tr>'
        )

    # Footer
    engine = "steward_runner.py (dry-run)" if dry_run else "steward_runner.py"
    footer = (f"carter2099.com · Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · "
              f"{engine} · run dir: {run_dir}")

    # Build template
    if not TEMPLATE_PATH.exists():
        TEMPLATE_PATH.write_text(DEFAULT_TEMPLATE)
    template = TEMPLATE_PATH.read_text()

    html = (
        template
        .replace("{{DATE}}", date_str)
        .replace("{{TLDR}}", tldr)
        .replace("{{UPDATES}}", _html_updates(applied))
        .replace("{{TROUBLESHOOT}}", troubleshoot_html)
        .replace("{{VALIDATION}}", _html_validation(validation))
        .replace("{{HEARTBEAT}}", _html_heartbeat(heartbeat))
        .replace("{{AUDIT}}", _html_audit(audit))
        .replace("{{QUEUE}}", _html_queue(queue))
        .replace("{{EXECUTOR}}", _html_executor(executor))
        .replace("{{BUDGET}}", _html_budget(budget))
        .replace("{{FOOTER}}", footer)
    )

    email_path = run_dir / "08-email.html"
    email_path.write_text(html)
    print(f"[P8] rendered -> {email_path}")

    # Build subject
    n_applied_count = n_applied
    audit_summary = f"{n_audit_drift} drift" if n_audit_drift else "clean"
    subject = (
        f"Steward {date_str} — "
        f"{n_applied_count} applied / audit: {audit_summary} / "
        f"queue: {n_ideas} ideas, {n_plans_approved} plans awaiting approval / "
        f"executor: {exec_status}"
    )

    if dry_run:
        print(f"  DRY RUN — would send: {subject}")
    else:
        try:
            run([
                "python3", str(DIGEST_SCRIPT),
                "--subject", subject,
                "--body-file", str(email_path),
                "--to", "carter2099@pm.me",
            ], timeout=60)
            print(f"  sent: {subject}")
        except subprocess.CalledProcessError as e:
            print(f"  SEND FAILED: {e}")

    return {"subject": subject, "email_path": str(email_path)}


# ── P9: archive ──────────────────────────────────────────────────────


def phase_9_archive(run_dir, setup_data, elapsed_s):
    """Phase 9: write summary.md, append runs.jsonl, prune old dirs."""
    print("[P9] archive")

    date_str = setup_data["date"]
    budget = setup_data.get("budget", {})

    # Load key artifacts for summary
    applied = read_json(run_dir / "01-applied.json") if (run_dir / "01-applied.json").exists() else {}
    validation = read_json(run_dir / "02-validation.json") if (run_dir / "02-validation.json").exists() else {}
    audit = read_json(run_dir / "07-audit.json") if (run_dir / "07-audit.json").exists() else {}
    queue = read_json(run_dir / "05-queue.json") if (run_dir / "05-queue.json").exists() else {}
    executor = read_json(run_dir / "06-executor.json") if (run_dir / "06-executor.json").exists() else {}

    # Build summary.md
    lines = [
        f"# Steward Report — {date_str}",
        f"**Engine:** steward_runner.py | **Guard:** {budget.get('guard_verdict','?')}",
        "",
        "## Updates Applied",
    ]
    for s in applied.get("steps", []):
        if s.get("dry_run"):
            lines.append("- Dry run — no mutations")
            break
        name = s.get("step", "")
        status = s.get("status", "")
        if name.startswith("auto_"):
            pkg = name.replace("auto_", "")
            if status == "ok":
                lines.append(f"- {pkg}: {s.get('pre_version')} -> {s.get('post_version')}")
            elif status == "skipped":
                lines.append(f"- {pkg}: already current ({s.get('pre_version')})")
            else:
                lines.append(f"- {pkg}: FAILED")
        elif name == "openwebui":
            if status == "bumped":
                lines.append(f"- open-webui: {s.get('current_tag')} -> {s.get('latest_tag')}")
            elif status == "current":
                lines.append(f"- open-webui: current at {s.get('current_tag')}")

    lines.append("")
    lines.append("## Validation")
    ep_ok = all(c.get("status") == "ok" for c in validation.get("checks", [])
                if c.get("name", "").startswith("endpoint_"))
    lines.append(f"- Endpoints: {'all passed' if ep_ok else 'SOME FAILED'}")

    lines.append("")
    lines.append("## Audit")
    for sec in audit.get("sections", []):
        lines.append(f"- {sec['name']}: {sec['verdict']}")

    lines.append("")
    lines.append("## Queue")
    lines.append(f"- Ideas outstanding: {queue.get('ideas', {}).get('total_outstanding', 0)}")
    lines.append(f"- Plans approved: {len(queue.get('plans', {}).get('approved', []))}")

    lines.append("")
    lines.append("## Executor")
    if executor.get("executed"):
        lines.append(f"- Plan: {executor.get('plan')} -> {executor.get('status')}")
    else:
        lines.append(f"- Idle ({executor.get('reason', 'no plan')})")

    md_content = "\n".join(lines) + "\n"
    (run_dir / "summary.md").write_text(md_content)

    # Append runs.jsonl
    n_sections_fired = sum(
        1 for s in audit.get("sections", [])
        if s.get("verdict") not in ("cached-PASS", "skipped-budget", "dry-run-collector-only")
    )
    n_judge_rejected = sum(
        len(s.get("judge_rejected", [])) for s in audit.get("sections", [])
    )
    runs_entry = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "duration_s": round(elapsed_s),
        "applied": sum(1 for s in applied.get("steps", []) if s.get("status") in ("ok", "bumped")),
        "guard": budget.get("guard_verdict", "?"),
        "executor": executor.get("status") if executor.get("executed") else None,
        "sections_fired": n_sections_fired,
        "judge_rejections": n_judge_rejected,
    }
    with open(RUNS_LOG, "a") as f:
        f.write(json.dumps(runs_entry) + "\n")

    # Prune run dirs >30 days
    cutoff = datetime.now() - timedelta(days=30)
    for d in RUN_DIR_BASE.iterdir():
        if d.is_dir() and len(d.name) == 10:  # YYYY-MM-DD
            try:
                d_date = datetime.strptime(d.name, "%Y-%m-%d")
                if d_date < cutoff:
                    import shutil
                    shutil.rmtree(d)
                    print(f"  pruned: {d.name}")
            except ValueError:
                pass

    print(f"[P9] done -> {run_dir / 'summary.md'}")


# ── main ──────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Homelab Steward — nightly deterministic Python orchestrator"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Skip mutations, executor, agent fan-out, and email send")
    parser.add_argument("--resume", action="store_true",
                        help="Skip phases whose output artifact already exists")
    args = parser.parse_args()

    start_ts = time.time()

    # P0: setup
    setup = phase_0_setup(args)
    run_dir = Path(setup["run_dir"])

    def should_run(artifact_name):
        if not args.resume:
            return True
        return not (run_dir / artifact_name).exists()

    # P1: apply
    try:
        if should_run("01-applied.json"):
            phase_1_apply(run_dir, dry_run=args.dry_run)
        else:
            print("[P1] skipped (resume)")
    except Exception as e:
        print(f"[P1] FAILED: {e}")
        write_json(run_dir / "01-applied.json",
                   {"steps": [], "phase_failed": True, "error": str(e)})

    # P2: validate
    try:
        if should_run("02-validation.json"):
            phase_2_validate(run_dir)
        else:
            print("[P2] skipped (resume)")
    except Exception as e:
        print(f"[P2] FAILED: {e}")
        write_json(run_dir / "02-validation.json",
                   {"checks": [], "phase_failed": True, "error": str(e)})

    # P3: troubleshoot
    try:
        phase_3_troubleshoot(run_dir, dry_run=args.dry_run)
    except Exception as e:
        print(f"[P3] FAILED: {e}")
        write_json(run_dir / "03-troubleshoot.json",
                   {"triggered": False, "phase_failed": True, "error": str(e)})

    # Check for reboot-required (kernel update from P1 apt upgrade)
    if _reboot_if_needed(run_dir, "P3", dry_run=args.dry_run):
        print("[reboot] system is going down for reboot — will resume on boot")
        sys.exit(0)
    # P4: heartbeat
    try:
        if should_run("04-heartbeat.json"):
            phase_4_heartbeat(run_dir)
        else:
            print("[P4] skipped (resume)")
    except Exception as e:
        print(f"[P4] FAILED: {e}")
        write_json(run_dir / "04-heartbeat.json",
                   {"phase_failed": True, "error": str(e)})

    # P5: work queue
    try:
        if should_run("05-queue.json"):
            phase_5_work_queue(run_dir)
        else:
            print("[P5] skipped (resume)")
    except Exception as e:
        print(f"[P5] FAILED: {e}")
        write_json(run_dir / "05-queue.json",
                   {"phase_failed": True, "error": str(e)})

    # P6: executor
    try:
        if should_run("06-executor.json"):
            phase_6_executor(run_dir, setup, dry_run=args.dry_run)
        else:
            print("[P6] skipped (resume)")
    except Exception as e:
        print(f"[P6] FAILED: {e}")
        write_json(run_dir / "06-executor.json",
                   {"executed": False, "phase_failed": True, "error": str(e)})

    # P7: audit
    try:
        if should_run("07-audit.json"):
            phase_7_audit(run_dir, setup, dry_run=args.dry_run)
        else:
            print("[P7] skipped (resume)")
    except Exception as e:
        print(f"[P7] FAILED: {e}")
        write_json(run_dir / "07-audit.json",
                   {"sections": [], "phase_failed": True, "error": str(e)})

    # P8: render + send
    try:
        phase_8_render_send(run_dir, setup, dry_run=args.dry_run)
    except Exception as e:
        print(f"[P8] FAILED: {e}")
        write_json(run_dir / "08-email.html",
                   f"<p>Render failed: {e}</p>")

    # P9: archive
    elapsed = time.time() - start_ts
    try:
        phase_9_archive(run_dir, setup, elapsed)
    except Exception as e:
        print(f"[P9] FAILED: {e}")

    print(f"\nDone in {elapsed:.0f}s")


if __name__ == "__main__":
    main()
