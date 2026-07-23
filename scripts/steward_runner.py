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
    "llm-proxy": "http://127.0.0.1:8081/health",
    "searxng": "http://127.0.0.1:8080/search?q=healthcheck&format=json",
}
STEWARD_MODEL = "opencode-go/deepseek-v4-flash"
SMALL_MODEL = "opencode-go/deepseek-v4-flash"
EXECUTOR_MODEL = "opencode-go/deepseek-v4-pro"
PROXY_HEALTH = "http://localhost:8082/health"
EXECUTOR_MONTHLY_CAP = 4
MAX_WORKERS = 3
EXECUTOR_TIMEOUT = 2700
EXECUTOR_MODE = "execute"
PENDING_PATH = HOME / "agent-state" / "pending.md"
DEPENDABOT_UNIT = "dependabot-webhook.service"


SECRET_PATTERNS = [
    re.compile(r".*api-token.*"),
    re.compile(r".*\.env$"),
    re.compile(r".*\.env\..*"),
    re.compile(r".*master\.key$"),
    re.compile(r".*auth\.json$"),
    re.compile(r".*\.pem$"),
    re.compile(r".*id_rsa.*"),
    re.compile(r".*id_ed25519.*"),
    re.compile(r".*\.ovpn$"),
    re.compile(r".*credentials\.json.*"),
    re.compile(r".*\.htpasswd.*"),
]

# ── default template ─────────────────────────────────────────────────

DEFAULT_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="margin:0; padding:0; background-color:#f4f4f7; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif; color:#2a2a36;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#f4f4f7; padding:24px 0;">
<tr><td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px; width:100%; background-color:#ffffff; border-radius:10px; overflow:hidden; box-shadow:0 2px 10px rgba(20,20,40,0.06);">
<!-- Header -->
<tr><td style="background-color:#1a1a2e; padding:26px 32px;">
<h1 style="margin:0; color:#ffffff; font-size:20px; font-weight:600; letter-spacing:0.2px;">Homelab Steward</h1>
<p style="margin:6px 0 0; color:#b8b8d0; font-size:13px;">{{DATE}}</p>
</td></tr>
<!-- Summary -->
<tr><td style="padding:22px 32px 14px;">
<p style="margin:0; color:#2a2a36; font-size:14px; line-height:1.55;">{{TLDR}}</p>
</td></tr>
{{TROUBLESHOOT}}
<!-- Updates Applied -->
<tr><td style="padding:18px 32px 0;">
<h2 style="margin:0; padding-left:10px; border-left:3px solid #2e7d32; color:#1a1a2e; font-size:13px; font-weight:700; letter-spacing:0.6px; text-transform:uppercase;">Updates Applied</h2>
</td></tr>
<tr><td style="padding:8px 32px 14px;">{{UPDATES}}</td></tr>
<tr><td style="padding:0 32px;"><hr style="border:none; border-top:1px solid #ececf2; margin:4px 0;"></td></tr>
<!-- Validation -->
<tr><td style="padding:18px 32px 0;">
<h2 style="margin:0; padding-left:10px; border-left:3px solid #1565c0; color:#1a1a2e; font-size:13px; font-weight:700; letter-spacing:0.6px; text-transform:uppercase;">Validation</h2>
</td></tr>
<tr><td style="padding:8px 32px 14px;">{{VALIDATION}}</td></tr>
<tr><td style="padding:0 32px;"><hr style="border:none; border-top:1px solid #ececf2; margin:4px 0;"></td></tr>
<!-- Heartbeat -->
<tr><td style="padding:18px 32px 0;">
<h2 style="margin:0; padding-left:10px; border-left:3px solid #5b3cc4; color:#1a1a2e; font-size:13px; font-weight:700; letter-spacing:0.6px; text-transform:uppercase;">Status Heartbeat</h2>
</td></tr>
<tr><td style="padding:8px 32px 14px;">{{HEARTBEAT}}</td></tr>
<tr><td style="padding:0 32px;"><hr style="border:none; border-top:1px solid #ececf2; margin:4px 0;"></td></tr>
<!-- Audit -->
<tr><td style="padding:18px 32px 0;">
<h2 style="margin:0; padding-left:10px; border-left:3px solid #00838f; color:#1a1a2e; font-size:13px; font-weight:700; letter-spacing:0.6px; text-transform:uppercase;">Nightly Audit</h2>
</td></tr>
<tr><td style="padding:8px 32px 14px;">{{AUDIT}}</td></tr>
<tr><td style="padding:0 32px;"><hr style="border:none; border-top:1px solid #ececf2; margin:4px 0;"></td></tr>
<!-- Work Queue -->
<tr><td style="padding:18px 32px 0;">
<h2 style="margin:0; padding-left:10px; border-left:3px solid #e65100; color:#1a1a2e; font-size:13px; font-weight:700; letter-spacing:0.6px; text-transform:uppercase;">Work Queue</h2>
</td></tr>
<tr><td style="padding:8px 32px 14px;">{{QUEUE}}</td></tr>
<tr><td style="padding:0 32px;"><hr style="border:none; border-top:1px solid #ececf2; margin:4px 0;"></td></tr>
<!-- Executor -->
<tr><td style="padding:18px 32px 0;">
<h2 style="margin:0; padding-left:10px; border-left:3px solid #6a1b9a; color:#1a1a2e; font-size:13px; font-weight:700; letter-spacing:0.6px; text-transform:uppercase;">Executor</h2>
</td></tr>
<tr><td style="padding:8px 32px 14px;">{{EXECUTOR}}</td></tr>
<tr><td style="padding:0 32px;"><hr style="border:none; border-top:1px solid #ececf2; margin:4px 0;"></td></tr>
<!-- Auto-Fixes -->
<tr><td style="padding:18px 32px 0;">
<h2 style="margin:0; padding-left:10px; border-left:3px solid #2e7d32; color:#1a1a2e; font-size:13px; font-weight:700; letter-spacing:0.6px; text-transform:uppercase;">Auto-Fixes</h2>
</td></tr>
<tr><td style="padding:8px 32px 14px;">{{FIXES}}</td></tr>
<tr><td style="padding:0 32px;"><hr style="border:none; border-top:1px solid #ececf2; margin:4px 0;"></td></tr>
<!-- Usage -->
<tr><td style="padding:18px 32px 0;">
<h2 style="margin:0; padding-left:10px; border-left:3px solid #37474f; color:#1a1a2e; font-size:13px; font-weight:700; letter-spacing:0.6px; text-transform:uppercase;">OpenCode Go Usage</h2>
</td></tr>
<tr><td style="padding:8px 32px 14px;">{{USAGE}}</td></tr>
<!-- Footer -->
<tr><td style="padding:18px 32px; background-color:#f8f8fb; border-top:1px solid #ececf2;">
<p style="margin:0; color:#7b7b8a; font-size:11px; text-align:center;">{{FOOTER}}</p>
</td></tr>
</table>
</td></tr>
</table>
</body></html>"""

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
        str(HOME / ".bun/bin/omp"), "-p", "--model", model,
        "--api-key", "proxy",
        "--session-dir", str(SESSION_DIR),
        "--allow-home",
        "--config", str(HOME / ".omp/agent/headless-override.yml"),
    ]
    cmd.append(prompt)
    result = subprocess.run(
        cmd,
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
    """Parse omp --mode json NDJSON output (same format as pi's --mode json).
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
        str(HOME / ".bun/bin/omp"), "-p", "--model", EXECUTOR_MODEL, "--mode", "json",
        "--api-key", "proxy",
        "--session-dir", str(SESSION_DIR),
        "--allow-home",
        "--config", str(HOME / ".omp/agent/headless-override.yml"),
    ]
    if extra_args:
        cmd.extend(extra_args)

    cmd.append(prompt)
    result = subprocess.run(
        cmd,
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
    """Create run dir, snapshot usage, stop dependabot, load prev-summary delta."""
    today = datetime.now()
    date_str = today.strftime("%Y-%m-%d")
    run_dir = RUN_DIR_BASE / date_str
    run_dir.mkdir(parents=True, exist_ok=True)

    prev_date = prev_workday(today)
    prev_date_str = prev_date.strftime("%Y-%m-%d")
    prev_md = RUN_DIR_BASE / f"{prev_date_str}" / "summary.md"
    prev_summary = parse_previous_summary(prev_md)

    # Usage report — snapshot proxy health (no gating, just reporting)
    usage = {"accounts": [], "proxy_error": None}
    try:
        req = urllib.request.Request(PROXY_HEALTH)
        with urllib.request.urlopen(req, timeout=10) as resp:
            proxy_health = json.loads(resp.read().decode())
    except Exception as e:
        proxy_health = {"error": str(e)}
        usage["proxy_error"] = str(e)

    if "accounts" in proxy_health:
        for acct in proxy_health["accounts"]:
            usage["accounts"].append({
                "name": acct.get("name", "?"),
                "tier": acct.get("tier", "unknown"),
                "rolling_pct": acct.get("rolling", {}).get("pct", 0),
                "weekly_pct": acct.get("weekly", {}).get("pct", 0),
                "monthly_pct": acct.get("monthly", {}).get("pct", 0),
                "payg_balance": acct.get("payg", {}).get("balance_usd"),
                "payg_monthly_used": acct.get("payg", {}).get("monthly_usage_usd"),
                "payg_monthly_limit": acct.get("payg", {}).get("monthly_limit_usd"),
            })

    # Dependabot management — stop the webhook so it doesn't race our executor
    dep = {"was_active": False, "stopped": False, "error": None}
    if not args.dry_run:
        try:
            active = run_capture(
                ["systemctl", "--user", "is-active", DEPENDABOT_UNIT],
                env=user_env(),
            ).strip()
            dep["was_active"] = (active == "active")
            if dep["was_active"]:
                run(["systemctl", "--user", "stop", DEPENDABOT_UNIT], env=user_env())
                dep["stopped"] = True
                print("  dependabot: stopped for steward run")
            else:
                print("  dependabot: already inactive")
        except Exception as e:
            dep["error"] = str(e)
            print(f"  dependabot: stop failed — {e}")
    data = {
        "date": date_str,
        "run_dir": str(run_dir),
        "prev_date": prev_date_str,
        "prev_summary_exists": prev_md.exists(),
        "dry_run": args.dry_run,
        "resume": args.resume,
        "usage": usage,
        "dependabot": dep,
    }
    artifact = run_dir / "00-setup.json"
    write_json(artifact, data)

    # Print usage summary
    acct_lines = []
    for a in usage["accounts"]:
        extra = ""
        if a["payg_balance"] is not None:
            extra = f", PAYG ${a['payg_balance']:.2f} remaining"
        acct_lines.append(f"    {a['name']} ({a['tier']}): "
                          f"rolling={a['rolling_pct']}%, weekly={a['weekly_pct']}%, "
                          f"monthly={a['monthly_pct']}%{extra}")
    print(f"[P0] setup -> {artifact}")
    if usage["proxy_error"]:
        print(f"  proxy: UNREACHABLE ({usage['proxy_error']})")
    else:
        print(f"  usage ({len(usage['accounts'])} accounts):")
        for line in acct_lines:
            print(line)
    return data


def _p1_apt_upgrade():
    """Run apt update + apt upgrade -y."""
    print("  [1a] apt update + upgrade")
    try:
        run(["sudo", "apt", "update"], capture_output=True, text=True)
        upgrade = run(["sudo", "apt", "upgrade", "-y"], capture_output=True, text=True)
        stdout = upgrade.stdout
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
        if name == "searxng":
            # SearXNG may return 200 with error content; validate JSON results array
            resp = run_capture(["curl", "-s", "--connect-timeout", "10", url])
            if resp:
                try:
                    data = json.loads(resp)
                    healthy = isinstance(data.get("results"), list)
                    checks.append({
                        "name": f"endpoint_{name}", "url": url,
                        "http_code": "200", "status": "ok" if healthy else "fail",
                        "content_valid": healthy,
                    })
                except json.JSONDecodeError:
                    checks.append({
                        "name": f"endpoint_{name}", "url": url,
                        "http_code": "??", "status": "fail",
                        "error": "invalid JSON response",
                    })
            else:
                checks.append({
                    "name": f"endpoint_{name}", "url": url,
                    "http_code": "??", "status": "fail",
                    "error": "empty response",
                })
        else:
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

    # open-webui running image vs compose tag
    owu_image_check = {"name": "openwebui_image_match", "status": "skipped"}
    try:
        running_image = run_capture(
            ["docker", "inspect", "open-webui", "--format", "{{.Config.Image}}"])
        if running_image:
            if OPENWEBUI_COMPOSE.exists():
                compose_text = OPENWEBUI_COMPOSE.read_text()
                compose_m = re.search(r"ghcr\.io/open-webui/open-webui:([^\s\"']+)", compose_text)
                compose_tag = compose_m.group(1) if compose_m else None
                if compose_tag:
                    owu_image_check["running_image"] = running_image
                    owu_image_check["compose_tag"] = compose_tag
                    if compose_tag in running_image:
                        owu_image_check["status"] = "ok"
                    else:
                        owu_image_check["status"] = "warning"
                else:
                    owu_image_check["reason"] = "could not parse compose tag"
            else:
                owu_image_check["reason"] = "compose file missing"
        else:
            owu_image_check["reason"] = "container not found or not running"
    except Exception as e:
        owu_image_check["status"] = "error"
        owu_image_check["error"] = str(e)
    checks.append(owu_image_check)

    # CF tunnel connector health
    cf_check = {"name": "endpoint_tunnel-health", "status": "skipped"}
    try:
        cf_token = (HOME / ".config" / "cloudflare" / "api-token").read_text().strip()
        cf_account_id = (HOME / ".config" / "cloudflare" / "account-id").read_text().strip()
        cf_tunnel_id = (HOME / ".config" / "cloudflare" / "homelab-tunnel-id").read_text().strip()
        if cf_token and cf_account_id and cf_tunnel_id:
            cf_url = (
                f"https://api.cloudflare.com/client/v4/accounts/{cf_account_id}"
                f"/cfd_tunnel/{cf_tunnel_id}/connections"
            )
            cf_req = urllib.request.Request(
                cf_url, headers={"Authorization": f"Bearer {cf_token}"})
            with urllib.request.urlopen(cf_req, timeout=15) as cf_resp:
                cf_data = json.loads(cf_resp.read().decode())
            connectors = cf_data.get("result", [])
            healthy = False
            active_conns = 0
            for connector in connectors:
                for conn in connector.get("conns", []):
                    if not conn.get("is_pending_reconnect", True):
                        active_conns += 1
            healthy = active_conns > 0
            cf_check["status"] = "ok" if healthy else "fail"
            cf_check["connector_count"] = len(connectors)
            cf_check["active_connections"] = active_conns
            cf_check["healthy"] = healthy
        else:
            cf_check["reason"] = "missing CF config files"
    except Exception as e:
        cf_check["status"] = "error"
        cf_check["error"] = str(e)
    checks.append(cf_check)

    data = {"checks": checks}
    write_json(run_dir / "02-validation.json", data)
    print(f"[P2] done -> {run_dir / '02-validation.json'}")
    return data


# ── P3: troubleshoot ─────────────────────────────────────────────────


def phase_3_troubleshoot(run_dir, dry_run=False):
    """Phase 3: spawn omp troubleshooting agent if endpoints regressed after P1 auto-apply.

    Loads yesterday's validation to detect regressions (was-ok, now-not-ok).
    Generalizes prompt with all regressed endpoints.
    Triggers only when an endpoint that was ok yesterday is now failing.
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

    # Check for P1 mutations
    auto_steps = [s for s in applied.get("steps", [])
                  if s.get("step", "").startswith("auto_") and s.get("status") == "ok"]
    owu_step = [s for s in applied.get("steps", [])
                if s.get("step") == "openwebui" and s.get("status") == "bumped"]
    mutations = len(auto_steps) + len(owu_step)
    if not mutations:
        print("[P3] skipped — no packages were actually upgraded")
        write_json(run_dir / "03-troubleshoot.json",
                   {"triggered": False, "reason": "no_mutations"})
        return

    # Build today's endpoint status map
    today_status = {}
    for c in validation.get("checks", []):
        if c.get("name", "").startswith("endpoint_"):
            today_status[c["name"]] = c.get("status", "?")

    # Load yesterday's validation for regression detection
    prev_date = prev_workday(datetime.now())
    prev_date_str = prev_date.strftime("%Y-%m-%d")
    prev_validation_path = RUN_DIR_BASE / prev_date_str / "02-validation.json"
    yesterday_status = {}
    if prev_validation_path.exists():
        try:
            prev_validation = read_json(prev_validation_path)
            for c in prev_validation.get("checks", []):
                if c.get("name", "").startswith("endpoint_"):
                    yesterday_status[c["name"]] = c.get("status", "?")
        except Exception as e:
            print(f"[P3] warning — could not read yesterday's validation: {e}")

    # Find regressions: yesterday ok, today not ok
    regressed = []
    for name, today_s in sorted(today_status.items()):
        yesterday_s = yesterday_status.get(name)
        if yesterday_s == "ok" and today_s != "ok":
            regressed.append(name)

    if not regressed:
        print("[P3] skipped — no endpoint regressions")
        write_json(run_dir / "03-troubleshoot.json",
                   {"triggered": False, "reason": "no_regressions",
                    "today_status": today_status, "yesterday_status": yesterday_status,
                    "mutations": mutations})
        return

    regressed_names = [r.replace("endpoint_", "") for r in regressed]
    print(f"[P3] TROUBLESHOOT — {len(regressed)} endpoint(s) regressed: {regressed_names}")

    # Gather diagnostic context for regressed services
    diag = {
        "applied_steps": applied.get("steps", []),
        "validation": today_status,
        "yesterday_validation": yesterday_status,
        "regressed": regressed,
        "containers": run_capture(
            ["docker", "ps", "-a", "--format", "{{.Names}} {{.Status}} {{.Image}}"]),
        "docker_journal": run_capture(
            ["sudo", "journalctl", "-u", "docker", "--since", "30 min ago",
             "--no-pager", "-n", "80"]),
    }

    # Add journal output for each regressed service
    for name in regressed_names:
        safe = name.replace("-", "_")
        journal_out = run_capture(
            ["journalctl", "--user", "-u", name, "--since", "30 min ago",
             "--no-pager", "-n", "50"], env=user_env())
        if not journal_out:
            journal_out = run_capture(
                ["sudo", "journalctl", "-u", name, "--since", "30 min ago",
                 "--no-pager", "-n", "50"])
        diag[f"{safe}_journal"] = journal_out


    # Build diagnostic journal sections for the prompt
    journal_sections = ""
    for key, val in sorted(diag.items()):
        if key.endswith("_journal") and key not in ("docker_journal",):
            journal_sections += f"- {key}:\n{val}\n\n"

    regressed_list = "\n".join(f"  - {r}" for r in regressed)
    troubleshoot_prompt = f"""
You are a homelab troubleshooter. The nightly steward auto-applied updates and now
the following endpoints have REGRESSED (were healthy yesterday, unhealthy today):

{regressed_list}


Your job: diagnose WHY these endpoints regressed and FIX them so we stay on the new versions.
Rolling back is a LAST RESORT — prefer fixing forward.

WHAT CHANGED (P1 applied steps):
{json.dumps(diag["applied_steps"], indent=2)}

VALIDATION TODAY:
{json.dumps(diag["validation"], indent=2)}

YESTERDAY (was healthy):
{json.dumps(diag.get("yesterday_validation", {}), indent=2)}

DIAGNOSTICS:
- Containers:
{diag["containers"]}
- Docker journal:
{diag["docker_journal"]}
{journal_sections}
RULES:
- You have full system access — use it.
- Common causes: orphaned docker-proxy holding a port (check ss -tlnp), docker daemon
  failed to restart after engine upgrade, cloudflared tunnel down, config mismatch,
  process crash.
- Export XDG_RUNTIME_DIR=/run/user/$(id -u) before any systemctl --user commands.
- If the fix is restarting a service, do it. If it's killing a docker-proxy, do it.
- If you genuinely cannot fix an endpoint, say so clearly and explain why.

Return a fenced ```json packet:
{{"status": "fixed"|"partial"|"failed",
 "diagnosis": "root cause in one sentence",
 "actions_taken": ["action 1", "action 2"],
 "healthy_endpoints": ["endpoint_name", ...],
 "remaining_issues": ["..."]}}
"""

    agent_output = ""
    agent_packet = {}
    try:
        agent_output = _call_omp_p(troubleshoot_prompt, timeout=600)
        agent_packet = _extract_json(agent_output, "troubleshoot packet")
    except Exception as e:
        agent_packet = {"status": "agent-failed", "diagnosis": str(e),
                        "actions_taken": [], "healthy_endpoints": [],
                        "remaining_issues": []}

    # Re-validate after agent
    re_validation = phase_2_validate(run_dir)
    write_json(run_dir / "02b-validation.json", re_validation)

    # Check which regressed endpoints are now healthy
    all_healthy = True
    for c in re_validation.get("checks", []):
        if c.get("name") in regressed and c.get("status") != "ok":
            all_healthy = False

    data = {
        "triggered": True,
        "regressed": regressed,
        "agent_status": agent_packet.get("status", "unknown"),
        "diagnosis": agent_packet.get("diagnosis", ""),
        "actions_taken": agent_packet.get("actions_taken", []),
        "healthy_endpoints": agent_packet.get("healthy_endpoints", []),
        "remaining_issues": agent_packet.get("remaining_issues", []),
        "agent_raw": agent_output[:4000],
        "re_validation_healthy": all_healthy,
    }
    if not all_healthy:
        data["final_diagnostics"] = {
            "containers": run_capture(
                ["docker", "ps", "-a", "--format", "{{.Names}} {{.Status}} {{.Image}}"]),
        }
    write_json(run_dir / "03-troubleshoot.json", data)
    print(f"[P3] done -> {run_dir / '03-troubleshoot.json'} "
          f"(agent: {agent_packet.get('status')}, regressed: {regressed_names})")
    return data


# ── P3a: deterministic auto-remediation ──────────────────────────────


def phase_3a_remediation(run_dir, dry_run=False):
    """Phase 3a: deterministic auto-remediation — no LLM.

    Checks:
    1. Orphaned docker-proxy processes on documented ports
    2. ufw rules for cni0/flannel.1
    3. Docker bridge rules for 8081/8082
    """
    print("[P3a] deterministic remediation")

    DOCUMENTED_PORTS = {
        33099: "blog",
        43080: "delta_neutral",
        48100: "open-webui",
        8080: "searxng",
        8081: "llm-proxy",
        8082: "opencode-go-proxy",
    }

    docker_proxy_results = []
    ufw_results = []
    bridge_results = []

    # ── 1. Orphaned docker-proxy check ──
    ss_out = run_capture(["sudo", "ss", "-tlnp", "state", "LISTEN"])
    for port, container_name in DOCUMENTED_PORTS.items():
        result = {"port": port, "container": container_name, "action": "skipped"}
        try:
            matching_lines = [l for l in ss_out.splitlines()
                              if re.search(rf":{port}\s", l)]
            if not matching_lines:
                result["action"] = "skipped"
                result["pre_state"] = "no_listener"
                result["post_state"] = "no_listener"
                docker_proxy_results.append(result)
                continue

            for line in matching_lines:
                result["pre_state"] = line.strip()
                if "docker-proxy" not in line:
                    result["action"] = "attention_needed"
                    result["reason"] = f"port held by non-docker-proxy process"
                    result["post_state"] = line.strip()
                    continue

                pid_match = re.search(r"pid=(\d+)", line)
                pid = int(pid_match.group(1)) if pid_match else None
                if not pid:
                    result["action"] = "attention_needed"
                    result["reason"] = "docker-proxy found but could not extract PID"
                    result["post_state"] = line.strip()
                    continue

                container_status = run_capture(
                    ["docker", "ps", "-a", "--filter", f"name={container_name}",
                     "--format", "{{.Status}}"])
                result["container_status"] = container_status or "not_found"

                if "Exited" in (container_status or ""):
                    if not dry_run:
                        run_capture(["sudo", "kill", str(pid)])
                        run_capture(["docker", "rm", container_name])
                        post_ss = run_capture(["sudo", "ss", "-tlnp", "state", "LISTEN"])
                        post_lines = [l for l in post_ss.splitlines()
                                      if re.search(rf":{port}\s", l)]
                        result["post_state"] = post_lines[0].strip() if post_lines else "port_free"
                    else:
                        result["post_state"] = f"would kill pid={pid} and rm {container_name} (dry run)"
                    result["action"] = "killed"
                    result["killed_pid"] = pid
                elif container_status:
                    result["action"] = "skipped"
                    result["reason"] = f"container running ({container_status})"
                    result["post_state"] = line.strip()
                else:
                    result["action"] = "attention_needed"
                    result["reason"] = "docker-proxy found but container not in docker ps"
                    result["post_state"] = line.strip()
        except Exception as e:
            result["action"] = "error"
            result["error"] = str(e)
        docker_proxy_results.append(result)

    # ── 2. ufw cni0/flannel.1 rules ──
    ufw_status = run_capture(["sudo", "ufw", "status", "numbered"])
    for iface in ["cni0", "flannel.1"]:
        result = {"rule": iface, "action": "already_present"}
        try:
            if iface in ufw_status:
                result["action"] = "already_present"
                result["output"] = "rule exists"
            else:
                if not dry_run:
                    ufw_out = run_capture(["sudo", "ufw", "allow", "in", "on", iface])
                    result["action"] = "added"
                    result["output"] = ufw_out
                else:
                    result["action"] = "would_add"
                    result["output"] = "dry run"
        except Exception as e:
            result["action"] = "error"
            result["error"] = str(e)
        ufw_results.append(result)

    # ── 3. Docker bridge rules for 8081/8082 ──
    bridge_id = run_capture(
        ["docker", "network", "inspect", "homelab-chat-search", "--format", "{{.Id}}"])

    if not bridge_id:
        for port in [8082, 8081]:
            bridge_results.append({
                "port": port, "bridge": None,
                "action": "skipped",
                "reason": "network homelab-chat-search not found",
            })
    else:
        short_id = bridge_id[:12]
        bridge_iface = f"br-{short_id}"

        # Probe: can open-webui reach host.docker.internal:8082?
        probe_ok = run_ok(["docker", "exec", "open-webui", "curl", "-s",
                          "--connect-timeout", "5",
                          "http://host.docker.internal:8082/health"])

        ufw_status_bridge = run_capture(["sudo", "ufw", "status"])

        for port in [8082, 8081]:
            result = {"port": port, "bridge": bridge_iface, "action": "already_present"}
            try:
                if probe_ok and port == 8082:
                    result["action"] = "skipped"
                    result["reason"] = "probe succeeded — bridge rules working"
                    bridge_results.append(result)
                    continue

                has_rule = any(
                    bridge_iface in line and str(port) in line
                    for line in ufw_status_bridge.splitlines()
                )
                if has_rule:
                    result["action"] = "already_present"
                else:
                    if not dry_run:
                        allow_out = run_capture(
                            ["sudo", "ufw", "allow", "in", "on", bridge_iface,
                             "to", "any", "port", str(port), "proto", "tcp"])
                        result["action"] = "added"
                        result["output"] = allow_out
                    else:
                        result["action"] = "would_add"
                        result["output"] = "dry run"
            except Exception as e:
                result["action"] = "error"
                result["error"] = str(e)
            bridge_results.append(result)

    data = {
        "docker_proxy": docker_proxy_results,
        "ufw_rules": ufw_results,
        "bridge_rules": bridge_results,
    }
    write_json(run_dir / "03a-remediation.json", data)
    print(f"[P3a] done -> {run_dir / '03a-remediation.json'}")
    return data


# ── P4: heartbeat ────────────────────────────────────────────────────

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

    # Journal disk usage
    journal_usage = run_capture(["journalctl", "--disk-usage"])

    # NVMe SMART health
    smart_data = {}
    smartctl_path = "/usr/sbin/smartctl"
    if Path(smartctl_path).exists() and Path("/dev/nvme0n1").exists():
        out, stderr, rc = run_capture_ok(["sudo", smartctl_path, "-a", "/dev/nvme0n1"], timeout=30)
        wear_pct = ""
        spare = ""
        spare_thresh = ""
        media_errors = ""
        error_log = ""
        for line in out.splitlines():
            if "Percentage Used:" in line:
                wear_pct = line.split(":")[-1].strip()
            elif "Available Spare:" in line:
                spare = line.split(":")[-1].strip()
            elif "Available Spare Threshold:" in line:
                spare_thresh = line.split(":")[-1].strip()
            elif "Media and Data Integrity Errors:" in line:
                media_errors = line.split(":")[-1].strip()
            elif "Error Information Log Entries:" in line:
                error_log = line.split(":")[-1].strip()
        smart_data = {
            "wear_pct": wear_pct, "available_spare": spare,
            "spare_threshold": spare_thresh, "media_errors": media_errors,
            "error_log_entries": error_log,
            "raw_output": out[:2000],
        }
    else:
        smart_data = {"status": "skipped", "reason": "smartctl or /dev/nvme0n1 not found"}

    # Reboot required
    reboot_needed = (Path("/var/run/reboot-required")).exists()
    kernel_ver = run_capture(["uname", "-r"])

    # Snap refresh
    snap_list = run_capture(["snap", "refresh", "--list"])

    # Memory pressure / OOM risk
    mem_free = run_capture(["free", "-h"])
    mem_pressure = run_capture(["cat", "/proc/pressure/memory"]) if Path("/proc/pressure/memory").exists() else ""
    mem_avail = ""
    for line in mem_free.splitlines():
        if "Mem:" in line:
            parts = line.split()
            if len(parts) >= 7:
                mem_avail = parts[6]

    # TLS cert expiry for 3 hostnames
    tls_certs = {}
    for host in ["blog.carter2099.com", "chat.carter2099.com"]:
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

    # DNS resolution of homelab hostnames
    dns_hostnames = [
        "blog.carter2099.com", "chat.carter2099.com",
        "freshrss.carter2099.com", "deltaneutral.carter2099.com", "hooks.carter2099.com",
    ]
    dns_results = {}
    for host in dns_hostnames:
        out = run_capture(["dig", "+short", host], timeout=10)
        dns_results[host] = {"resolves": bool(out), "records": out.splitlines() if out else []}

    # /etc/hosts gamingrig entry
    hosts_gamingrig = run_capture(["getent", "hosts", "gamingrig"])
    hosts_gamingrig_ok = bool(hosts_gamingrig and not hosts_gamingrig.startswith("error"))

    # docker-user-rules iptables verification
    iptables_docker_user = run_capture(["sudo", "iptables", "-L", "DOCKER-USER", "-n"])
    iptables_ok = "DROP" in iptables_docker_user and "0.0.0.0/0" in iptables_docker_user

    # User-unit inventory vs documented set
    documented_units = {
        "homelab-backup.service", "homelab-backup.timer",
        "homelab-backup-notify.service",
        "digests-daily.service", "digests-daily.timer",
        "hyperliquid-sdk.service", "hyperliquid-sdk.timer",
        "homelab-steward.service", "homelab-steward.timer",
        "homelab-steward-resume.service", "homelab-steward-resume.timer",
        "homelab-steward-notify.service",
        "opencode-go-proxy.service",
        "llm-proxy.service",
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

    # System unit inventory
    documented_system_units = {
        "cloudflared.service", "docker-user-rules.service", "ssh.service",
        "ufw.service", "cron.service", "containerd.service", "docker.service",
        "apparmor.service", "fstrim.timer",
    }
    all_system_units = run_capture(["systemctl", "list-units", "--all", "--no-legend"])
    active_system_units = set()
    for line in all_system_units.splitlines():
        parts = line.split()
        if parts:
            name = parts[0]
            if name.endswith(".service") or name.endswith(".timer"):
                active_system_units.add(name)
    extra_system_units = active_system_units - documented_system_units
    missing_system_units = documented_system_units - active_system_units

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

    # bundle-audit
    bundle_audit = {}
    for app_name, gemfile_lock in [("blog", HOME / "blog" / "blog" / "Gemfile.lock"),
                                     ("delta_neutral", HOME / "delta_neutral" / "delta_neutral" / "Gemfile.lock")]:
        if gemfile_lock.exists():
            out = run_capture(["bundle-audit", "check", "--gemfile-lock", str(gemfile_lock)],
                             timeout=120)
            bundle_audit[app_name] = out if out else "no vulnerabilities found"
        else:
            bundle_audit[app_name] = "Gemfile.lock not found"

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

    # Self-drift detection
    # Endpoints: compare docker exposed ports to ENDPOINTS
    docker_ps = run_capture(["docker", "ps", "--format", "{{.Names}}\t{{.Ports}}"])
    exposed_ports = set()
    for line in docker_ps.splitlines():
        if "\t" in line:
            _, ports = line.split("\t", 1)
            for part in ports.split(", "):
                if "->" in part:
                    host_part = part.split("->")[0]
                    if ":" in host_part:
                        port_str = host_part.rsplit(":", 1)[-1]
                        try:
                            exposed_ports.add(int(port_str))
                        except ValueError:
                            pass
    endpoint_ports = set()
    for url in ENDPOINTS.values():
        m = re.search(r":(\d+)", url)
        if m:
            endpoint_ports.add(int(m.group(1)))
    extra_ports_drift = sorted(exposed_ports - endpoint_ports)
    missing_endpoints_drift = sorted(endpoint_ports - exposed_ports)

    # Unit drift: installed user units vs documented
    installed_user_units = set()
    user_unit_files = run_capture(
        ["systemctl", "--user", "list-unit-files", "--no-legend"],
        env=env,
    )
    for line in user_unit_files.splitlines():
        parts = line.split()
        if parts:
            name = parts[0]
            if name.endswith(".service") or name.endswith(".timer"):
                installed_user_units.add(name)
    extra_installed_units = sorted(installed_user_units - documented_units)
    stale_documented_units = sorted(documented_units - installed_user_units)

    # AUTO_PKGS drift
    auto_pkg_installed = set()
    try:
        apt_check = run_capture(
            ["bash", "-c",
             "apt list --installed 2>/dev/null | grep -E 'docker-ce|docker-ce-cli|containerd|cloudflared'"]
        )
        for line in apt_check.splitlines():
            pkg = line.split("/")[0].strip()
            if pkg:
                auto_pkg_installed.add(pkg)
    except Exception:
        pass
    auto_pkg_extra = sorted(auto_pkg_installed - set(AUTO_PKGS))
    auto_pkg_missing = sorted(set(AUTO_PKGS) - auto_pkg_installed)

    # TLS hostname drift: compare tunnel routes to TLS-checked hostnames
    tunnel_hostnames = []
    try:
        tunnel_list = run_capture(["cloudflared", "tunnel", "list"], timeout=15)
        for line in tunnel_list.splitlines():
            parts = line.split()
            if parts and len(parts) >= 2:
                tid = parts[0]
                if tid and tid != "ID":
                    routes = run_capture(
                        ["cloudflared", "tunnel", "route", "dns", tid],
                        timeout=15,
                    )
                    for rline in routes.splitlines():
                        rparts = rline.split()
                        if rparts and "." in rparts[0]:
                            tunnel_hostnames.append(rparts[0])
                    break
    except Exception:
        pass
    tls_checked_hostnames = ["blog.carter2099.com", "chat.carter2099.com"]
    unchecked_tls = sorted(set(tunnel_hostnames) - set(tls_checked_hostnames))

    self_drift = {
        "endpoints": {
            "extra_ports": extra_ports_drift,
            "missing_endpoints": missing_endpoints_drift,
        },
        "units": {
            "extra_installed": extra_installed_units,
            "stale_documented": stale_documented_units,
        },
        "auto_pkgs": {
            "extra_installed": auto_pkg_extra,
            "missing_from_list": auto_pkg_missing,
        },
        "tls_hostnames": {
            "tunnel_hostnames": tunnel_hostnames,
            "checked_hostnames": tls_checked_hostnames,
            "unchecked": unchecked_tls,
        },
    }

    data = {
        "failed_units": {
            "user": failed_user.splitlines() if failed_user else [],
            "system": failed_system.splitlines() if failed_system else [],
        },
        "llm_stack": {"health": llm_health, "falling_back": falling_back},
        "backup": {"last_run": backup_ts},
        "k3s_nodes": nodes.splitlines() if nodes else [],
        "disk": {"df_root": disk_df, "docker_system_df": docker_df},
        "journal_disk_usage": journal_usage,
        "smart": smart_data,
        "reboot": {"needed": reboot_needed, "kernel": kernel_ver},
        "snap": {"refresh_list": snap_list if snap_list and "All snaps up to date" not in snap_list else ""},
        "memory": {"free_output": mem_free, "available": mem_avail, "pressure": mem_pressure},
        "tls_certs": tls_certs,
        "dns": dns_results,
        "hosts": {"gamingrig": {"resolves": hosts_gamingrig_ok, "output": hosts_gamingrig}},
        "docker_user_rules": {
            "chain_present": bool(iptables_docker_user),
            "has_drop_default": iptables_ok,
            "output": iptables_docker_user[:500],
        },
        "units": {
            "active": sorted(active_units),
            "documented": sorted(documented_units),
            "extra": sorted(extra_units),
            "missing": sorted(missing_units),
            "system": {
                "active": sorted(active_system_units),
                "documented": sorted(documented_system_units),
                "extra": sorted(extra_system_units),
                "missing": sorted(missing_system_units),
            },
        },
        "agent_state_stale": agent_state_stale,
        "bundle_audit": bundle_audit,
        "steward_self": steward_self,
        "self_drift": self_drift,
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
    """Phase 6: execute one approved plan via omp agent + post-impl review."""
    print("[P6] executor")
    queue_path = run_dir / "05-queue.json"
    if not queue_path.exists():
        print("  skipped — no queue data")
        data = {"executed": False, "reason": "no_queue_data"}
        write_json(run_dir / "06-executor.json", data)
        return data

    queue = read_json(queue_path)
    candidate = queue.get("executor_candidate")
    usage = setup_data.get("usage", {})

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
        raw_text, stats, packet, raw_ndjson = _call_omp_p_json(full_prompt)
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
        "npm_global": run_capture(["npm", "ls", "-g", "omp"]),
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


def _gather_repo_secrets():
    """Scan repos for uncommitted secret files and recent secret-commits in git history.

    Pure Python, deterministic, no LLM. Returns a dict with:
      - repos_scanned: int
      - working_tree_issues: list of dicts
      - commit_issues: list of dicts
      - findings_summary: str
    """
    issues_wt = []
    issues_commit = []
    repos_scanned = 0

    repo_candidates = []

    # ~/dev/*/ directories
    dev_dir = HOME / "dev"
    if dev_dir.is_dir():
        for d in sorted(dev_dir.iterdir()):
            if d.is_dir():
                repo_candidates.append(("dev/" + d.name, d, False))

    # Specific repos
    for name, path, is_bare in [
        ("homelab-backup", HOME / "homelab-backup", False),
        ("notes", HOME / "notes", False),
    ]:
        if path.is_dir():
            repo_candidates.append((name, path, is_bare))

    # Dotfiles bare repo
    dotfiles_git_dir = HOME / ".dotfiles-homelab"
    if dotfiles_git_dir.is_dir():
        repo_candidates.append(("dotfiles", dotfiles_git_dir, True))

    for name, path, is_bare in repo_candidates:
        # Verify git repo
        if is_bare:
            git_base = ["--git-dir", str(path)]
        else:
            git_base = ["-C", str(path)]

        check = run_capture_ok(["git"] + git_base + ["rev-parse", "--git-dir"])
        if check[2] != 0:
            continue

        remotes = run_capture(["git"] + git_base + ["remote", "-v"])
        if not remotes:
            continue

        repos_scanned += 1

        # Working tree scan (skip bare repos — P9b handles dotfiles)
        if not is_bare:
            status_out = run_capture(["git"] + git_base + ["status", "--short"])
            if status_out:
                for line in status_out.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    xy = line[:2]
                    filepath = line[3:].strip()
                    for pat in SECRET_PATTERNS:
                        if pat.match(filepath) or pat.match(Path(filepath).name):
                            issue_type = "untracked" if xy == "??" else "modified"
                            issues_wt.append({
                                "repo": name,
                                "path": filepath,
                                "issue": f"{issue_type} secret file",
                                "status": xy,
                            })
                            break

        # Recent commit scan
        log_cmd = ["git"] + git_base + ["log", "--all", "--since=24 hours ago", "-p", "--", "."]
        log_out, log_err, log_rc = run_capture_ok(log_cmd)
        if log_out:
            current_commit = ""
            current_date = ""
            current_file = ""
            findings = 0

            for line in log_out.splitlines():
                if line.startswith("commit "):
                    current_commit = line.split()[1][:8]
                    current_date = ""
                    current_file = ""
                    continue
                if line.startswith("Date:"):
                    current_date = line[5:].strip()
                    continue
                if line.startswith("diff --git a/"):
                    parts = line.split(" b/")
                    current_file = parts[-1] if len(parts) > 1 else ""
                    continue
                if line.startswith("---") or line.startswith("+++") or line.startswith("@@"):
                    continue
                if line.startswith("index ") or line.startswith("new file ") or line.startswith("deleted file "):
                    continue

                if not line.startswith("+"):
                    continue
                if line.startswith("+++"):
                    continue

                content = line[1:]

                found_issue = None
                if re.search(r"AKIA[0-9A-Z]{16}", content):
                    found_issue = "possible AWS access key in diff"
                elif re.search(r"ghp_[0-9a-zA-Z]{36}|gho_[0-9a-zA-Z]{36}|ghu_[0-9a-zA-Z]{36}|ghs_[0-9a-zA-Z]{36}|ghr_[0-9a-zA-Z]{36}|github_pat_[0-9a-zA-Z_]{82,}", content):
                    found_issue = "possible GitHub token in diff"
                elif re.search(r"-----BEGIN\s?(?:RSA|DSA|EC|OPENSSH|PGP)\s?PRIVATE KEY-----", content):
                    found_issue = "possible private key in diff"
                elif re.search(r"hooks\.slack\.com/services/T[a-zA-Z0-9_]{8,}/B[a-zA-Z0-9_]{8,}/[a-zA-Z0-9_]{24,}", content):
                    found_issue = "possible Slack webhook in diff"
                elif re.search(r"-----BEGIN CERTIFICATE-----", content):
                    found_issue = "possible certificate in diff"

                if found_issue and findings < 20:
                    issues_commit.append({
                        "repo": name,
                        "commit": current_commit,
                        "date": current_date,
                        "path": current_file,
                        "issue": found_issue,
                    })
                    findings += 1

    total_issues = len(issues_wt) + len(issues_commit)
    if total_issues == 0:
        findings_summary = "clean \u2014 no secrets detected"
    else:
        repo_count = len({i["repo"] for i in issues_wt + issues_commit})
        findings_summary = f"{total_issues} issues across {repo_count} repos scanned"

    return {
        "repos_scanned": repos_scanned,
        "working_tree_issues": issues_wt,
        "commit_issues": issues_commit,
        "findings_summary": findings_summary,
    }

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
        "repo_secrets": _gather_repo_secrets(),
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

    # Parse notes INDEX.md for cross-reference with disk
    indexed = set()
    index_path = HOME / "notes" / "INDEX.md"
    if index_path.exists():
        for line in index_path.read_text().splitlines():
            # Skip format template lines - the literal example "path/to/file.md"
            if line.strip().startswith("Format:") or "path/to/" in line:
                continue
            m = re.search(r"\]\(([^)]+\.md)\)", line)
            if m:
                indexed.add(m.group(1))

    notes_dir = HOME / "notes"
    on_disk = set()
    if notes_dir.exists():
        for md in notes_dir.rglob("*.md"):
            if "sessions" in md.parts:
                continue
            rel = str(md.relative_to(notes_dir))
            # Never flag the index itself or repo boilerplate
            if rel in ("INDEX.md", "README.md"):
                continue
            on_disk.add(rel)

    return {
        "k3s_config_diff": k3s_diff,
        "dotfiles_status": dotfiles_status,
        "notes_status": notes_status,
        "deploy_repos": deploy_repos,
        "notes_in_index_not_on_disk": sorted(list(indexed - on_disk)),
        "notes_on_disk_not_in_index": sorted(list(on_disk - indexed)),
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

def _audit_collector_8_docs_accuracy():
    """Collector: docs/ file content + related system state for fact-checking."""
    docs_dir = HOME / "notes" / "docs"
    evidence = {"doc_files": {}, "system_state": {}}

    # Doc file hashes (for delta gate — skip if no prior run to compare)
    if docs_dir.exists():
        for md_file in sorted(docs_dir.rglob("*.md")):
            rel = md_file.relative_to(docs_dir)
            evidence["doc_files"][str(rel)] = {
                "sha256": hashlib.sha256(md_file.read_bytes()).hexdigest(),
                "size": md_file.stat().st_size,
            }
    else:
        evidence["doc_files"]["_missing"] = True

    # System state that docs reference
    evidence["system_state"] = {
        "docker_ps": run_capture(
            ["docker", "ps", "--format", "{{.Names}}\t{{.Image}}\t{{.Ports}}\t{{.Status}}"]),
        "listening_ports": run_capture(
            ["sudo", "ss", "-tlnp", "--no-header"]),
        "user_services": run_capture(
            ["systemctl", "--user", "list-units", "--type=service", "--all", "--no-legend"],
            env=user_env()),
        "user_timers": run_capture(
            ["systemctl", "--user", "list-timers", "--all"], env=user_env()),
        "docker_images": run_capture(
            ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}\t{{.CreatedAt}}"]),
        "k3s_nodes": run_capture(
            [K3S, "kubectl", "get", "nodes"], env=user_env()),
        "ip_addr": run_capture(["ip", "-4", "addr", "show", "enp3s0f0"]),
    }
    return evidence


AUDIT_SECTIONS = [
    {
        "name": "agents-md-truth",
        "collector": _audit_collector_1_agents_md,
        "artifact": "07-audit-1-agents-md.json",
        "timeout": 600,
        "guidance": (
            "Truth-check /home/carter/AGENTS.md against the live host. READ the file first. "
            "Verify (1) pointer targets still resolve (paths, commands it cites) and (2) structural/"
            "semantic facts: IP roles (.100 DHCP/default, .92 k3s+blog/delta_neutral), "
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
            "neovim, omp (npm), docker images (searxng, freshrss, traefik, open-webui), "
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
            "(unique domains), stories-in-flight.json hygiene (5d cool / 7d prune enforced), duration trends, "
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
            "(loopback-only: open-webui 48100, searxng 8080, llm-proxy 8081; ufw-gated: 8082; "
            "LAN: blog 33099, delta 43080), ufw ruleset intact (cni0/flannel.1/docker bridges), unattended-upgrades "
            "active, carter2099.com RDAP expiry (>30d out = ok), CF tunnel ingress vs expected hostnames "
            "(chat, hooks, deltaneutral, freshrss, blog, ssh), SSH failed-password volume. Flag anything unexpected. For repo_secrets: working_tree_issues means secret-pattern files are uncommitted in a repo \u2014 flag each as ATTENTION; commit_issues means a secret-pattern string appeared in recent diffs \u2014 flag as ATTENTION with the commit SHA. No findings = PASS for this sub-check."
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
            "~/.omp/agent/sessions-automated if you need outcomes the journal lacks. Flag failed or silently-"
            "skipped runs."
        ),
    },
    {
        "name": "docs-accuracy",
        "collector": _audit_collector_8_docs_accuracy,
        "artifact": "07-audit-8-docs.json",
        "timeout": 600,
        "guidance": (
            "Truth-check the doc files in ~/notes/docs/ against the live host. "
            "Read each .md file that has changed (check evidence doc_files sha256 vs prior run) "
            "and verify factual claims: port numbers, paths, service names, process names, "
            "URLs, config file locations, IP addresses, command syntax. "
            "For every DRIFT propose exact OLD_TEXT -> NEW_TEXT edits. "
            "Prefer UNVERIFIABLE over guessing. "
            "Files to check: docs/homelab/hardware.md, deployment.md, k3s.md, blog.md, "
            "delta-neutral.md, dependabot-webhook.md, open-webui.md, omp-web.md, searxng.md, "
            "cloudflare.md, opencode-go-proxy.md, local-llm-gaming-rig.md, email-digests.md, "
            "homelab-steward.md, homelab-backup.md."
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
    master = {"sections": all_results}
    write_json(run_dir / "07-audit.json", master)
    print(f"[P7] done -> {run_dir / '07-audit.json'}")
    return master


# ── P8: render + send ────────────────────────────────────────────────


def _chip(text, color):
    """Inline rounded status chip."""
    return (
        f'<span style="display:inline-block; padding:1px 8px; border-radius:10px; '
        f'background-color:{color}1f; color:{color}; font-size:10px; font-weight:700; '
        f'letter-spacing:0.6px; text-transform:uppercase; white-space:nowrap;">{text}</span>'
    )


def _dot(level):
    color = {"ok": "#2e7d32", "warn": "#e65100",
             "danger": "#c62828", "muted": "#9aa0b2"}.get(level, "#9aa0b2")
    return (
        f'<span style="display:inline-block; width:7px; height:7px; border-radius:50%; '
        f'background-color:{color}; vertical-align:middle; margin-right:5px; '
        f'font-size:0; line-height:0;">&nbsp;</span>'
    )


def _sub_header(label):
    return (
        f'<p style="margin:12px 0 3px; color:#7b7b8a; font-size:10px; font-weight:700; '
        f'letter-spacing:0.8px; text-transform:uppercase;">{label}</p>'
    )


def _kv_rows(rows):
    """rows: list of (label, value_html). Returns a 2-column nested table."""
    if not rows:
        return ""
    out = ['<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
           'style="font-size:13px; color:#2a2a36; border-collapse:collapse;">']
    for label, val in rows:
        out.append(
            '<tr>'
            f'<td width="40%" style="padding:4px 12px 4px 0; vertical-align:top; '
            f'color:#7b7b8a; font-size:12px;">{label}</td>'
            f'<td style="padding:4px 0; vertical-align:top; color:#2a2a36;">{val}</td>'
            '</tr>'
        )
    out.append('</table>')
    return "".join(out)


def _badge(verdict):
    """Return an HTML status chip for an audit verdict."""
    palette = {
        "PASS": "#2e7d32",
        "DRIFT": "#c62828",
        "ATTENTION": "#e65100",
        "UNVERIFIABLE": "#9aa0b2",
        "collector-failed": "#c62828",
        "worker-failed": "#c62828",
        "dry-run-collector-only": "#9aa0b2",
    }
    label = verdict
    if verdict == "dry-run-collector-only":
        label = "collector-only · dry-run"
    elif verdict == "collector-failed":
        label = "collector failed"
    elif verdict == "worker-failed":
        label = "worker failed"
    color = palette.get(verdict, "#9aa0b2")
    if verdict.startswith("cached-"):
        base = verdict.removeprefix("cached-")
        color = {"PASS": "#2e7d32", "DRIFT": "#c62828",
                 "ATTENTION": "#e65100"}.get(base, "#9aa0b2")
        return _chip(f"CACHED {base}", color)
    return _chip(label, color)


def _fix_one_section(section_name, confirmed_findings, dry_run):
    """Fix all confirmed findings for one audit section. Returns fix result dict."""
    if dry_run:
        return {
            "section": section_name,
            "status": "dry-run",
            "findings_count": len(confirmed_findings),
            "fixes_applied": [],
            "judge_verdict": "dry-run",
        }

    # Skip if no findings or none are actionable
    actionable = confirmed_findings  # all confirmed findings are actionable
    if not actionable:
        return {
            "section": section_name,
            "status": "skipped",
            "reason": "no actionable findings",
            "findings_count": len(confirmed_findings),
            "fixes_applied": [],
        }

    # Build fix prompt with all findings for this section
    findings_text = json.dumps(actionable, indent=2)
    fix_prompt = (
        f"Fix the following homelab issues found by the steward audit "
        f"for section '{section_name}'.\n\n"
        f"You are a homelab maintenance agent. For each finding below, apply "
        f"the fix described. Work in ~/dev/ clones for code changes, commit + push, "
        f"and update AGENTS.md if needed.\n\n"
        f"RULES:\n"
        f"- Fix ONLY what the finding describes — don't go beyond scope.\n"
        f"- For AGENTS.md edits: apply the exact OLD_TEXT to NEW_TEXT replacement.\n"
        f"- For config drift (k3s, dotfiles, notes): sync the live config to tracked copies.\n"
        f"- For resource issues: prune old files, clean up disk.\n"
        f"- For agent fleet issues: restart failed services, fix timers.\n"
        f"- Skip findings that would require upgrading production infrastructure "
        f"(k3s, Docker daemon, etc.) — mark those as 'deferred'.\n"
        f"- Commit each fix with a clear message referencing the audit section.\n"
        f"- Return a fenced ```json packet with your results.\n\n"
        f"FINDINGS:\n{findings_text}\n\n"
        f'Return JSON:\n'
        f'{{"fixes_applied": [{{"finding": "...", "action": "...", '
        f'"commit": "hash or N/A", "status": "fixed"|"deferred"|"failed"}}], '
        f'"summary": "one sentence"}}'
    )
    try:
        fix_output = _call_omp_p(fix_prompt, timeout=600)
        fix_packet = _extract_json(fix_output, f"fix-{section_name}")
    except Exception as e:
        fix_packet = {"fixes_applied": [], "summary": str(e), "error": str(e)}

    # Judge review of all fixes
    fixes_json = json.dumps(fix_packet.get("fixes_applied", []), indent=2)
    judge_prompt = (
        f"Review these automated fixes for audit section '{section_name}'.\n\n"
        f"For each fix, verify it was applied correctly by checking the actual "
        f"files/state. Flag any fix that was incorrect, incomplete, or overreaching.\n\n"
        f"FIXES APPLIED:\n{fixes_json}\n\n"
        f'Return JSON:\n'
        f'{{"verdict": "pass"|"partial"|"fail", '
        f'"reviewed": [{{"finding": "...", "ok": true|false, "note": "..."}}], '
        f'"summary": "one sentence"}}'
    )
    try:
        judge_output = _call_omp_p(judge_prompt, timeout=600)
        judge_packet = _extract_json(judge_output, f"judge-fix-{section_name}")
    except Exception as e:
        judge_packet = {"verdict": "fail", "reviewed": [], "summary": str(e)}

    return {
        "section": section_name,
        "status": "fixed",
        "findings_count": len(confirmed_findings),
        "actionable_count": len(actionable),
        "fixes_applied": fix_packet.get("fixes_applied", []),
        "fix_summary": fix_packet.get("summary", ""),
        "judge_verdict": judge_packet.get("verdict", "unknown"),
        "judge_summary": judge_packet.get("summary", ""),
        "judge_reviewed": judge_packet.get("reviewed", []),
    }


def phase_7b_fix(run_dir, dry_run=False):
    """Phase 7b: auto-fix confirmed audit findings, with judge review."""
    print("[P7b] auto-fix")
    audit_path = run_dir / "07-audit.json"
    if not audit_path.exists():
        print("  skipped — no audit data")
        write_json(run_dir / "07b-fixes.json", {"sections": [], "status": "no_audit"})
        return

    audit = read_json(audit_path)
    sections = audit.get("sections", [])

    # Collect sections with confirmed findings
    to_fix = []
    for s in sections:
        confirmed = s.get("judge_confirmed", [])
        if not confirmed:
            continue
        to_fix.append((s["name"], confirmed))

    if not to_fix:
        print("  skipped — no confirmed findings to fix")
        write_json(run_dir / "07b-fixes.json", {"sections": [], "status": "nothing_to_fix"})
        return

    print(f"  fixing {len(to_fix)} sections (max_workers={MAX_WORKERS})")
    fix_results = []

    if dry_run:
        for name, findings in to_fix:
            r = _fix_one_section(name, findings, dry_run=True)
            fix_results.append(r)
            print(f"    {name}: DRY RUN — {len(findings)} findings would be fixed")
    else:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {
                pool.submit(_fix_one_section, name, findings, False): name
                for name, findings in to_fix
            }
            for fut in as_completed(futures):
                name = futures[fut]
                try:
                    r = fut.result()
                except Exception as e:
                    r = {"section": name, "status": "fix-failed", "error": str(e)}
                fix_results.append(r)
                applied = len(r.get("fixes_applied", []))
                jv = r.get("judge_verdict", "?")
                print(f"    {name}: {r['status']} — {applied} fixes, judge: {jv}")

    # Sort to canonical section order
    order = {s["name"]: i for i, s in enumerate(AUDIT_SECTIONS)}
    fix_results.sort(key=lambda r: order.get(r.get("section", ""), 99))

    master = {"sections": fix_results, "status": "done"}
    write_json(run_dir / "07b-fixes.json", master)

    total_fixes = sum(len(r.get("fixes_applied", [])) for r in fix_results)
    judge_oks = sum(1 for r in fix_results if r.get("judge_verdict") == "pass")
    print(f"[P7b] done -> {run_dir / '07b-fixes.json'} "
          f"({total_fixes} fixes across {len(fix_results)} sections, {judge_oks} judge-pass)")
    return master

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
        if name == "k3s_pods":
            bad = c.get("bad_pods", [])
            if bad:
                lines.append(f'<p style="margin:0 0 4px; color:#f57f17; font-size:13px;">'
                             f'k3s: {len(bad)} pods not Running/Completed</p>')
            else:
                lines.append(f'<p style="margin:0 0 4px; color:#2e7d32; font-size:13px;">'
                             f'k3s pods: all healthy</p>')
        elif name == "llm_fallback":
            fb = c.get("fallback_active", False)
            lines.append(f'<p style="margin:0 0 4px; color:#{"f57f17" if fb else "2e7d32"}; font-size:13px;">'
                         f'LLM: {"CLOUD FALLBACK" if fb else "local"}</p>')
        elif name == "openwebui_image_match":
            st = c.get("status", "?")
            color = {"ok": "#2e7d32", "warning": "#f57f17", "error": "#c62828"}.get(st, "#555")
            lines.append(f'<p style="margin:0 0 4px; color:{color}; font-size:13px;">'
                         f'open-webui image: {st}</p>')
        elif name.startswith("endpoint_"):
            svc = name.replace("endpoint_", "")
            ok = c.get("status") == "ok"
            icon = "OK" if ok else "FAIL"
            color = "#2e7d32" if ok else "#c62828"
            # Tunnel health: show connector count instead of HTTP code
            if svc == "tunnel-health":
                detail = f'{c.get("active_connections", "?")} connectors'
            else:
                code = c.get("http_code", "?")
                detail = f'HTTP {code}'
            lines.append(f'<p style="margin:0 0 4px; color:{color}; font-size:13px;">'
                         f'{icon} {svc} — {detail}</p>')
        elif name == "docker_containers":
            out = c.get("output", "")
            if out:
                lines.append(f'<p style="margin:0 0 4px; color:#2e7d32; font-size:13px;">'
                             f'containers: all running</p>')
    return "\n".join(lines)




def _trend_bar(values, segments=14, color="#5b3cc4"):
    """Email-safe trend visualization: a row of cells whose shade ramps with value.
    Uses a nested table (no SVG, no flexbox) so it renders in every mail client."""
    nums = [float(v) for v in (values or []) if v is not None]
    if not nums:
        return '<span style="color:#9aa0b2; font-size:11px;">no data</span>'
    mn, mx = min(nums), max(nums)
    shades = ["#eef0f6", "#d8def0", "#bcc7e8", "#9daee0",
              "#7c91d4", "#5b71c4", "#3d4fa8", "#2a2e78"]
    cell_w = 100.0 / segments
    n = len(nums)
    cells = []
    for i in range(segments):
        idx = int(i * n / segments)
        if idx >= n:
            idx = n - 1
        v = nums[idx]
        if mx == mn:
            si = len(shades) // 2 if mn > 0 else 0
        else:
            si = int(round((v - mn) / (mx - mn) * (len(shades) - 1)))
        shade = shades[si]
        cells.append(
            f'<td width="{cell_w:.2f}%" style="background-color:{shade}; '
            f'height:8px; line-height:8px; font-size:0;" align="left">&nbsp;</td>'
        )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="border-collapse:collapse;"><tr>' + "".join(cells) + '</tr></table>'
    )


def _fmt_num(v):
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return f"{f:g}"


def _html_heartbeat(hb_data, sparklines=None):
    """Render the heartbeat as grouped, scannable HTML instead of a wall of <p>."""
    out = []

    def append_group(header, rows):
        if not rows:
            return
        out.append(_sub_header(header))
        out.append(_kv_rows(rows))

    # ── Systemd units + reboot ──
    sys_rows = []
    uf = hb_data.get("failed_units", {}) or {}
    user_f = [x for x in uf.get("user", []) if x and x.strip()]
    sys_f = [x for x in uf.get("system", []) if x and x.strip()]
    missing = (hb_data.get("units", {}) or {}).get("missing", [])
    if not user_f and not sys_f and not missing:
        sys_rows.append(("Systemd units", _dot("ok") + "All units healthy"))
    else:
        for u in user_f:
            sys_rows.append(("Failed (user)", _dot("danger") + u.strip()))
        for u in sys_f:
            sys_rows.append(("Failed (system)", _dot("danger") + u.strip()))
        if missing:
            sys_rows.append(("Missing units", _dot("warn") + ", ".join(missing)))
    rb = hb_data.get("reboot", {}) or {}
    if rb.get("needed"):
        sys_rows.append(("Reboot", _dot("danger") + f'Needed — kernel {rb.get("kernel","?")}'.strip()))
    else:
        sys_rows.append(("Reboot", _dot("ok") + "Not needed"))
    append_group("System health", sys_rows)

    # ── Resources ──
    res_rows = []
    mem = hb_data.get("memory", {}) or {}
    mem_avail = mem.get("available", "")
    if mem_avail:
        pressure = ""
        mp = mem.get("pressure", "")
        m = re.search(r"some avg10=([\d.]+).*full avg10=([\d.]+)", mp) if mp else None
        if m:
            pressure = f' · pressure {m.group(1)}/{m.group(2)}'
        res_rows.append(("Memory", f'{mem_avail} available{pressure}'))
    disk_parts = []
    disk = hb_data.get("disk", {}) or {}
    if disk.get("df_root"):
        parts = disk["df_root"].splitlines()[-1].split()
        if len(parts) >= 5:
            disk_parts.append(f'{parts[4]} used ({parts[2]}/{parts[1]})')
    journal = hb_data.get("journal_disk_usage", "")
    if journal:
        jm = re.search(r"take up (\S+)", journal)
        if jm:
            disk_parts.append(f'journal {jm.group(1)}')
    if disk_parts:
        res_rows.append(("Disk · journal", " · ".join(disk_parts)))
    smart = hb_data.get("smart", {}) or {}
    if smart.get("wear_pct") or smart.get("available_spare"):
        res_rows.append(("NVMe SMART", _dot("ok") +
                         f'{smart.get("wear_pct","?")} wear · '
                         f'{smart.get("available_spare","?")} spare · '
                         f'{smart.get("media_errors","?")} media errors'))
    elif smart.get("status") == "skipped":
        pass
    append_group("System resources", res_rows)

    # ── Network & services ──
    net_rows = []
    nodes = hb_data.get("k3s_nodes", [])
    for n in nodes:
        if "NAME" in n and "STATUS" in n:
            continue
        parts = n.split()
        if len(parts) >= 2:
            net_rows.append(("k3s node",
                f'{parts[0]} <span style="color:#2e7d32; font-weight:600;">{parts[1]}</span>'))
            break
    fb = (hb_data.get("llm_stack", {}) or {}).get("falling_back", False)
    net_rows.append(("LLM proxy",
                     _dot("warn") + "Cloud fallback" if fb else _dot("ok") + "Local"))
    bt = (hb_data.get("backup", {}) or {}).get("last_run", "")
    if bt:
        net_rows.append(("Last backup", bt))
    dns = hb_data.get("dns", {}) or {}
    if dns:
        ok = sum(1 for v in dns.values() if v.get("resolves"))
        total = len(dns)
        level = "ok" if ok == total else ("warn" if ok > 0 else "danger")
        net_rows.append(("DNS", _dot(level) + f'{ok}/{total} hostnames resolve'))
    hosts = hb_data.get("hosts", {}) or {}
    for hostname, info in hosts.items():
        level = "ok" if info.get("resolves") else "danger"
        ip = info.get("output", "").split()[0] if info.get("output") else "?"
        net_rows.append((f'/etc/hosts {hostname}', _dot(level) + ip))
    dur = hb_data.get("docker_user_rules", {}) or {}
    if dur:
        if dur.get("has_drop_default"):
            net_rows.append(("ufw docker-user", _dot("ok") + "DROP present"))
        else:
            net_rows.append(("ufw docker-user", _dot("danger") + "MISSING DROP"))
    append_group("Network & services", net_rows)

    # ── Security ──
    sec_rows = []
    tls = hb_data.get("tls_certs", {}) or {}
    if tls:
        tls_parts = []
        for host, expiry in tls.items():
            dm = re.search(r"notAfter=(.+?\d{4})\s", expiry)
            date_str = dm.group(1) if dm else expiry[:20]
            tls_parts.append(
                f'<code style="font-family:Menlo,Consolas,monospace; font-size:11px; '
                f'color:#2a2a36;">{host.split(".")[0]} {date_str}</code>')
        sec_rows.append(("TLS certificates", "  ·  ".join(tls_parts)))
    ba = hb_data.get("bundle_audit", {}) or {}
    if ba:
        ba_parts = []
        for app, result in ba.items():
            level = "ok" if "no vulnerabilities" in str(result) else "warn"
            ba_parts.append(f"{app} {_dot(level)}")
        sec_rows.append(("bundle-audit", "  ·  ".join(ba_parts)))
    append_group("Security", sec_rows)

    # ── Config drift ──
    sd = hb_data.get("self_drift", {}) or {}
    drift_rows = []
    for section, data in sd.items():
        if isinstance(data, dict):
            issues = sum(1 for v in data.values()
                         if v and isinstance(v, list) and len(v) > 0)
            if issues:
                drift_rows.append((section.replace("_", " "),
                    _dot("warn") + f'{issues} drift item{"s" if issues != 1 else ""}'))
    append_group("Config drift", drift_rows)

    # ── 30-day trends ──
    if sparklines:
        out.append(_sub_header("30-day trends"))
        trends = ['<table role="presentation" width="100%" cellpadding="0" '
                  'cellspacing="0" style="font-size:12px; border-collapse:collapse;">']
        for label, values in sparklines:
            nums = [float(v) for v in (values or []) if v is not None]
            latest = _fmt_num(nums[-1] if nums else None)
            peak = _fmt_num(max(nums) if nums else None)
            bar = _trend_bar(nums) if nums else '<span style="color:#9aa0b2; font-size:11px;">no data</span>'
            trends.append(
                '<tr>'
                f'<td width="42%" style="padding:4px 0 2px; color:#7b7b8a; vertical-align:middle;">{label}</td>'
                f'<td align="right" style="padding:4px 10px 2px 0; color:#2a2a36; font-weight:600; vertical-align:middle;">{latest}</td>'
                f'<td align="right" style="padding:4px 0 2px; color:#9aa0b2; font-size:11px; vertical-align:middle;">peak {peak}</td>'
                '</tr>'
                f'<tr><td colspan="3" style="padding:0 0 8px;">{bar}</td></tr>'
            )
        trends.append('</table>')
        out.append("".join(trends))

    return "".join(out)


def _mini_bar(pct, color="#37474f"):
    """Inline 0-100% horizontal bar, email-safe via nested table cells."""
    try:
        w = max(0.0, min(100.0, float(pct)))
    except (TypeError, ValueError):
        w = 0.0
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" width="100%" '
        'style="border-collapse:collapse;"><tr>'
        f'<td width="{w:.0f}%" style="background-color:{color}; height:5px; '
        f'line-height:5px; font-size:0;">&nbsp;</td>'
        f'<td width="{100-w:.0f}%" style="background-color:#ececf2; height:5px; '
        f'line-height:5px; font-size:0;">&nbsp;</td>'
        '</tr></table>'
    )


def _html_audit(audit_data):
    """Render audit sections as compact per-section cards."""
    sections = audit_data.get("sections", []) or []
    if not sections:
        return '<p style="margin:0; color:#9aa0b2; font-size:13px;">No audit results.</p>'
    out = []
    for sec in sections:
        name = (sec.get("name", "unknown") or "unknown").replace("_", " ")
        verdict = sec.get("verdict", "UNKNOWN")
        badge = _badge(verdict)
        confirmed = sec.get("judge_confirmed", []) or sec.get("confirmed_findings", []) or []
        rejected = sec.get("judge_rejected", []) or []
        n_confirmed = len(confirmed)
        n_rejected = len(rejected)
        summary = f'{n_confirmed} finding{"s" if n_confirmed != 1 else ""}'
        if n_rejected:
            summary += f' · {n_rejected} rejected'
        out.append(
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            'style="font-size:13px; border-collapse:collapse; margin:0 0 12px;">'
            f'<tr><td style="padding:3px 0; color:#1a1a2e; font-weight:700;">{name}</td>'
            f'<td align="right" style="padding:3px 0; white-space:nowrap;">{badge}</td></tr>'
            f'<tr><td colspan="2" style="padding:0 0 4px; color:#9aa0b2; '
            f'font-size:11px;">{summary}</td></tr>'
        )
        for finding in confirmed[:3]:
            claim = (finding.get("claim") or finding.get("evidence") or "").strip()
            out.append(
                f'<tr><td colspan="2" style="padding:2px 0 2px 14px; color:#3a3a4a; '
                f'font-size:12px; border-left:2px solid #ececf2;">{claim[:180]}</td></tr>'
            )
        if n_confirmed > 3:
            out.append(
                f'<tr><td colspan="2" style="padding:2px 0 2px 14px; color:#9aa0b2; '
                f'font-size:11px;">+ {n_confirmed - 3} more</td></tr>'
            )
        for r in rejected[:2]:
            claim = (r.get("claim") or "").strip()
            reason = (r.get("reason") or "").strip()
            out.append(
                f'<tr><td colspan="2" style="padding:2px 0 1px 14px; color:#9aa0b2; '
                f'font-size:12px; text-decoration:line-through; '
                f'border-left:2px solid #ececf2;">{claim[:140]}</td></tr>'
                f'<tr><td colspan="2" style="padding:0 0 3px 14px; color:#9aa0b2; '
                f'font-size:11px;">Judge: {reason[:120]}</td></tr>'
            )
        if n_rejected > 2:
            out.append(
                f'<tr><td colspan="2" style="padding:2px 0 1px 14px; color:#9aa0b2; '
                f'font-size:11px;">+ {n_rejected - 2} more rejected</td></tr>'
            )
        out.append('</table>')
    return "".join(out)


def _html_queue(queue_data):
    """Render work queue as clean grouped rows with status chips."""
    ideas = queue_data.get("ideas", {}) or {}
    plans = queue_data.get("plans", {}) or {}
    inconsistencies = queue_data.get("inconsistencies", []) or []
    out = []
    out.append(
        f'<p style="margin:0 0 6px; font-size:13px;">'
        f'<strong style="color:#1a1a2e;">Ideas outstanding:</strong> '
        f'{ideas.get("total_outstanding", 0)}</p>'
    )
    for idea in ideas.get("outstanding", [])[:10]:
        out.append(
            f'<p style="margin:0 0 2px 14px; color:#3a3a4a; font-size:12px; '
            f'border-left:2px solid #ececf2; padding-left:8px;">'
            f'{idea["file"]} <span style="color:#9aa0b2;">({idea["age_days"]}d)</span> '
            f'— {idea["heading"][:80]}</p>'
        )

    def _plan_row(chip_text, color, detail):
        return (
            f'<p style="margin:0 0 2px 14px; font-size:12px; padding-left:8px; '
            f'border-left:2px solid {color};">{_chip(chip_text, color)} '
            f'<span style="color:#3a3a4a;">{detail}</span></p>'
        )

    for plan in plans.get("draft", []):
        out.append(_plan_row("DRAFT", "#1565c0",
                              f'{plan["file"]} — {plan["heading"][:80]}'))
    for plan in plans.get("approved", []):
        out.append(_plan_row("APPROVED", "#2e7d32",
                              f'{plan["file"]} (priority {plan["priority"]})'))
    for plan in plans.get("implementing", []):
        out.append(_plan_row("IMPLEMENTING", "#e65100",
                              f'{plan["file"]} ({plan["age_days"]}d)'))
    for plan in plans.get("done_this_week", []):
        out.append(_plan_row("DONE", "#9aa0b2", plan["file"]))

    if inconsistencies:
        out.append(
            '<p style="margin:10px 0 2px; color:#1a1a2e; font-size:11px; '
            'font-weight:700; letter-spacing:0.6px; '
            'text-transform:uppercase;">Inconsistencies</p>'
        )
        for inc in inconsistencies:
            out.append(
                f'<p style="margin:0 0 2px 14px; color:#c62828; font-size:12px; '
                f'border-left:2px solid #c62828; padding-left:8px;">'
                f'{inc["type"]}: {inc["detail"][:200]}</p>'
            )

    candidate = queue_data.get("executor_candidate")
    cap = queue_data.get("executor_monthly_cap", 4)
    used = queue_data.get("executor_monthly_used", 0)
    if candidate:
        out.append(
            f'<p style="margin:10px 0 0; color:#00838f; font-size:12px;">'
            f'<strong>Next executor candidate:</strong> {candidate["file"]} '
            f'<span style="color:#9aa0b2;">(monthly {used}/{cap})</span></p>'
        )
    return "".join(out) if out else \
        '<p style="margin:0; color:#9aa0b2; font-size:13px;">Queue empty.</p>'


def _html_executor(exec_data):
    """Render executor result as HTML."""
    if not exec_data.get("executed"):
        reason = exec_data.get("reason", "no plan")
        return f'<p style="margin:0; color:#9aa0b2; font-size:13px;">Idle — {reason}</p>'
    status = exec_data.get("status", "unknown")
    plan = exec_data.get("plan", "?")
    packet = exec_data.get("executor_packet", {}) or {}
    review = exec_data.get("review_packet", {}) or {}
    status_color = {"done": "#2e7d32", "partial": "#e65100",
                    "failed": "#c62828"}.get(status, "#9aa0b2")
    out = [
        f'<p style="margin:0 0 4px; font-size:13px;">{_chip(status.upper(), status_color)} '
        f'<strong style="color:#1a1a2e; margin-left:6px;">{plan}</strong></p>',
        f'<p style="margin:0 0 6px; color:#3a3a4a; font-size:13px;">'
        f'Summary: {str(packet.get("summary", "N/A"))[:300]}</p>',
    ]
    commits = packet.get("commits", []) or []
    if commits:
        out.append(
            '<p style="margin:0 0 2px; color:#9aa0b2; font-size:10px; font-weight:700; '
            'letter-spacing:0.8px; text-transform:uppercase;">Commits</p>'
        )
        for c in commits[:5]:
            out.append(
                f'<p style="margin:0 0 2px 14px; color:#3a3a4a; font-size:12px; '
                f'font-family:Menlo,Consolas,monospace;">{str(c)[:120]}</p>'
            )
    if review:
        rev = (review.get("verdict") or "?")
        rc = "#2e7d32" if rev == "pass" else "#c62828"
        out.append(
            f'<p style="margin:6px 0 0; font-size:13px;">'
            f'{_chip("REVIEW " + rev.upper(), rc)}</p>'
        )
    return "".join(out)


def _html_fixes(fixes_data):
    """Render auto-fix results as HTML."""
    sections = fixes_data.get("sections", []) or []
    if not sections:
        return '<p style="margin:0; color:#9aa0b2; font-size:13px;">No fixes applied.</p>'
    out = []
    for s in sections:
        status = s.get("status", "?")
        if status == "dry-run":
            out.append(
                f'<p style="margin:0 0 8px; font-size:13px;">{_chip("DRY-RUN", "#9aa0b2")} '
                f'<strong style="color:#1a1a2e; margin-left:6px;">{s["section"]}</strong> '
                f'<span style="color:#9aa0b2; font-size:12px;">'
                f'{s.get("findings_count", 0)} findings</span></p>'
            )
            continue
        if status == "skipped":
            out.append(
                f'<p style="margin:0 0 8px; font-size:13px;">{_chip("SKIPPED", "#9aa0b2")} '
                f'<strong style="color:#1a1a2e; margin-left:6px;">{s["section"]}</strong> '
                f'<span style="color:#9aa0b2; font-size:12px;">{s.get("reason","")}</span></p>'
            )
            continue
        jv = s.get("judge_verdict", "?")
        jv_color = {"pass": "#2e7d32", "partial": "#e65100",
                    "fail": "#c62828"}.get(jv, "#9aa0b2")
        fixes = s.get("fixes_applied", []) or []
        out.append(
            f'<p style="margin:0 0 4px; font-size:13px;">'
            f'<strong style="color:#1a1a2e;">{s["section"]}</strong> '
            f'{len(fixes)} fixes {_chip("JUDGE " + jv.upper(), jv_color)}</p>'
        )
        for f in fixes:
            st = f.get("status", "?")
            st_color = {"fixed": "#2e7d32", "deferred": "#e65100",
                        "failed": "#c62828"}.get(st, "#9aa0b2")
            finding_txt = (f.get("finding", "") or "")[:120]
            action_txt = (f.get("action", "") or "")[:80]
            out.append(
                f'<p style="margin:0 0 2px 14px; font-size:12px; '
                f'border-left:2px solid {st_color}; padding-left:8px;">'
                f'{_chip(st.upper(), st_color)} '
                f'<span style="color:#3a3a4a;">{finding_txt}</span> '
                f'<span style="color:#9aa0b2;">{action_txt}</span></p>'
            )
    return "".join(out)


def _html_usage(usage_data):
    """Render OpenCode Go usage report with mini-bars per utilization metric."""
    accounts = usage_data.get("accounts", []) or []
    out = []
    for acct in accounts:
        name = acct.get("name", "?")
        tier = acct.get("tier", "?")
        extra = ""
        if acct.get("payg_balance") is not None:
            extra = f' · PAYG ${acct["payg_balance"]:.2f} remaining'
        out.append(
            f'<p style="margin:0 0 3px; font-size:13px;">'
            f'<strong style="color:#1a1a2e;">{name}</strong> '
            f'<span style="color:#9aa0b2; font-size:12px;">({tier}){extra}</span></p>'
        )
        rows = (
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            'style="font-size:12px; border-collapse:collapse; margin-bottom:10px;">'
        )
        for label, key, color in [
            ("Rolling 24h", "rolling_pct", "#37474f"),
            ("Weekly", "weekly_pct", "#1565c0"),
            ("Monthly", "monthly_pct", "#5b3cc4"),
        ]:
            pct = acct.get(key, 0)
            try:
                pct = float(pct)
            except (TypeError, ValueError):
                pct = 0.0
            rows += (
                '<tr>'
                f'<td width="22%" style="padding:3px 0; color:#7b7b8a; font-size:12px; '
                f'vertical-align:middle;">{label}</td>'
                f'<td width="68%" style="padding:3px 0; vertical-align:middle;">'
                f'{_mini_bar(pct, color)}</td>'
                f'<td align="right" width="10%" style="padding:3px 0; color:#2a2a36; '
                f'font-weight:600; vertical-align:middle; white-space:nowrap;">'
                f'{pct:.0f}%</td>'
                '</tr>'
            )
        rows += '</table>'
        out.append(rows)
    if usage_data.get("proxy_error"):
        out.append(
            f'<p style="margin:6px 0 0; color:#c62828; font-size:12px;">'
            f'{_dot("danger")}Proxy unreachable: {usage_data["proxy_error"]}</p>'
        )
    if not out:
        out.append('<p style="margin:0; color:#9aa0b2; font-size:13px;">No usage data.</p>')
    return "".join(out)


def phase_8_render_send(run_dir, setup_data, dry_run=False):
    """Phase 8: render HTML from all artifacts and send email."""
    print("[P8] render + send")

    date_str = setup_data["date"]
    usage = setup_data.get("usage", {})

    # Load all phase data
    applied = read_json(run_dir / "01-applied.json") if (run_dir / "01-applied.json").exists() else {"steps": []}
    validation = read_json(run_dir / "02-validation.json") if (run_dir / "02-validation.json").exists() else {"checks": []}
    troubleshoot = read_json(run_dir / "03-troubleshoot.json") if (run_dir / "03-troubleshoot.json").exists() else None
    heartbeat = read_json(run_dir / "04-heartbeat.json") if (run_dir / "04-heartbeat.json").exists() else {}
    queue = read_json(run_dir / "05-queue.json") if (run_dir / "05-queue.json").exists() else {}
    executor = read_json(run_dir / "06-executor.json") if (run_dir / "06-executor.json").exists() else {}
    fixes = read_json(run_dir / "07b-fixes.json") if (run_dir / "07b-fixes.json").exists() else {"sections": []}
    audit = read_json(run_dir / "07-audit.json") if (run_dir / "07-audit.json").exists() else {"sections": []}

    # Load sparkline data from runs log (last 30 entries)
    sparklines = []
    if RUNS_LOG.exists():
        try:
            raw_lines = RUNS_LOG.read_text().strip().splitlines()
            entries = [json.loads(l) for l in raw_lines[-30:] if l.strip()]
            series = {
                "duration_s": [],
                "applied": [],
                "sections_fired": [],
                "judge_rejections": [],
                "usage_accounts": [],
            }
            for e in entries:
                for k in series:
                    series[k].append(e.get(k, 0))
            sparklines = [
                ("duration", series["duration_s"]),
                ("updates applied", series["applied"]),
                ("audit sections", series["sections_fired"]),
                ("judge rej", series["judge_rejections"]),
                ("OCG accts", series["usage_accounts"]),
            ]
        except Exception:
            pass

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
    n_audit_failed = sum(1 for s in audit.get("sections", []) if s.get("verdict", "").endswith("-failed"))
    n_ideas = queue.get("ideas", {}).get("total_outstanding", 0)
    n_plans_approved = len(queue.get("plans", {}).get("approved", []))
    n_fixes = sum(len(s.get("fixes_applied", [])) for s in fixes.get("sections", []))
    exec_status = "idle"
    if executor.get("executed"):
        exec_status = executor.get("status", "done")

    tldr_parts = [f"{n_applied} updates applied"]
    if n_failed_apply:
        tldr_parts.append(f"{n_failed_apply} failed")
    if n_audit_drift:
        tldr_parts.append(f"{n_audit_drift} audit items need attention")
    elif n_audit_failed:
        tldr_parts.append(f"{n_audit_failed} audit sections FAILED")
    else:
        tldr_parts.append("audit clean")
    tldr_parts.append(f"{n_ideas} ideas, {n_plans_approved} plans approved")
    tldr_parts.append(f"executor: {exec_status}")
    if n_fixes:
        tldr_parts.append(f"{n_fixes} fixes applied")
    tldr = " · ".join(tldr_parts) + "."
    if phase_failures:
        tldr += (
            f'<br><span style="display:inline-block; margin-top:6px; padding:2px 8px; '
            f'border-radius:6px; background-color:#c628281f; color:#c62828; '
            f'font-size:12px; font-weight:700;">PHASE FAILURES: '
            f'{", ".join(phase_failures)}</span>'
        )

    # Troubleshoot section
    troubleshoot_html = ""
    if troubleshoot and troubleshoot.get("triggered"):
        ts_status = troubleshoot.get("agent_status", "unknown")
        diagnosis = troubleshoot.get("diagnosis", "")
        actions = troubleshoot.get("actions_taken", [])
        if ts_status == "fixed":
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
        .replace("{{HEARTBEAT}}", _html_heartbeat(heartbeat, sparklines=sparklines))
        .replace("{{AUDIT}}", _html_audit(audit))
        .replace("{{QUEUE}}", _html_queue(queue))
        .replace("{{EXECUTOR}}", _html_executor(executor))
        .replace("{{FIXES}}", _html_fixes(fixes))
        .replace("{{USAGE}}", _html_usage(usage))
        .replace("{{FOOTER}}", footer)
    )

    email_path = run_dir / "08-email.html"
    email_path.write_text(html)
    print(f"[P8] rendered -> {email_path}")

    # Build subject
    n_applied_count = n_applied
    audit_summary = f"{n_audit_drift} drift" if n_audit_drift else (f"{n_audit_failed} failed" if n_audit_failed else "clean")
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
    usage = setup_data.get("usage", {})

    # Load key artifacts for summary
    applied = read_json(run_dir / "01-applied.json") if (run_dir / "01-applied.json").exists() else {}
    validation = read_json(run_dir / "02-validation.json") if (run_dir / "02-validation.json").exists() else {}
    audit = read_json(run_dir / "07-audit.json") if (run_dir / "07-audit.json").exists() else {}
    queue = read_json(run_dir / "05-queue.json") if (run_dir / "05-queue.json").exists() else {}
    executor = read_json(run_dir / "06-executor.json") if (run_dir / "06-executor.json").exists() else {}
    fixes = read_json(run_dir / "07b-fixes.json") if (run_dir / "07b-fixes.json").exists() else {"sections": []}


    # Build summary.md
    lines = [
        f"# Steward Report — {date_str}",
        f"**Engine:** steward_runner.py",
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
    executor = read_json(run_dir / "06-executor.json") if (run_dir / "06-executor.json").exists() else {}
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
        if s.get("verdict") not in ("cached-PASS", "dry-run-collector-only")
    )
    n_judge_rejected = sum(
        len(s.get("judge_rejected", [])) for s in audit.get("sections", [])
    )
    runs_entry = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "duration_s": round(elapsed_s),
        "applied": sum(1 for s in applied.get("steps", []) if s.get("status") in ("ok", "bumped")),
        "usage_accounts": len(usage.get("accounts", [])),
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



# ── P9b: dotfiles hygiene ────────────────────────────────────────────


def phase_9b_dotfiles(run_dir, dry_run=False):
    """Phase 9b: detect dirty dotfiles, classify, commit via agent, judge review."""
    print("[P9b] dotfiles hygiene")

    DOTFILES_GIT = str(HOME / ".dotfiles-homelab")
    ALLOWED_PREFIXES = [
        str(HOME / ".config"),
        str(HOME / ".local" / "bin"),
        str(HOME / ".zshrc"),
        str(HOME / ".omp"),
        str(HOME / "scripts"),
        str(HOME / "open-webui"),
        str(HOME / "searxng"),
        str(HOME / "k3s"),
        str(HOME / ".config" / "systemd" / "user"),
    ]
    ACTIVE_WINDOW_MINUTES = 15

    gate = {
        "active_edit": False,
        "out_of_scope": [],
        "skipped_secret": [],
    }

    # ── 1. Run dotfiles status ────────────────────────────────────
    dotfiles_cmd = [
        "/usr/bin/git", "--git-dir", DOTFILES_GIT,
        "--work-tree", str(HOME), "status", "--short",
    ]
    status_out = run_capture(dotfiles_cmd)

    if not status_out:
        print("  clean — no dirty dotfiles")
        result = {"status": "clean", "gate": gate}
        write_json(run_dir / "09b-dotfiles.json", result)
        return

    # ── 2. Parse changed paths ────────────────────────────────────
    changed = []
    for line in status_out.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # "XY path" — status codes are 2 chars
        if len(stripped) >= 3 and stripped[1:3] == "??":
            path = stripped[3:].strip()
        elif len(stripped) >= 3 and stripped[2] == " ":
            path = stripped[3:].strip()
        else:
            # Fallback: split on first space-after-status
            parts = stripped.split(None, 1)
            path = parts[1] if len(parts) > 1 else stripped
        path = path.strip().strip('"').strip("'")
        if path:
            changed.append(path)

    if not changed:
        print("  no changed paths parsed")
        result = {"status": "clean", "gate": gate}
        write_json(run_dir / "09b-dotfiles.json", result)
        return

    print(f"  dirty paths: {len(changed)}")

    # ── 3. Active-edit guard ──────────────────────────────────────
    now = datetime.now()
    youngest = None
    for p in changed:
        full = HOME / p
        try:
            mtime = datetime.fromtimestamp(full.stat().st_mtime)
            age_min = (now - mtime).total_seconds() / 60.0
            if youngest is None or mtime > youngest:
                youngest = mtime
            if age_min < ACTIVE_WINDOW_MINUTES:
                gate["active_edit"] = True
        except (FileNotFoundError, OSError):
            # Path doesn't exist on disk — skip active-edit check for it
            continue

    if gate["active_edit"]:
        print(f"  SKIPPED — active edit window ({ACTIVE_WINDOW_MINUTES} min)")
        result = {
            "status": "skipped",
            "reason": "active_edit_window",
            "youngest_mtime": youngest.isoformat() if youngest else None,
            "gate": gate,
        }
        write_json(run_dir / "09b-dotfiles.json", result)
        return

    # ── 4. Path-sanity gate ───────────────────────────────────────
    in_scope = []
    for p in changed:
        full = str(HOME / p)
        allowed = any(full.startswith(prefix + "/") or full == prefix
                      for prefix in ALLOWED_PREFIXES)
        if not allowed:
            gate["out_of_scope"].append(p)
            print(f"    out_of_scope: {p}")
        else:
            in_scope.append(p)

    # ── 5. Secret gate ────────────────────────────────────────────
    clean_paths = []
    for p in in_scope:
        if any(pat.match(p) or pat.match(Path(p).name) for pat in SECRET_PATTERNS):
            gate["skipped_secret"].append(p)
            print(f"    skipped_secret: {p}")
        else:
            clean_paths.append(p)

    if not clean_paths:
        print("  no in-scope paths after filtering")
        result = {
            "status": "clean",
            "gate": gate,
            "filtered_all": True,
            "reason": "all paths out of scope or secret",
        }
        write_json(run_dir / "09b-dotfiles.json", result)
        return

    print(f"  in-scope: {len(clean_paths)} paths")

    # ── Dry-run bailout ───────────────────────────────────────────
    if dry_run:
        print("  DRY RUN — would commit:")
        for p in clean_paths:
            print(f"    {p}")
        result = {
            "status": "dry_run",
            "gate": gate,
            "would_commit": clean_paths,
        }
        write_json(run_dir / "09b-dotfiles.json", result)
        return

    # ── 6. Agent commit ───────────────────────────────────────────
    skipped_list = gate["out_of_scope"] + gate["skipped_secret"]
    path_list = "\n".join(f"- {p}" for p in clean_paths)
    skip_list = "\n".join(f"- {p}" for p in skipped_list) if skipped_list else "(none)"

    agent_prompt = f"""You are committing dirty dotfiles on Carter's homelab. The dirty paths are:
{path_list}

Rules:
- Use `dotfiles` (alias: /usr/bin/git --git-dir=$HOME/.dotfiles-homelab
  --work-tree=$HOME). NEVER bare `dotfiles add -A` or `dotfiles add .` —
  AGENTS.md rule. Use targeted `dotfiles add <path>` per logical commit.
- Group changes into one or more logical commits with conventional-style messages
  ("feat: ...", "fix: ...", "chore: ...", "refactor: ..."). Group by concern.
- Read each changed file's diff to decide grouping (`dotfiles diff <path>`).
- Do NOT stage any file in the skip-list: [{', '.join(skipped_list)}].
- `dotfiles push` exactly once after all commits succeed.
- Return the fenced JSON:
  {{"commits": [{{"message": "...", "files": [...]}}], "pushed": true|false,
   "skipped": [{{"path": "...", "reason": "..."}}]}}"""

    print("  spawning dotfiles commit agent …")
    agent_raw = _call_omp_p(agent_prompt, model=SMALL_MODEL, timeout=600)
    try:
        agent_json = _extract_json(agent_raw, "dotfiles agent")
    except ValueError as e:
        print(f"  agent JSON extraction failed: {e}")
        result = {
            "status": "agent_failed",
            "gate": gate,
            "agent": {"raw_output": agent_raw[:2000], "error": str(e)},
        }
        write_json(run_dir / "09b-dotfiles.json", result)
        return

    commits = agent_json.get("commits", [])
    pushed = agent_json.get("pushed", False)
    print(f"  agent: {len(commits)} commits, pushed={pushed}")

    # ── 7. Judge review ──────────────────────────────────────────
    log_cmd = [
        "/usr/bin/git", "--git-dir", DOTFILES_GIT,
        "--work-tree", str(HOME), "log", "-5", "--oneline",
    ]
    dotfiles_log = run_capture(log_cmd)

    judge_prompt = f"""You are reviewing dotfiles commits made by another agent on Carter's homelab.
The agent reported these commits: {json.dumps(agent_json, indent=2)}
Actual dotfiles log (last 5): {dotfiles_log}

Verify:
(a) push succeeded (check dotfiles log shows the commits)
    (b) no secret-bearing file was committed (check file list against: api-token, .env, .env.*, master.key, auth.json, .pem, id_rsa, id_ed25519, .ovpn, credentials.json, .htpasswd)
(c) every dirty in-scope path is either committed or in the skipped list with a real reason

Return fenced JSON:
{{"verdict": "confirmed"|"rejected", "issues": ["..."], "confirmed_commits": [...]}}"""

    print("  spawning judge review …")
    judge_raw = _call_omp_p(judge_prompt, model=SMALL_MODEL, timeout=300)
    try:
        judge_json = _extract_json(judge_raw, "dotfiles judge")
    except ValueError as e:
        print(f"  judge JSON extraction failed: {e}")
        judge_json = {"verdict": "judge_parse_error", "issues": [str(e)],
                       "raw_output": judge_raw[:2000]}

    verdict = judge_json.get("verdict", "unknown")
    issues = judge_json.get("issues", [])
    print(f"  judge: {verdict}" + (f" ({len(issues)} issues)" if issues else ""))

    # ── 8. Output ────────────────────────────────────────────────
    result = {
        "status": "committed" if (commits and pushed) else "agent_partial",
        "gate": gate,
        "agent": {"commits": commits, "pushed": pushed, "raw_output": agent_raw[:3000]},
        "judge": {"verdict": verdict, "issues": issues},
    }
    write_json(run_dir / "09b-dotfiles.json", result)
    print(f"[P9b] done -> {run_dir / '09b-dotfiles.json'}")

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

    # P3a: deterministic auto-remediation
    try:
        if should_run("03a-remediation.json"):
            phase_3a_remediation(run_dir, dry_run=args.dry_run)
        else:
            print("[P3a] skipped (resume)")
    except Exception as e:
        print(f"[P3a] FAILED: {e}")
        write_json(run_dir / "03a-remediation.json",
                   {"phase_failed": True, "error": str(e)})

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
    # P7b: auto-fix
    try:
        phase_7b_fix(run_dir, dry_run=args.dry_run)
    except Exception as e:
        print(f"[P7b] FAILED: {e}")
        write_json(run_dir / "07b-fixes.json",
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

    # P9b: dotfiles hygiene
    try:
        phase_9b_dotfiles(run_dir, dry_run=args.dry_run)
    except Exception as e:
        print(f"[P9b] FAILED: {e}")
        write_json(run_dir / "09b-dotfiles.json",
                   {"status": "phase_failed", "error": str(e)})

    # Restart dependabot-webhook (stopped in P0)
    dep = setup.get("dependabot", {})
    if dep.get("stopped") and not args.dry_run:
        try:
            run(["systemctl", "--user", "start", DEPENDABOT_UNIT], env=user_env())
            print("[cleanup] dependabot-webhook restarted")
        except Exception as e:
            print(f"[cleanup] dependabot restart failed: {e}")

    print(f"\nDone in {elapsed:.0f}s")
if __name__ == "__main__":
    main()
