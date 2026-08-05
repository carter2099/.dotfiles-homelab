#!/usr/bin/env python3
"""
Homelab Steward — nightly deterministic Python orchestrator.
Replaces update-check + agents-md-audit. Adds work queue.

Scheduled via homelab-steward.timer. Every phase writes a numbered artifact;
skip-if-exists resume; failures become email badges, never sys.exit mid-run.
"""

import argparse
import hashlib
import html
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
STEWARD_MODEL = "opencode-go/deepseek-v4-pro"
STEWARD_PATH = "/home/carter/.rbenv/shims:/home/carter/.rbenv/versions/4.0.6/bin:/home/carter/.local/bin:/home/carter/.bun/bin:/home/carter/.local/share/fnm:/home/carter/go/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# Resolve fnm default node bin for PATH (if available)
_FNM_NODE_DIRS = sorted(
    (d for d in (HOME / ".local/share/fnm/node-versions").glob("*/installation/bin")
     if (d / "node").exists()),
    key=lambda d: d.stat().st_mtime if d.exists() else 0,
    reverse=True)
if _FNM_NODE_DIRS:
    STEWARD_PATH = f"{_FNM_NODE_DIRS[0]}:{STEWARD_PATH}"
SMALL_MODEL = "opencode-go/deepseek-v4-flash"
PROXY_HEALTH = "http://localhost:8082/health"
MAX_WORKERS = 3
# P7b fix↔judge loop: re-fix judge-not-ok items up to N times (env override).
FIX_MAX_ITERS = max(1, int(os.environ.get("STEWARD_FIX_MAX_ITERS", "3")))

# Timeout and model for headless omp JSON calls
OMP_JSON_TIMEOUT = 2700
OMP_JSON_MODEL = "opencode-go/deepseek-v4-pro"
PENDING_PATH = HOME / "agent-state" / "pending.md"
DEPENDABOT_UNIT = "dependabot-webhook.service"

# Session memory (P0b) — interactive omp sessions documented into the notes vault
SESSION_INTERACTIVE_DIR = HOME / ".omp" / "agent" / "sessions"   # interactive: project-scoped subdirs
SESSION_MEMOIR_DIR = HOME / "notes" / "logs" / "sessions"        # memory bank: YYYY-MM-DD/<HHMM>-<slug>.md
SESSION_ACTIVE_MINUTES = 15        # skip sessions still being written (document next night)
SESSION_MEMORY_CONTEXT_DAYS = 2    # P7 workers/judges + TL;DR context window
SESSION_MEMORY_CONTEXT_MAX = 8000  # chars, bounded


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
<!-- Health -->
<tr><td style="padding:18px 32px 0;">
<h2 style="margin:0; padding-left:10px; border-left:3px solid #5b3cc4; color:#1a1a2e; font-size:13px; font-weight:700; letter-spacing:0.6px; text-transform:uppercase;">Health</h2>
</td></tr>
<tr><td style="padding:8px 32px 14px;">{{HEALTH}}</td></tr>
<tr><td style="padding:0 32px;"><hr style="border:none; border-top:1px solid #ececf2; margin:4px 0;"></td></tr>
<!-- Audit &amp; Fixes -->
<tr><td style="padding:18px 32px 0;">
<h2 style="margin:0; padding-left:10px; border-left:3px solid #00838f; color:#1a1a2e; font-size:13px; font-weight:700; letter-spacing:0.6px; text-transform:uppercase;">Audit &amp; Fixes</h2>
</td></tr>
<tr><td style="padding:8px 32px 14px;">{{AUDIT}}</td></tr>
<tr><td style="padding:0 32px;"><hr style="border:none; border-top:1px solid #ececf2; margin:4px 0;"></td></tr>
<!-- Work Queue -->
<tr><td style="padding:18px 32px 0;">
<h2 style="margin:0; padding-left:10px; border-left:3px solid #e65100; color:#1a1a2e; font-size:13px; font-weight:700; letter-spacing:0.6px; text-transform:uppercase;">Work Queue</h2>
</td></tr>
<tr><td style="padding:8px 32px 14px;">{{QUEUE}}</td></tr>
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
    except (subprocess.CalledProcessError, FileNotFoundError, NotADirectoryError):
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
    env = {**os.environ,
           "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}",
           "KUBECONFIG": str(HOME / ".kube" / "config"),
           "PATH": f"{STEWARD_PATH}:{os.environ.get('PATH', '')}"}
    return env


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


def _ndjson_looks_like_event_stream(text):
    """True when text is omp --mode json NDJSON (not assistant prose/JSON)."""
    if not text or not isinstance(text, str):
        return False
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return False
        if not isinstance(obj, dict):
            return False
        return obj.get("type") in {
            "session", "agent_start", "agent_end", "turn_start", "turn_end",
            "message_start", "message_end", "message_update", "message",
        }
    return False


def _assistant_text_from_message(msg):
    """Join text parts from an assistant message content list. Skip thinking/tools."""
    if not isinstance(msg, dict):
        return ""
    if msg.get("role") and msg.get("role") != "assistant":
        return ""
    parts = []
    for item in msg.get("content") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            t = item.get("text")
            if isinstance(t, str) and t:
                parts.append(t)
    return "".join(parts)


def _message_error_str(msg):
    """Format stopReason=error / errorMessage from an omp message object."""
    if not isinstance(msg, dict):
        return ""
    err_msg = (msg.get("errorMessage") or msg.get("error") or "").strip()
    status = msg.get("errorStatus") or msg.get("status")
    stop = msg.get("stopReason") or ""
    if stop != "error" and not err_msg and not status:
        return ""
    first = err_msg.splitlines()[0] if err_msg else stop or "error"
    first = first[:300]
    if status:
        return f"{status} {first}".strip()
    return first



def _balanced_json_slice(text, start):
    """Return exclusive end index of balanced JSON value starting at start, or -1."""
    if start < 0 or start >= len(text):
        return -1
    open_ch = text[start]
    if open_ch not in "{[":
        return -1
    pairs = {"{": "}", "[": "]"}
    stack = [open_ch]
    in_str = False
    esc = False
    for i in range(start + 1, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if not stack:
                return -1
            op = stack.pop()
            if pairs[op] != ch:
                return -1
            if not stack:
                return i + 1
    return -1


def _call_omp_p(prompt, model=STEWARD_MODEL, timeout=600, append_system=None, mode="text"):
    """Call omp -p (headless). Returns assistant text. Retries on transient API errors.

    mode="text": plain -p stdout (final assistant message only). Fine for free-form
    summaries. Rejects NDJSON event streams (misconfigured --mode json bleed).
    mode="json": --mode json NDJSON, assistant text accumulated across ALL turns via
    extract_from_ndjson. Required when the caller will _extract_json — advisor loops
    often end on a prose ack while the real ```json packet was on an earlier turn.
    Never returns raw NDJSON for the caller to scavenge.
    """
    if mode not in ("text", "json"):
        raise ValueError(f"unsupported omp mode: {mode!r}")
    cmd = [
        str(HOME / ".bun/bin/omp"), "-p", "--model", model,
        "--api-key", "proxy",
        "--session-dir", str(SESSION_DIR),
        "--allow-home",
        "--config", str(HOME / ".omp/agent/headless-override.yml"),
    ]
    if mode == "json":
        cmd.extend(["--mode", "json"])
    if append_system:
        cmd.extend(["--append-system-prompt", append_system])
    cmd.append(prompt)
    # Retry transient errors: 401 (stale key / account rotation), 429 (rate limit),
    # 5xx (server error), subprocess timeout, and API errors surfaced in NDJSON.
    max_retries = 3
    last_error = None
    recoverable_markers = ("401", "429", "500", "502", "503", "504")

    def _is_recoverable(err_text: str) -> bool:
        return any(code in (err_text or "") for code in recoverable_markers)

    for attempt in range(max_retries):
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env={**os.environ, "HOME": str(HOME), "PATH": f"{STEWARD_PATH}:{os.environ.get('PATH', '')}"},
            )
        except subprocess.TimeoutExpired as e:
            last_error = e
            if attempt < max_retries - 1:
                delay = 2 ** attempt
                print(f"  omp -p timeout (attempt {attempt + 1}/{max_retries}), retrying in {delay}s")
                time.sleep(delay)
                continue
            raise RuntimeError(f"omp -p timed out after {max_retries} attempts (timeout={timeout}s)")

        stdout = result.stdout or ""
        stderr = result.stderr or ""

        if mode == "json":
            if not stdout.strip() and result.returncode != 0:
                err_text = stderr[:500]
                if _is_recoverable(err_text) and attempt < max_retries - 1:
                    delay = 2 ** attempt
                    print(f"  omp -p rc={result.returncode} (attempt {attempt + 1}/{max_retries}), retrying in {delay}s")
                    time.sleep(delay)
                    last_error = f"rc={result.returncode}: {err_text}"
                    continue
                raise RuntimeError(
                    f"omp -p failed (rc={result.returncode}): {err_text}"
                )

            text, stats = extract_from_ndjson(stdout)
            errors = stats.get("errors") or []
            err_join = "; ".join(errors)

            if errors and not text.strip():
                if _is_recoverable(err_join) and attempt < max_retries - 1:
                    delay = 2 ** attempt
                    print(f"  omp -p api error (attempt {attempt + 1}/{max_retries}): {err_join[:120]}; retrying in {delay}s")
                    time.sleep(delay)
                    last_error = err_join
                    continue
                raise RuntimeError(f"omp -p api error: {err_join}")

            if not text.strip():
                # Empty assistant — may be hard failure or stream with only tools
                if result.returncode != 0:
                    err_text = stderr[:500] or err_join or "empty assistant text"
                    if _is_recoverable(err_text) and attempt < max_retries - 1:
                        delay = 2 ** attempt
                        print(f"  omp -p rc={result.returncode} (attempt {attempt + 1}/{max_retries}), retrying in {delay}s")
                        time.sleep(delay)
                        last_error = f"rc={result.returncode}: {err_text}"
                        continue
                    raise RuntimeError(
                        f"omp -p failed (rc={result.returncode}): {err_text}"
                    )
                raise RuntimeError(
                    "omp -p json returned empty assistant text"
                    + (f" ({err_join})" if err_join else "")
                )

            # Prefer assistant text; if API errors also present, still return text
            # (partial success) — callers extract JSON from text.
            return text

        # mode == "text"
        if result.returncode == 0 or stdout.strip():
            if _ndjson_looks_like_event_stream(stdout):
                raise RuntimeError(
                    "omp -p text mode received NDJSON event stream; "
                    "refusing to treat session logs as assistant prose"
                )
            if stdout.strip():
                return stdout
            # rc==0 but empty
            if result.returncode == 0:
                return stdout

        err_text = stderr[:500]
        if _is_recoverable(err_text) and attempt < max_retries - 1:
            delay = 2 ** attempt
            print(f"  omp -p rc={result.returncode} (attempt {attempt + 1}/{max_retries}), retrying in {delay}s")
            time.sleep(delay)
            last_error = f"rc={result.returncode}: {err_text}"
            continue

        raise RuntimeError(
            f"omp -p failed (rc={result.returncode}): {err_text}"
        )

    raise RuntimeError(
        f"omp -p failed after {max_retries} attempts: {last_error}"
    )


def _extract_json(text, label="output"):
    """Extract JSON from assistant text. Never scavenges omp NDJSON event streams."""
    if text is None:
        raise ValueError(f"Could not extract JSON from {label}: text is None")
    if not isinstance(text, str):
        text = str(text)

    if _ndjson_looks_like_event_stream(text):
        raise ValueError(
            f"Could not extract JSON from {label}: got omp NDJSON event stream "
            f"(parser should have returned assistant text only). "
            f"Raw text (first 500 chars):\n{text[:500]}"
        )

    # Fenced blocks — last successful parse wins (final agent packet).
    fences = re.findall(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    for block in reversed(fences):
        try:
            return json.loads(block.strip())
        except json.JSONDecodeError:
            continue

    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass

    # Brace-balanced scan — keep (start, end, value) and prefer largest dict packet.
    candidates = []  # (end-start, start, value)
    for i, ch in enumerate(text):
        if ch not in "{[":
            continue
        end = _balanced_json_slice(text, i)
        if end < 0:
            continue
        snippet = text[i:end]
        try:
            val = json.loads(snippet)
        except json.JSONDecodeError:
            continue
        candidates.append((end - i, i, val))
    if candidates:
        dicts = [c for c in candidates if isinstance(c[2], dict)]
        pool = dicts or candidates
        # Largest span wins; tie-break later start (final packet in multi-object prose).
        pool.sort(key=lambda c: (c[0], c[1]))
        return pool[-1][2]

    raise ValueError(
        f"Could not extract JSON from {label}. Raw text (first 500 chars):\n{text[:500]}"
    )


def extract_from_ndjson(stdout):
    """Parse omp --mode json NDJSON stdout → (assistant_text, stats).

    Supports:
      - Legacy (pi-style): message_update / assistantMessageEvent text_delta
      - Current omp (2026-07+): message_start/message_end with full message objects;
        also turn_end and agent_end.assistant messages

    stats keys: input_tokens, output_tokens, cost_usd, errors[list[str]], format
    Never returns raw NDJSON as text.
    """
    stats = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
        "errors": [],
        "format": "empty",
    }
    if not stdout or not str(stdout).strip():
        return "", stats

    delta_parts = []
    end_texts = []  # assistant texts from message_end / turn_end (ordered)
    saw_delta = False
    saw_end = False
    agent_end_text = ""

    def _absorb_usage(msg):
        if not isinstance(msg, dict):
            return
        usage = msg.get("usage") or {}
        if not isinstance(usage, dict):
            return
        stats["input_tokens"] += int(usage.get("input") or 0)
        stats["output_tokens"] += int(usage.get("output") or 0)
        cost = usage.get("cost") or {}
        if isinstance(cost, dict):
            try:
                stats["cost_usd"] += float(cost.get("total") or 0.0)
            except (TypeError, ValueError):
                pass

    def _absorb_error(msg):
        err = _message_error_str(msg)
        if err and err not in stats["errors"]:
            stats["errors"].append(err)

    for line in str(stdout).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        typ = obj.get("type") or ""

        if typ == "message_update":
            ev = obj.get("assistantMessageEvent") or {}
            if isinstance(ev, dict) and ev.get("type") == "text_delta":
                delta = ev.get("delta") or ""
                if delta:
                    delta_parts.append(delta)
                    saw_delta = True

        elif typ in ("message_end", "turn_end"):
            msg = obj.get("message") or {}
            if not isinstance(msg, dict):
                continue
            _absorb_usage(msg)
            _absorb_error(msg)
            if msg.get("role") == "assistant":
                saw_end = True
                t = _assistant_text_from_message(msg)
                if t:
                    end_texts.append(t)

        elif typ == "message_start":
            # Errors sometimes only appear fully on message_end; still check.
            msg = obj.get("message") or {}
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                _absorb_error(msg)

        elif typ == "agent_end":
            messages = obj.get("messages") or []
            if isinstance(messages, list):
                for msg in messages:
                    if not isinstance(msg, dict):
                        continue
                    if msg.get("role") == "assistant":
                        _absorb_usage(msg)
                        _absorb_error(msg)
                        t = _assistant_text_from_message(msg)
                        if t:
                            agent_end_text = t  # last assistant wins

    if saw_delta and saw_end:
        stats["format"] = "mixed"
    elif saw_delta:
        stats["format"] = "legacy-delta"
    elif saw_end or agent_end_text:
        stats["format"] = "message-end"
    else:
        stats["format"] = "empty"

    # Prefer deltas when present (streaming path); else message_end texts;
    # else agent_end fallback. Do not double-append end text when deltas exist.
    if saw_delta:
        text = "".join(delta_parts)
    elif end_texts:
        # Multiple assistant message_ends across turns — join in order (advisor loops).
        text = "\n".join(end_texts)
    else:
        text = agent_end_text or ""

    return text, stats


def _call_omp_p_json(prompt, timeout=OMP_JSON_TIMEOUT, extra_args=None):
    """Call omp -p in --mode json. Returns (accumulated_text, stats, packet, raw_stdout)."""
    cmd = [
        str(HOME / ".bun/bin/omp"), "-p", "--model", OMP_JSON_MODEL, "--mode", "json",
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
    raw_stdout = result.stdout or ""
    if result.returncode != 0 and not raw_stdout.strip():
        raise RuntimeError(
            f"omp -p json failed (rc={result.returncode}): {(result.stderr or '')[:500]}"
        )

    text, stats = extract_from_ndjson(raw_stdout)
    errors = stats.get("errors") or []
    if errors and not text.strip():
        raise RuntimeError(f"omp -p api error: {'; '.join(errors)}")
    if not text.strip():
        raise RuntimeError("omp -p json returned empty assistant text")

    try:
        packet = _extract_json(text, "executor packet")
    except ValueError:
        packet = {"raw_text": text[:2000]}
    return text, stats, packet, raw_stdout



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
            rolling = acct.get("rolling") or {}
            weekly = acct.get("weekly") or {}
            monthly = acct.get("monthly") or {}
            usage["accounts"].append({
                "name": acct.get("name", "?"),
                "tier": acct.get("tier", "unknown"),
                "rolling_pct": rolling.get("pct", 0),
                "weekly_pct": weekly.get("pct", 0),
                "monthly_pct": monthly.get("pct", 0),
                "rolling_reset_in": rolling.get("reset_in") or "",
                "weekly_reset_in": weekly.get("reset_in") or "",
                "monthly_reset_in": monthly.get("reset_in") or "",
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
        resets = []
        if a.get("rolling_reset_in"):
            resets.append(f"5h→{a['rolling_reset_in']}")
        if a.get("weekly_reset_in"):
            resets.append(f"7d→{a['weekly_reset_in']}")
        if a.get("monthly_reset_in"):
            resets.append(f"30d→{a['monthly_reset_in']}")
        reset_s = f" [{', '.join(resets)}]" if resets else ""
        acct_lines.append(
            f"    {a['name']} ({a['tier']}): "
            f"5h={a['rolling_pct']}%, weekly={a['weekly_pct']}%, "
            f"monthly={a['monthly_pct']}%{extra}{reset_s}"
        )
    print(f"[P0] setup -> {artifact}")
    if usage["proxy_error"]:
        print(f"  proxy: UNREACHABLE ({usage['proxy_error']})")
    else:
        print(f"  usage ({len(usage['accounts'])} accounts):")
        for line in acct_lines:
            print(line)
    return data


# ── P0b: session memory ───────────────────────────────────────────────

def _session_header(path):
    """Parse session metadata from a session jsonl (type=session|title lines)."""
    remembered = None
    try:
        with open(path, "r", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i > 20:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("type") == "session":
                    return obj
                if remembered is None and obj.get("type") == "title" and obj.get("title"):
                    remembered = {"id": None, "timestamp": None, "cwd": None,
                                  "title": obj.get("title")}
    except Exception:
        pass
    return remembered or {}


def _iter_interactive_sessions(cutoff_ts):
    """Yield (project, path, mtime, header) for interactive session transcripts >= cutoff.

    Interactive sessions live in ~/.omp/agent/sessions/<project>/; headless invocations
    go to sessions-automated/ via --session-dir and are intentionally excluded.
    """
    if not SESSION_INTERACTIVE_DIR.exists():
        return
    for proj_dir in sorted(SESSION_INTERACTIVE_DIR.iterdir()):
        if not proj_dir.is_dir():
            continue
        project = proj_dir.name
        for f in sorted(proj_dir.glob("*.jsonl")):
            try:
                mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
            except OSError:
                continue
            if mtime < cutoff_ts:
                continue
            yield project, f, mtime, _session_header(f)


def _session_date_str(header, mtime):
    ts = header.get("timestamp")
    if ts:
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%Y-%m-%d")
        except Exception:
            pass
    return mtime.strftime("%Y-%m-%d")


def _existing_memoir_for(session_id, source_path, date_str):
    """Find a memoir whose frontmatter matches this session (session_id or source path)."""
    day_dir = SESSION_MEMOIR_DIR / date_str
    if not day_dir.is_dir():
        return None
    for f in sorted(day_dir.glob("*.md")):
        try:
            txt = f.read_text(errors="replace")
        except OSError:
            continue
        m = re.search(r"^session_id:\s*(\S+)", txt, re.MULTILINE)
        if m and m.group(1) == session_id:
            return f
        m = re.search(r"^source:\s*(\S+)", txt, re.MULTILINE)
        if m and m.group(1) == str(source_path):
            return f
    return None


def _session_excerpt(path, head=2500, tail=800):
    """Compact transcript excerpt (head + tail) for the filter judge."""
    try:
        txt = path.read_text(errors="replace")
    except OSError:
        return ""
    if len(txt) <= head + tail:
        return txt
    return txt[:head] + "\n…[truncated]…\n" + txt[-tail:]


def _sanitize_slug(s, maxlen=48):
    s = re.sub(r"[^a-z0-9-]+", "-", (s or "").lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    return (s[:maxlen].rstrip("-")) or "session"


def _parse_iso(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _memoir_path(date_str, hhmm, slug):
    day_dir = SESSION_MEMOIR_DIR / date_str
    day_dir.mkdir(parents=True, exist_ok=True)
    base = f"{hhmm}-{slug}"
    p = day_dir / f"{base}.md"
    n = 2
    while p.exists():
        p = day_dir / f"{base}-{n}.md"
        n += 1
    return p


def _write_memoir(path, date_str, project, header, body, source_path, session_id):
    """Deterministic frontmatter + H1; LLM body. path is the target file."""
    title = (header.get("title") or "session").strip()[:80] or "session"
    hhmm = "0000"
    ts = header.get("timestamp")
    if ts:
        try:
            hhmm = _parse_iso(ts).strftime("%H%M")
        except Exception:
            pass
    fm = (
        "---\n"
        f"title: {title}\n"
        f"source: {source_path}\n"
        f"session_id: {session_id}\n"
        f"project: {project}\n"
        f"date: {date_str}\n"
        "---\n"
    )
    h1 = f"# Session: {date_str} {hhmm[:2]}:{hhmm[2:]} — {title}"
    path.write_text(fm + h1 + "\n\n" + body.strip() + "\n")


FILTER_JUDGE_PROMPT = """You are a light filter judge for Carter's session-memory vault.

Decide whether this interactive omp session is worth documenting in the homelab memory
bank. SKIP only sessions that are clearly not worth recording:
- test / scratch / throwaway runs (e.g. in /tmp, trying a flag, playing around)
- sessions with no decisions, no state changes, no findings — "didn't go anywhere"
FAIL OPEN: when in doubt, verdict "document". An extra short memoir is cheap; a missing
one loses context.

SESSION:
- project: {project}
- title: {title}
- started: {started}
- cwd: {cwd}

TRANSCRIPT EXCERPT (head + tail):
{excerpt}

Return a fenced ```json packet:
{{"verdict": "document"|"skip", "reason": "<one line>"}}"""


SUMMARIZER_PROMPT = """You are writing a session memoir for Carter's homelab memory bank.

Read the source session transcript with your read tool, then write a COMPACT memoir
(aim <= 15 lines, terse bullets — a memory bank, not a log). Keep only durable content:
decisions, state changes (concrete files/commands/system changes), gotchas, next steps.

SOURCE SESSION (read this file):
{path}

Session metadata: project={project}, title={title}, started={started}

Write the body ONLY (no frontmatter, no leading H1 — the steward adds those), starting
with exactly these fields:
**Topics:** comma-separated list
**Decisions:**
- ...
**State changes:**
- ...
**Context for next time:** 1-2 sentences

Return a fenced ```json packet:
{{"label": "<short kebab-case topic slug, <= 5 words>", "markdown": "<the memoir body>"}}"""


MEMOIR_JUDGE_PROMPT = """You are verifying a session memoir in Carter's memory vault
against its source session transcript.

MEMOIR FILE: {memoir_path}
MEMOIR CONTENT:
{memoir_content}

SOURCE SESSION (verify against this file):
{path}

Check: (a) the summary matches the transcript — no fabricated claims; (b) important
decisions/state changes are not missing; (c) the content is compact and terse.

If the memoir is accurate and complete, verdict "ok". If it needs fixes, return the
full corrected body (no frontmatter, no H1) in "updated_markdown".

Return a fenced ```json packet:
{{"verdict": "ok"|"update", "reason": "<one line>", "updated_markdown": "<full body only if update>"}}"""


def _document_session(path, s):
    """Filter judge -> summarizer -> write memoir. Fail-open on LLM errors."""
    excerpt = _session_excerpt(path)
    if len(excerpt.strip()) < 200:
        return {"action": "skipped_empty", "reason": "transcript too short (<200 chars)"}

    try:
        raw = _call_omp_p(
            FILTER_JUDGE_PROMPT.format(project=s["project"], title=s.get("title") or "(untitled)",
                                       started=s["started"], cwd=s.get("cwd") or "",
                                       excerpt=excerpt),
            model=SMALL_MODEL, timeout=300, mode="json")
        packet = _extract_json(raw, "session-filter-judge")
    except Exception as e:
        packet = {"verdict": "document", "filter_error": str(e)[:200]}
    if packet.get("verdict") == "skip":
        return {"action": "skipped", "reason": (packet.get("reason") or "")[:200],
                "filter_error": packet.get("filter_error", "")}

    try:
        raw = _call_omp_p(
            SUMMARIZER_PROMPT.format(path=path, project=s["project"],
                                     title=s.get("title") or "(untitled)",
                                     started=s["started"]),
            model=SMALL_MODEL, timeout=600, mode="json")
        packet = _extract_json(raw, "session-summarizer")
        body = (packet.get("markdown") or "").strip()
        label = _sanitize_slug(packet.get("label") or s.get("title"))
        if not body:
            raise ValueError("summarizer returned empty markdown")
    except Exception as e:
        return {"action": "summarizer_failed", "error": str(e)[:200]}

    hhmm = _parse_iso(s["started"]).strftime("%H%M")
    mp = _memoir_path(s["date"], hhmm, label)
    _write_memoir(mp, s["date"], s["project"], {"title": s.get("title") or label,
                                                "timestamp": s["started"]},
                  body, path, s["session_id"])
    return {"action": "documented", "memoir": str(mp)}


def _judge_existing_memoir(path, s, memoir_path):
    """Judge an agent-written memoir against the source transcript; update if needed."""
    try:
        content = memoir_path.read_text(errors="replace")
    except OSError as e:
        return {"action": "judge_error", "error": str(e)[:200]}
    try:
        raw = _call_omp_p(
            MEMOIR_JUDGE_PROMPT.format(memoir_path=memoir_path,
                                       memoir_content=content[:6000], path=path),
            model=SMALL_MODEL, timeout=600, mode="json")
        packet = _extract_json(raw, "session-memoir-judge")
    except Exception as e:
        return {"action": "judge_error", "error": str(e)[:200]}
    verdict = packet.get("verdict")
    reason = (packet.get("reason") or "")[:200]
    if verdict == "update":
        new_body = (packet.get("updated_markdown") or "").strip()
        if new_body:
            _write_memoir(memoir_path, s["date"], s["project"],
                          {"title": s.get("title"), "timestamp": s["started"]},
                          new_body, path, s["session_id"])
            return {"action": "updated", "reason": reason}
        return {"action": "judge_error", "reason": "update verdict without updated_markdown"}
    return {"action": "judged_ok" if verdict == "ok" else "judge_unknown", "reason": reason}


def _commit_memoirs(date_str):
    """Commit + push the notes vault session-memory changes. Best-effort."""
    try:
        r = run_capture_ok(["git", "-C", str(HOME / "notes"), "add", "logs/sessions"])
        if r[2] != 0:
            return {"ok": False, "error": (r[1] or r[0])[:300]}
        staged = run_capture(["git", "-C", str(HOME / "notes"), "diff", "--cached", "--name-only"])
        if not staged.strip():
            return {"ok": True, "nothing_to_commit": True}
        run(["git", "-C", str(HOME / "notes"), "commit", "-m", f"session memory: {date_str}"],
            capture_output=True, text=True)
        run(["git", "-C", str(HOME / "notes"), "push"], capture_output=True, text=True, timeout=60)
        return {"ok": True, "files": len(staged.splitlines())}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def phase_0b_session_memory(run_dir, dry_run=False, setup=None):
    """Phase 0b: maintain the session memory bank (~/notes/logs/sessions/).

    Scans interactive omp sessions (~/.omp/agent/sessions/<project>/*.jsonl) newer than
    the last steward run, filter-skips test/dead-end sessions (LLM judge, fail-open),
    then writes compact memoirs with source pointers — or judges/updates memoirs the
    agent already wrote during the session. Commits the notes vault. Headless sessions
    (sessions-automated/) are excluded by directory.
    """
    print("[P0b] session memory")
    prev_date = (setup or {}).get("prev_date", "")

    # Cutoff: start of the last steward run; fall back to yesterday 00:00 UTC
    cutoff = None
    try:
        if RUNS_LOG.exists():
            lines = [l for l in RUNS_LOG.read_text().splitlines() if l.strip()]
            if lines:
                ts = json.loads(lines[-1]).get("ts")
                if ts:
                    cutoff = _parse_iso(ts)
    except Exception as e:
        print(f"  warn: runs log cutoff parse failed: {e}")
    if cutoff is None:
        try:
            cutoff = datetime.strptime(prev_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    print(f"  cutoff: {cutoff.isoformat()}")

    now = datetime.now(timezone.utc)
    sessions = []
    for project, path, mtime, header in _iter_interactive_sessions(cutoff):
        stem = path.stem
        session_id = header.get("id") or (stem.split("_")[-1] if "_" in stem else stem)
        date_str = _session_date_str(header, mtime)
        entry = {
            "project": project, "path": str(path), "mtime": mtime.isoformat(),
            "started": header.get("timestamp") or mtime.isoformat(),
            "session_id": session_id, "title": header.get("title") or "",
            "cwd": header.get("cwd") or "", "date": date_str,
        }
        age_min = (now - mtime).total_seconds() / 60.0
        if age_min < SESSION_ACTIVE_MINUTES:
            entry["action"] = "skipped_active"
        else:
            memoir = _existing_memoir_for(session_id, path, date_str)
            entry["action"] = "judge" if memoir else "document"
            entry["memoir"] = str(memoir) if memoir else None
        sessions.append(entry)

    n_active = sum(1 for s in sessions if s["action"] == "skipped_active")
    print(f"  sessions since cutoff: {len(sessions)} ({n_active} active, skipped)")

    if dry_run:
        for s in sessions:
            if s["action"] in ("document", "judge"):
                print(f"  DRY RUN would {s['action']}: {s['path']}")
        write_json(run_dir / "00b-session-memory.json",
                   {"cutoff": cutoff.isoformat(), "dry_run": True, "sessions": sessions})
        return

    errors = []
    for s in sessions:
        if s["action"] == "skipped_active":
            continue
        path = Path(s["path"])
        try:
            if s["action"] == "judge":
                s.update(_judge_existing_memoir(path, s, Path(s["memoir"])))
            else:
                s.update(_document_session(path, s))
        except Exception as e:
            s["action"] = "error"
            s["error"] = str(e)[:300]
            errors.append({"path": s["path"], "error": str(e)[:300]})
        print(f"  {s['action']}: {s['path']}")

    commit = {}
    if any(s["action"] in ("documented", "updated") for s in sessions):
        commit = _commit_memoirs(datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        print(f"  notes commit: {commit}")

    write_json(run_dir / "00b-session-memory.json",
               {"cutoff": cutoff.isoformat(), "sessions": sessions, "commit": commit,
                "errors": errors})
    print(f"[P0b] done -> {run_dir / '00b-session-memory.json'}")


def _p1_apt_upgrade():
    """Run apt update + apt upgrade -y."""
    print("  [1a] apt update + upgrade")
    try:
        run(["sudo", "apt", "update"], capture_output=True, text=True)
        upgrade = run(["sudo", "apt", "upgrade", "-y"], capture_output=True, text=True)
        stdout = upgrade.stdout or ""
        m = re.search(r"(\d+)\s+upgraded", stdout)
        upgraded = int(m.group(1)) if m else 0
        # needrestart / apt may restart docker even when our auto_* steps later
        # report "skipped" (versions already match post-upgrade).
        docker_touched = bool(re.search(
            r"(?im)^(setting up|unpacking)\s+docker-|"
            r"^setting up\s+containerd\.io|"
            r"restarting.*\bdocker\.service\b|"
            r"\bdocker\.service\b.*restart",
            stdout,
        ))
        return {
            "step": "apt_upgrade",
            "status": "ok",
            "upgraded_count": upgraded,
            "docker_touched": docker_touched,
            "output_tail": "\n".join(stdout.strip().splitlines()[-20:]),
        }
    except subprocess.CalledProcessError as e:
        return {"step": "apt_upgrade", "status": "failed", "error": str(e),
                "output": e.stdout if e.stdout else ""}


def _wait_docker_stack_ready(timeout_s=120):
    """After docker daemon restart, wait until docker + key HTTP endpoints answer.

    open-webui in particular can take >30s to leave 'health: starting' and bind
    48100; a single immediate curl yields empty http_code and a false FAIL.
    """
    print(f"  [docker-settle] waiting up to {timeout_s}s for docker + endpoints")
    deadline = time.time() + timeout_s
    last = {"docker": "", "endpoints": {}}
    while time.time() < deadline:
        root = run_capture(
            ["docker", "info", "--format", "{{.DockerRootDir}}"], timeout=15)
        last["docker"] = root
        if root != "/var/lib/docker":
            time.sleep(3)
            continue

        endpoint_ok = {}
        all_ok = True
        for name, url in ENDPOINTS.items():
            if name == "searxng":
                # JSON content check is heavier; connectivity is enough here.
                code = run_capture(
                    ["curl", "-so", "/dev/null", "-w", "%{http_code}",
                     "--connect-timeout", "3", "--max-time", "8",
                     url.split("?")[0] if "?" in url else url],
                    timeout=15,
                )
            else:
                code = run_capture(
                    ["curl", "-so", "/dev/null", "-w", "%{http_code}",
                     "--connect-timeout", "3", "--max-time", "8", url],
                    timeout=15,
                )
            ok = bool(code) and (code.startswith("2") or code.startswith("3"))
            endpoint_ok[name] = code or "empty"
            if not ok:
                all_ok = False
        last["endpoints"] = endpoint_ok
        if all_ok:
            print(f"  [docker-settle] ready: {endpoint_ok}")
            return {"status": "ok", "endpoints": endpoint_ok}
        time.sleep(3)

    print(f"  [docker-settle] timed out: {last}")
    return {"status": "timeout", **last}


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


FRESHRSS_DEPLOYMENT = HOME / "k3s" / "freshrss" / "freshrss-deployment.yaml"


def _p1_freshrss_update():
    """Check FreshRSS Docker Hub for newer tag, bump and apply if newer."""
    print("  [1e] freshrss tag check")
    env = user_env()

    if not FRESHRSS_DEPLOYMENT.exists():
        return {"step": "freshrss", "status": "skipped",
                "reason": f"deployment file not found: {FRESHRSS_DEPLOYMENT}"}

    dep_text = FRESHRSS_DEPLOYMENT.read_text()
    m = re.search(r"freshrss/freshrss:(\S+)", dep_text)
    if not m:
        return {"step": "freshrss", "status": "skipped",
                "reason": "could not parse current tag from deployment yaml"}
    current_tag = m.group(1)

    # Resolve latest from Docker Hub
    latest_tag = None
    try:
        req = urllib.request.Request(
            "https://hub.docker.com/v2/repositories/freshrss/freshrss/tags?"
            "page_size=50&ordering=last_updated",
            headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            tags = [t["name"] for t in data.get("results", [])]
        # Prefer newest stable semver (N.N.N)
        semver = [t for t in tags if re.match(r"^\d+\.\d+\.\d+$", t)]
        if semver:
            latest_tag = sorted(semver, key=lambda s: tuple(int(x) for x in s.split(".")))[-1]
        else:
            # Fallback: newest numeric build tag
            numeric = [t for t in tags if re.match(r"^\d+$", t)]
            if numeric:
                latest_tag = sorted(numeric, key=int)[-1]
    except Exception as e:
        return {"step": "freshrss", "status": "error",
                "reason": f"Docker Hub unreachable: {e}", "current_tag": current_tag}

    if not latest_tag:
        return {"step": "freshrss", "status": "error",
                "reason": "no valid tag found on Docker Hub", "current_tag": current_tag}

    if current_tag == latest_tag:
        return {"step": "freshrss", "status": "current", "current_tag": current_tag}

    # Newer tag found — bump, apply, rollout
    print(f"  freshrss: {current_tag} -> {latest_tag}")
    new_dep_text = dep_text.replace(f"freshrss/freshrss:{current_tag}",
                                     f"freshrss/freshrss:{latest_tag}")

    try:
        FRESHRSS_DEPLOYMENT.write_text(new_dep_text)
        run(["kubectl", "apply", "-f", str(FRESHRSS_DEPLOYMENT)],
            env=env, capture_output=True, text=True)
        run(["kubectl", "rollout", "status", "deploy/freshrss", "-n", "freshrss",
             "--timeout=180s"],
            env=env, capture_output=True, text=True)
        return {"step": "freshrss", "status": "bumped",
                "current_tag": current_tag, "latest_tag": latest_tag}
    except subprocess.CalledProcessError as e:
        msg = str(e)
        if e.stderr:
            msg += f" | stderr: {e.stderr[-500:]}"
        # If apply failed, revert yaml
        if "apply" in msg:
            FRESHRSS_DEPLOYMENT.write_text(dep_text)
        return {"step": "freshrss", "status": "failed",
                "current_tag": current_tag, "latest_tag": latest_tag, "error": msg}


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


def _p1_herdr_update():
    """Self-update herdr via `herdr update` (installs to ~/.local/bin/herdr).

    `herdr update` refuses to run inside a herdr session (env-var detection) —
    that only happens on a manual steward run launched from a herdr pane, so it
    is reported as skipped rather than failing P1. The nightly timer runs
    outside herdr (systemd user manager has no HERDR_* vars) and updates
    normally.
    """
    print("  [1g] herdr update")
    env = user_env()
    pre_ver = run_capture(["herdr", "--version"], env=env, timeout=30)
    stdout, stderr, code = run_capture_ok(["herdr", "update"], env=env, timeout=300)
    out = f"{stdout}\n{stderr}".strip()
    post_ver = run_capture(["herdr", "--version"], env=env, timeout=30)

    if "outside herdr" in out:
        return {"step": "herdr_update", "status": "skipped",
                "reason": "refused: run inside a herdr session (nightly timer runs outside)",
                "pre_version": pre_ver, "output_tail": out[-500:]}
    if post_ver and post_ver != pre_ver:
        return {"step": "herdr_update", "status": "ok",
                "pre_version": pre_ver, "post_version": post_ver,
                "output_tail": out[-500:]}
    if code == 0:
        return {"step": "herdr_update", "status": "skipped",
                "pre_version": pre_ver, "post_version": post_ver,
                "reason": "already current", "output_tail": out[-500:]}
    return {"step": "herdr_update", "status": "failed",
            "pre_version": pre_ver, "post_version": post_ver,
            "error": out[-500:] or f"exit {code}"}


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

    # 1c: settle docker after apt/auto path restarts the daemon.
    # apt needrestart may bounce docker even when auto_* later reports "skipped"
    # (versions already match). open-webui takes >30s after daemon restart.
    docker_upgraded = any(
        s["step"].startswith("auto_docker") and s["status"] == "ok"
        for s in auto_results
    )
    docker_touched = bool(result.get("docker_touched")) or docker_upgraded
    if docker_touched:
        settle = _wait_docker_stack_ready(timeout_s=120)
        steps.append({
            "step": "docker_settle",
            "status": settle.get("status", "ok"),
            "endpoints": settle.get("endpoints", {}),
            "reason": "apt_docker_touched" if result.get("docker_touched") else "auto_docker_upgrade",
        })
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

    # 1e: freshrss update
    steps.append(_p1_freshrss_update())

    # 1f: open-webui
    steps.append(_p1_openwebui())

    # 1g: herdr self-update (refuses inside a herdr session → skipped, not failed)
    steps.append(_p1_herdr_update())

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

    # Endpoint curls — retry briefly; containers may still be binding after an
    # apt-triggered docker restart (open-webui especially).
    def _probe_endpoint(name, url, attempts=8, delay_s=5):
        last = None
        for i in range(attempts):
            if name == "searxng":
                resp = run_capture(
                    ["curl", "-s", "--connect-timeout", "10", "--max-time", "20", url],
                    timeout=25,
                )
                if resp:
                    try:
                        data = json.loads(resp)
                        healthy = isinstance(data.get("results"), list)
                        last = {
                            "name": f"endpoint_{name}", "url": url,
                            "http_code": "200", "status": "ok" if healthy else "fail",
                            "content_valid": healthy,
                        }
                        if healthy:
                            return last
                    except json.JSONDecodeError:
                        last = {
                            "name": f"endpoint_{name}", "url": url,
                            "http_code": "??", "status": "fail",
                            "error": "invalid JSON response",
                        }
                else:
                    last = {
                        "name": f"endpoint_{name}", "url": url,
                        "http_code": "", "status": "fail",
                        "error": "empty response",
                    }
            else:
                code = run_capture(
                    ["curl", "-so", "/dev/null", "-w", "%{http_code}",
                     "--connect-timeout", "5", "--max-time", "15", url],
                    timeout=20,
                )
                healthy = bool(code) and (code.startswith("2") or code.startswith("3"))
                last = {
                    "name": f"endpoint_{name}", "url": url,
                    "http_code": code, "status": "ok" if healthy else "fail",
                }
                if not code:
                    last["error"] = "empty response / connect failed"
                if healthy:
                    return last
            if i + 1 < attempts:
                time.sleep(delay_s)
        return last

    for name, url in ENDPOINTS.items():
        checks.append(_probe_endpoint(name, url))

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
        agent_output = _call_omp_p(troubleshoot_prompt, timeout=600, mode="json")
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


def _systemctl_unit_name(token):
    """Normalize a systemctl list/failed token to a unit name.

    Non-plain list-units prefixes failed/not-found rows with a glyph (often '●'),
    so naive split()[0] returns the glyph and drops the real unit from the set —
    which then falsely flags oneshot units like hyperliquid-sdk.service as missing.
    Prefer --plain when collecting; this helper still defends mixed inputs.
    """
    if not token:
        return ""
    tok = token.strip()
    if tok in {"●", "○", "×", "*"}:
        return ""
    # Strip leading non-unit junk (UTF-8 bullet etc.)
    tok = re.sub(r"^[^\w@.\\-]+", "", tok)
    return tok


def _parse_systemctl_unit_names(output, suffixes=(".service", ".timer")):
    """Extract unit names from systemctl list-units / --failed output."""
    names = set()
    for line in (output or "").splitlines():
        parts = line.split()
        if not parts:
            continue
        unit = ""
        for tok in parts[:3]:
            cand = _systemctl_unit_name(tok)
            if cand.endswith(suffixes):
                unit = cand
                break
        if unit:
            names.add(unit)
    return names


def _parse_failed_unit_lines(output):
    """Return structured failed-unit rows from systemctl --failed --no-legend."""
    rows = []
    for line in (output or "").splitlines():
        raw = line.strip()
        if not raw:
            continue
        parts = raw.split()
        unit = ""
        for tok in parts[:3]:
            cand = _systemctl_unit_name(tok)
            if cand.endswith((".service", ".timer", ".socket", ".target", ".path", ".mount")):
                unit = cand
                break
        if not unit:
            continue
        rows.append({"unit": unit, "raw": raw})
    return rows


def _clear_stale_oneshot_failures(failed_rows, env):
    """Reset oneshot units stuck failed after a later successful run.

    systemd leaves Type=oneshot units in failed until reset-failed. If the
    hyperliquid state file records a Last run newer than the unit's last exit,
    clear the stale failure so heartbeat/email stop alarming.
    """
    cleared = []
    kept = []
    for row in failed_rows:
        unit = row.get("unit") or ""
        if unit != "hyperliquid-sdk.service":
            kept.append(row)
            continue
        state_path = HOME / "agent-state" / "hyperliquid-sdk.md"
        if not state_path.exists():
            kept.append(row)
            continue
        try:
            text = state_path.read_text(errors="replace")
        except OSError:
            kept.append(row)
            continue
        m = re.search(r"\*\*Last run:\*\*\s*(\d{4}-\d{2}-\d{2})", text)
        if not m:
            kept.append(row)
            continue
        try:
            last_run = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except ValueError:
            kept.append(row)
            continue
        exit_ts = run_capture(
            ["systemctl", "--user", "show", unit,
             "-p", "ExecMainExitTimestamp", "--value"],
            env=env,
        ).strip()
        exit_date = None
        if exit_ts and exit_ts not in ("", "n/a", "0"):
            dm = re.search(r"(\d{4}-\d{2}-\d{2})", exit_ts)
            if dm:
                try:
                    exit_date = datetime.strptime(dm.group(1), "%Y-%m-%d").date()
                except ValueError:
                    exit_date = None
        if exit_date is None or not (last_run > exit_date):
            kept.append(row)
            continue
        run_capture(["systemctl", "--user", "reset-failed", unit], env=env)
        still = run_capture(
            ["systemctl", "--user", "is-failed", unit], env=env).strip()
        if still == "failed":
            kept.append(row)
            continue
        cleared.append({
            "unit": unit,
            "reason": f"state Last run {last_run} > unit exit {exit_date}",
        })
        print(f"  cleared stale failed unit {unit} (Last run {last_run} > exit {exit_date})")
    return kept, cleared


def phase_4_heartbeat(run_dir):
    """Phase 4: extended heartbeat block."""
    print("[P4] heartbeat checks")
    env = user_env()

    # Failed systemd units (plain output avoids ● glyph prefix)
    failed_user = run_capture(
        ["systemctl", "--user", "--failed", "--no-legend", "--plain"], env=env)
    failed_system = run_capture(
        ["systemctl", "--failed", "--no-legend", "--plain"])
    failed_user_rows = _parse_failed_unit_lines(failed_user)
    failed_system_rows = _parse_failed_unit_lines(failed_system)
    failed_user_rows, cleared_failed = _clear_stale_oneshot_failures(
        failed_user_rows, env)

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
        ["systemctl", "--user", "list-units", "--all", "--no-legend", "--plain"],
        env=env,
    )
    # Prefer unit-files for "installed"; list-units can miss inactive oneshots.
    # Union both so documented oneshots aren't false-missing.
    user_unit_files_out = run_capture(
        ["systemctl", "--user", "list-unit-files", "--no-legend", "--plain"],
        env=env,
    )
    active_units = _parse_systemctl_unit_names(all_user_units)
    active_units |= _parse_systemctl_unit_names(user_unit_files_out)
    extra_units = active_units - documented_units
    missing_units = documented_units - active_units

    # System unit inventory
    documented_system_units = {
        "cloudflared.service", "docker-user-rules.service", "ssh.service",
        "ufw.service", "cron.service", "containerd.service", "docker.service",
        "apparmor.service", "fstrim.timer",
    }
    all_system_units = run_capture(
        ["systemctl", "list-units", "--all", "--no-legend", "--plain"])
    system_unit_files_out = run_capture(
        ["systemctl", "list-unit-files", "--no-legend", "--plain"])
    active_system_units = _parse_systemctl_unit_names(all_system_units)
    active_system_units |= _parse_systemctl_unit_names(system_unit_files_out)
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
    installed_user_units = _parse_systemctl_unit_names(user_unit_files_out)
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
            "user": [r["unit"] for r in failed_user_rows],
            "system": [r["unit"] for r in failed_system_rows],
            "user_raw": [r["raw"] for r in failed_user_rows],
            "system_raw": [r["raw"] for r in failed_system_rows],
            "cleared": cleared_failed,
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


def _item_abs_path(item, kind):
    """Resolve absolute path for a scanned idea/plan item (open dirs only)."""
    base = IDEAS_DIR if kind == "idea" else PLANS_DIR
    return base / item["file"]


def _set_status_line(path, status_value):
    """Rewrite **Status:** line (or insert after first heading). Returns new text."""
    text = path.read_text()
    if re.search(r"^\*\*Status:\*\*", text, re.MULTILINE):
        return re.sub(
            r"^\*\*Status:\*\*\s*.+$",
            f"**Status:** {status_value}",
            text,
            count=1,
            flags=re.MULTILINE,
        )
    # Insert after first H1
    m = re.search(r"^#\s+.+$", text, re.MULTILINE)
    if m:
        insert_at = m.end()
        return text[:insert_at] + f"\n\n**Status:** {status_value}\n" + text[insert_at:].lstrip("\n")
    return f"**Status:** {status_value}\n\n" + text


def _apply_queue_status_update(update, dry_run=False):
    """Apply one judge-confirmed status update. Returns result dict."""
    kind = update.get("kind", "idea")
    rel = update.get("file") or update.get("path") or ""
    # Accept absolute or bare filename
    path = Path(rel).expanduser() if rel.startswith("/") or rel.startswith("~") else None
    if path is None:
        base = IDEAS_DIR if kind == "idea" else PLANS_DIR
        path = base / Path(rel).name
    if not path.exists():
        # Maybe already moved
        done_path = (IDEAS_DIR if kind == "idea" else PLANS_DIR) / "done" / path.name
        if done_path.exists():
            return {"file": path.name, "kind": kind, "status": "already_done",
                    "path": str(done_path)}
        return {"file": path.name, "kind": kind, "status": "missing", "path": str(path)}

    new_status = (update.get("new_status") or "").strip().lower()
    reason = (update.get("reason") or "").strip()
    move_to_done = bool(update.get("move_to_done")) or new_status == "done"

    # Status line value — keep a short human note for done
    if new_status == "done":
        today = datetime.now().strftime("%Y-%m-%d")
        status_value = f"DONE ({today})"
        if reason:
            status_value += f" — {reason[:120]}"
    elif new_status in ("planned", "idea", "scrapped", "draft", "approved",
                        "implementing"):
        status_value = new_status
        if reason and new_status == "scrapped":
            status_value += f" — {reason[:120]}"
    else:
        return {"file": path.name, "kind": kind, "status": "rejected_status",
                "new_status": new_status}

    if dry_run:
        return {
            "file": path.name, "kind": kind, "status": "dry_run",
            "would_set": status_value, "would_move": move_to_done and new_status == "done",
            "path": str(path),
        }

    new_text = _set_status_line(path, status_value)
    path.write_text(new_text)

    final_path = path
    if move_to_done and new_status == "done":
        done_dir = (IDEAS_DIR if kind == "idea" else PLANS_DIR) / "done"
        done_dir.mkdir(parents=True, exist_ok=True)
        dest = done_dir / path.name
        if dest.exists() and dest.resolve() != path.resolve():
            # Prefer not to clobber; leave updated file in place
            return {
                "file": path.name, "kind": kind, "status": "updated_exists_in_done",
                "path": str(path), "dest_exists": str(dest),
            }
        if dest.resolve() != path.resolve():
            path.rename(dest)
            final_path = dest

    return {
        "file": path.name, "kind": kind, "status": "updated",
        "new_status": new_status, "path": str(final_path),
        "moved_to_done": move_to_done and new_status == "done",
        "reason": reason[:200],
    }


def _reconcile_queue_statuses(ideas_outstanding, plans_open, dry_run=False):
    """Agent checks each open idea/plan; judge verifies; apply confirmed updates.

    Returns {updates_proposed, updates_confirmed, applied, judge, error?}.
    """
    candidates = []
    for item in ideas_outstanding:
        p = _item_abs_path(item, "idea")
        if p.exists():
            candidates.append({
                "kind": "idea",
                "file": item["file"],
                "path": str(p),
                "status": item["status"],
                "heading": item.get("heading", ""),
                "summary": item.get("summary", ""),
                "age_days": item.get("age_days"),
            })
    for item in plans_open:
        p = _item_abs_path(item, "plan")
        if p.exists():
            candidates.append({
                "kind": "plan",
                "file": item["file"],
                "path": str(p),
                "status": item["status"],
                "heading": item.get("heading", ""),
                "summary": item.get("summary", ""),
                "age_days": item.get("age_days"),
            })

    if not candidates:
        return {"updates_proposed": [], "updates_confirmed": [], "applied": [],
                "judge": {"verdict": "skip", "reason": "no open items"}}

    # Include short file bodies so the agent can read intent without extra tools first
    for c in candidates:
        try:
            body = Path(c["path"]).read_text()[:2500]
        except OSError:
            body = ""
        c["body_preview"] = body

    agent_prompt = f"""You are the Homelab Steward work-queue status agent.

For each open idea/plan below, decide whether its **Status** is stale relative to
reality on disk. Investigate with tools when needed (read files, list dirs, check
AGENTS.md / ~/notes / git history). Only propose a status change when evidence is
strong.

Valid idea statuses: idea | planned | done | scrapped
Valid plan statuses: draft | approved | implementing | done | scrapped

Rules:
- If the work described is clearly already implemented on the host, set new_status
  to "done" and move_to_done=true.
- If an idea is still raw thought with no implementation, leave it (omit from updates).
- Do NOT mark something done just because the markdown body talks in past tense —
  verify against the live filesystem / docs.
- Do NOT invent new work. Status hygiene only.
- Prefer fewer, high-confidence updates.

CANDIDATES:
{json.dumps(candidates, indent=2)}

Return ONLY fenced JSON:
```json
{{
  "updates": [
    {{
      "kind": "idea"|"plan",
      "file": "filename.md",
      "path": "/home/carter/ideas/filename.md",
      "current_status": "planned",
      "new_status": "done",
      "move_to_done": true,
      "reason": "short why",
      "evidence": ["concrete evidence strings"]
    }}
  ],
  "unchanged": [{{"file": "...", "reason": "still open because..."}}]
}}
```
"""

    print(f"  status agent: checking {len(candidates)} open item(s)")
    try:
        agent_raw = _call_omp_p(agent_prompt, model=SMALL_MODEL, timeout=600, mode="json")
        agent_packet = _extract_json(agent_raw, "queue-status-agent")
    except Exception as e:
        print(f"  status agent failed: {e}")
        return {
            "updates_proposed": [], "updates_confirmed": [], "applied": [],
            "error": str(e), "judge": {"verdict": "agent_failed"},
        }

    proposed = agent_packet.get("updates") or []
    if not isinstance(proposed, list):
        proposed = []
    # Keep only well-formed updates with a real status change
    clean_proposed = []
    for u in proposed:
        if not isinstance(u, dict):
            continue
        if not u.get("file") and not u.get("path"):
            continue
        ns = (u.get("new_status") or "").strip().lower()
        cs = (u.get("current_status") or "").strip().lower()
        if ns and ns != cs:
            clean_proposed.append(u)

    if not clean_proposed:
        print("  status agent: no status changes proposed")
        return {
            "updates_proposed": [], "updates_confirmed": [], "applied": [],
            "unchanged": agent_packet.get("unchanged", []),
            "judge": {"verdict": "nothing_to_do"},
        }

    judge_prompt = f"""You are a skeptical judge reviewing work-queue status changes
proposed by another agent on Carter's homelab.

For each proposed update, independently verify the evidence. Confirm only if you
agree the item's status should change. Reject weak or wrong claims.

PROPOSED UPDATES:
{json.dumps(clean_proposed, indent=2)}

Open candidates for context:
{json.dumps([{k: c[k] for k in ('kind','file','path','status','heading')} for c in candidates], indent=2)}

Return ONLY fenced JSON:
```json
{{
  "verdict": "pass"|"partial"|"fail",
  "confirmed": [
    {{
      "kind": "idea"|"plan",
      "file": "filename.md",
      "path": "...",
      "current_status": "...",
      "new_status": "done",
      "move_to_done": true,
      "reason": "..."
    }}
  ],
  "rejected": [{{"file": "...", "reason": "why rejected"}}],
  "summary": "one sentence"
}}
```
"""

    print(f"  status judge: reviewing {len(clean_proposed)} proposal(s)")
    try:
        judge_raw = _call_omp_p(judge_prompt, model=SMALL_MODEL, timeout=600, mode="json")
        judge_packet = _extract_json(judge_raw, "queue-status-judge")
    except Exception as e:
        print(f"  status judge failed: {e}")
        return {
            "updates_proposed": clean_proposed, "updates_confirmed": [], "applied": [],
            "error": f"judge failed: {e}",
            "judge": {"verdict": "judge_failed", "summary": str(e)},
        }

    confirmed = judge_packet.get("confirmed") or []
    if not isinstance(confirmed, list):
        confirmed = []

    applied = []
    for u in confirmed:
        # Force kind/file from proposal when missing
        if not u.get("kind") and u.get("file"):
            for p in clean_proposed:
                if p.get("file") == u.get("file"):
                    u.setdefault("kind", p.get("kind", "idea"))
                    u.setdefault("path", p.get("path"))
                    break
        result = _apply_queue_status_update(u, dry_run=dry_run)
        applied.append(result)
        print(f"    {result.get('kind')} {result.get('file')}: {result.get('status')}"
              f" -> {result.get('new_status', result.get('would_set', ''))}")

    return {
        "updates_proposed": clean_proposed,
        "updates_confirmed": confirmed,
        "applied": applied,
        "rejected": judge_packet.get("rejected", []),
        "unchanged": agent_packet.get("unchanged", []),
        "judge": {
            "verdict": judge_packet.get("verdict", "unknown"),
            "summary": judge_packet.get("summary", ""),
        },
    }


def phase_5_work_queue(run_dir, dry_run=False):
    """Phase 5: scan ideas/plans, reconcile stale statuses, consistency checks."""
    print("[P5] work queue scan")

    ideas = _scan_md_files(IDEAS_DIR, default_status="idea")
    plans = _scan_md_files(PLANS_DIR, default_status="draft")

    # Also scan done subdirectories
    ideas_done = _scan_md_files(IDEAS_DIR / "done", default_status="done")
    plans_done = _scan_md_files(PLANS_DIR / "done", default_status="done")

    # Buckets (pre-reconcile)
    ideas_outstanding = [i for i in ideas if i["status"] not in ("done", "scrapped")]
    plans_draft = [p for p in plans if p["status"] == "draft"]
    plans_approved = [p for p in plans if p["status"] == "approved"]
    plans_implementing = [p for p in plans if p["status"] == "implementing"]
    plans_open = plans_draft + plans_approved + plans_implementing

    # Agent + judge: mark completed ideas/plans done when evidence supports it
    reconcile = {"updates_proposed": [], "updates_confirmed": [], "applied": [],
                 "judge": {"verdict": "skipped"}}
    if ideas_outstanding or plans_open:
        if dry_run:
            print("  DRY RUN — status reconcile will not mutate files")
        try:
            reconcile = _reconcile_queue_statuses(
                ideas_outstanding, plans_open, dry_run=dry_run
            )
        except Exception as e:
            print(f"  status reconcile failed: {e}")
            reconcile = {
                "updates_proposed": [], "updates_confirmed": [], "applied": [],
                "error": str(e), "judge": {"verdict": "error"},
            }

        # Re-scan after mutations so the email reflects new reality
        if any(a.get("status") in ("updated", "dry_run") for a in reconcile.get("applied", [])):
            ideas = _scan_md_files(IDEAS_DIR, default_status="idea")
            plans = _scan_md_files(PLANS_DIR, default_status="draft")
            ideas_done = _scan_md_files(IDEAS_DIR / "done", default_status="done")
            plans_done = _scan_md_files(PLANS_DIR / "done", default_status="done")
            ideas_outstanding = [i for i in ideas if i["status"] not in ("done", "scrapped")]
            plans_draft = [p for p in plans if p["status"] == "draft"]
            plans_approved = [p for p in plans if p["status"] == "approved"]
            plans_implementing = [p for p in plans if p["status"] == "implementing"]

    plans_done_this_week = [
        p for p in plans_done
        if (datetime.now() - datetime.fromisoformat(p["mtime"])).days <= 7
    ]
    # Ideas moved to done today also surface under a lightweight note in inconsistencies? No —
    # keep queue clean. Applied updates live in reconcile artifact field.

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
            # Also accept idea still in open dir only if status already done (mid-move)
            if not matching_idea:
                open_match = [i for i in ideas if i["file"] in plan["idea_link"]
                              and i["status"] == "done"]
                if not open_match:
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
        "status_reconcile": reconcile,
    }
    write_json(run_dir / "05-queue.json", data)
    print(f"[P5] done -> {run_dir / '05-queue.json'}")
    print(f"  ideas outstanding: {len(ideas_outstanding)}")
    print(f"  plans: {len(plans_draft)} draft, {len(plans_approved)} approved, "
          f"{len(plans_implementing)} implementing, {len(plans_done_this_week)} done this week")
    print(f"  inconsistencies: {len(inconsistencies)}")
    applied_n = len([a for a in reconcile.get("applied", [])
                     if a.get("status") in ("updated", "dry_run")])
    print(f"  status reconcile: judge={reconcile.get('judge', {}).get('verdict')} "
          f"applied={applied_n}")
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
                # Phase 9 archival copy (top-level YYYY-MM-DD.md, digest_md_path) lives
                # outside the run dir and was never scanned — it has hidden fabricated
                # example.com stories and raw prompt echoes (digest-quality audit gap).
                top_md = topic_dir / f"{d.name}.md"
                if top_md.exists():
                    top_text = top_md.read_text()
                    placeholder_count += len(re.findall(r"\{\{[A-Z_]+\}\}", top_text))
                    placeholder_count += len(re.findall(r"https?://example\.com\b", top_text))
                    placeholder_count += len(re.findall(
                        r"Any notable stories or angles that were missed today", top_text))
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


# Known scanner files to skip in commit diff scan (avoid self-flagging).
_SKIP_SCANNER_FILES = {
    "scripts/steward_runner.py",
}

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
            skip_file = False  # skip content lines when inside a known scanner file
            findings = 0

            for line in log_out.splitlines():
                if line.startswith("commit "):
                    current_commit = line.split()[1][:8]
                    current_date = ""
                    current_file = ""
                    skip_file = False
                    continue
                if line.startswith("Date:"):
                    current_date = line[5:].strip()
                    continue
                if line.startswith("diff --git a/"):
                    parts = line.split(" b/")
                    current_file = parts[-1] if len(parts) > 1 else ""
                    stripped_file = current_file.lstrip("/")
                    skip_file = stripped_file in _SKIP_SCANNER_FILES
                    continue
                if skip_file:
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
         "--since", "7 days ago", "--no-pager"],
        env=env,
    )
    return {
        "hyperliquid_sdk_recent": hyperliquid_log[:3000],
        "dependabot_errors": dependabot_errors[:2000],
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
            "Do NOT exec into containers — the docker images evidence IS the current version. "
            "Checking upstream (GitHub releases, npm registry, go.dev) is allowed; mutations are not."
        ),
    },
    {
        "name": "digest-quality",
        "collector": _audit_collector_3_digest_quality,
        "artifact": "07-audit-3-digests.json",
        "timeout": 600,
        "guidance": (
            "Judge digest quality focusing on the last 48 hours plus any ongoing systemic regressions: "
            "run completeness, story freshness, cross-day duplication, stories-in-flight.json hygiene "
            "(5d cool / 7d prune). Do NOT file historical empty-digest days as findings once they are "
            "already known — only flag recent misses. Sample up to 3 links with curl -sI (read-only)."
        ),
    },
    {
        "name": "security-posture",
        "collector": _audit_collector_4_security,
        "artifact": "07-audit-4-security.json",
        "timeout": 600,
        "guidance": (
            "Judge the security posture from the evidence: listening sockets vs the documented set "
            "(loopback-only: open-webui 48100, searxng 8080, prompt-guard 8090; ufw-gated: llm-proxy 8081, 8082; "
            "LAN: blog 33099, delta 43080), ufw ruleset intact (cni0/flannel.1/docker bridges), unattended-upgrades "
            "active, carter2099.com RDAP expiry (>30d out = ok), CF tunnel ingress vs expected hostnames "
            "(chat, hooks, deltaneutral, freshrss, blog, omp, ssh), SSH failed-password volume. Flag anything unexpected. For repo_secrets: working_tree_issues means secret-pattern files are uncommitted in a repo \u2014 flag each as ATTENTION; commit_issues means a secret-pattern string appeared in recent diffs \u2014 flag as ATTENTION with the commit SHA. No findings = PASS for this sub-check."
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
            "did it fire? outcome? errors?), dependabot-webhook (jobs, failures). Also read recent session files in "
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


def _session_memory_context(days=SESSION_MEMORY_CONTEXT_DAYS,
                            max_chars=SESSION_MEMORY_CONTEXT_MAX):
    """Recent session memoirs (last N day-folders) as a compact markdown block."""
    if not SESSION_MEMOIR_DIR.is_dir():
        return "(no session memory yet)"
    day_dirs = sorted([d for d in SESSION_MEMOIR_DIR.iterdir()
                       if d.is_dir() and re.match(r"^\d{4}-\d{2}-\d{2}$", d.name)],
                      reverse=True)
    parts, total = [], 0
    for d in day_dirs[:days]:
        for f in sorted(d.glob("*.md")):
            try:
                txt = f.read_text(errors="replace").strip()
            except OSError:
                continue
            snippet = txt[:1200]
            parts.append(f"### {d.name}/{f.name}\n{snippet}")
            total += len(snippet)
            if total > max_chars:
                parts.append("…(truncated)")
                break
        if total > max_chars:
            break
    return "\n\n".join(parts) if parts else "(no session memory yet)"


def _run_audit_agent_pair(section, evidence, current_hash, session_memory=""):
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
- CRITICAL: every assistant turn that yields must include that fenced ```json block.
  If the advisor requests changes, emit a REVISED ```json packet — never a prose-only
  ack like "you're right" / "updated above". The JSON is the only durable output.

{_date_context()}

COLLECTED EVIDENCE:
{json.dumps(evidence, indent=2, default=str)[:8000]}

RECENT SESSION MEMORY (Carter's recent interactive omp sessions — context for interpreting homelab state):
{session_memory}
"""
    try:
        worker_text = _call_omp_p(worker_prompt, model=SMALL_MODEL, timeout=section["timeout"], mode="json")
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

RECENT SESSION MEMORY (context for interpreting the state the findings describe):
{session_memory}

Return a fenced ```json packet:
{{"confirmed": [{{"claim": "...", "evidence": "..."}}],
 "rejected": [{{"claim": "...", "reason": "..."}}]}}
- CRITICAL: every yielding turn must include the fenced ```json block. If the advisor
  requests changes, emit a REVISED ```json packet — never a prose-only ack.
"""
    try:
        judge_text = _call_omp_p(judge_prompt, timeout=section["timeout"], mode="json")
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

    session_memory = _session_memory_context()

    # Fan out worker+judge pairs in parallel (cloud model, staggered via pool)
    if to_fire:
        print(f"  fanning out {len(to_fire)} sections (max_workers={MAX_WORKERS})")
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {
                pool.submit(_run_audit_agent_pair, section, evidence, chash, session_memory): (section, artifact_name)
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


def _parse_fix_markdown_table(raw_text, section_name, original_error):
    """Fallback: try to extract fixes from plain text when JSON parsing fails.
    Handles markdown tables OR simple 'PASS' / 'all current' text."""
    text_lower = raw_text.lower()

    # If the agent just said everything is fine / PASS
    if re.search(r"(?i)(all|everything).*(pass|current|fine|ok|clean|up to date|no finding|nothing to)", text_lower):
        return {
            "fixes_applied": [],
            "summary": "All findings already current or clean — no fixes needed.",
            "status": "no_action",
        }

    lines = raw_text.strip().splitlines()
    # Find a markdown table (line with |---| pattern)
    table_start = None
    for i, line in enumerate(lines):
        if re.search(r"\|---\|.*\|---\|", line):
            table_start = i + 1  # data starts after header separator
            break
    if table_start is None or table_start >= len(lines):
        return {"fixes_applied": [], "summary": original_error, "error": original_error}

    fixes = []
    summary_parts = []
    for line in lines[table_start:]:
        line = line.strip()
        if not line or not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.split("|")[1:-1]]
        if len(cells) < 3:
            continue
        finding = re.sub(r"\*\*(.*?)\*\*", r"\1", cells[0]).strip()
        action = re.sub(r"`(.*?)`", r"\1", cells[1]).strip()
        status_cell = cells[2].strip().lower()
        if "fixed" in status_cell or "check" in status_cell or "✅" in status_cell:
            status = "fixed"
        elif "defer" in status_cell or "skip" in status_cell:
            status = "deferred"
        elif "fail" in status_cell:
            status = "failed"
        else:
            status = "fixed"
        fixes.append({"finding": finding, "action": action, "commit": "N/A", "status": status})
        summary_parts.append(finding[:60])

    if fixes:
        summary = "Extracted from markdown table: " + "; ".join(summary_parts[:3])
        return {"fixes_applied": fixes, "summary": summary}
    return {"fixes_applied": [], "summary": original_error, "error": original_error}


def _finding_key(item):
    """Stable key across worker findings, fix rows, and judge reviews."""
    if not isinstance(item, dict):
        return re.sub(r"\s+", " ", str(item or "").strip().lower())[:200]
    raw = (
        item.get("claim")
        or item.get("finding")
        or item.get("evidence")
        or item.get("action")
        or ""
    )
    return re.sub(r"\s+", " ", str(raw).strip().lower())[:200]


def _is_unfixable_note(note):
    """True when judge/fix text says human/deferred/unfixable — don't retry."""
    return bool(re.search(
        r"(?i)\b(unfixable|cannot fix|can't fix|needs? human|manual(ly)?|"
        r"deferred|out of scope|not actionable|unverifiable|"
        r"requires? (carter|human|manual)|correctly deferred)\b",
        note or "",
    ))


def _index_by_finding_key(items):
    """Map finding_key -> item (last write wins). Skips empty keys."""
    out = {}
    for it in items or []:
        if not isinstance(it, dict):
            continue
        k = _finding_key(it)
        if k:
            out[k] = it
    return out


def _remaining_after_judge(findings, judge_packet, fixes_applied):
    """Findings still needing work after a judge pass.

    Keeps only items the judge marked ok=false (or failed fixes with no
    positive review). Drops pass/deferred/unfixable. Unmatched judge
    reviews are returned separately for artifact logging.
    """
    reviewed = judge_packet.get("reviewed") or []
    if not isinstance(reviewed, list):
        reviewed = []
    verdict = str(judge_packet.get("verdict") or "").lower()
    if verdict == "pass":
        return [], [r for r in reviewed if isinstance(r, dict)]

    review_by_key = _index_by_finding_key(reviewed)
    fix_by_key = _index_by_finding_key(fixes_applied)
    matched_review_keys = set()
    remaining = []

    for f in findings or []:
        if not isinstance(f, dict):
            continue
        key = _finding_key(f)
        if not key:
            continue
        rev = review_by_key.get(key)
        fix = fix_by_key.get(key)
        if rev is not None:
            matched_review_keys.add(key)
            ok = rev.get("ok")
            note = str(rev.get("note") or "")
            if ok is True or str(ok).lower() in ("true", "yes", "pass"):
                continue
            if _is_unfixable_note(note):
                continue
            # ok=false (or missing/ambiguous) → retry with judge note
            retry = dict(f)
            retry["prior_judge_note"] = note[:400]
            if fix and fix.get("action"):
                retry["prior_fix_action"] = str(fix.get("action"))[:400]
            if fix and fix.get("status"):
                retry["prior_fix_status"] = fix.get("status")
            remaining.append(retry)
            continue

        # No matching review row — use fix status + overall verdict.
        status = str((fix or {}).get("status") or "").lower()
        if status == "deferred" or _is_unfixable_note(str((fix or {}).get("action") or "")):
            continue
        if status == "fixed" and verdict in ("partial", ""):
            # Judge didn't call this one out; treat as accepted on partial.
            continue
        if status == "failed" or verdict == "fail":
            retry = dict(f)
            if fix and fix.get("action"):
                retry["prior_fix_action"] = str(fix.get("action"))[:400]
            retry["prior_fix_status"] = status or "unknown"
            retry["prior_judge_note"] = str(judge_packet.get("summary") or "judge fail / fix failed")[:400]
            remaining.append(retry)

    unmatched_reviews = [
        r for k, r in review_by_key.items() if k not in matched_review_keys
    ]
    return remaining, unmatched_reviews


def _merge_fixes_applied(iterations):
    """Collapse per-iteration fixes; last write wins per finding key."""
    merged = {}
    order = []
    for it in iterations:
        for fix in it.get("fixes_applied") or []:
            if not isinstance(fix, dict):
                continue
            k = _finding_key(fix) or f"anon-{len(order)}"
            if k not in merged:
                order.append(k)
            row = dict(fix)
            row["iteration"] = it.get("n")
            merged[k] = row
    return [merged[k] for k in order]


def _fix_one_section(section_name, confirmed_findings, dry_run):
    """Fix confirmed findings for one audit section; loop until judge pass or cap.

    Each iteration: fix agent on *remaining* findings only → judge → drop ok/
    unfixable items. Cap via FIX_MAX_ITERS / STEWARD_FIX_MAX_ITERS (default 3).
    """
    if dry_run:
        return {
            "section": section_name,
            "status": "dry-run",
            "findings_count": len(confirmed_findings),
            "fixes_applied": [],
            "judge_verdict": "dry-run",
            "iterations": [],
            "iteration_count": 0,
            "max_iters": FIX_MAX_ITERS,
        }

    if not confirmed_findings:
        return {
            "section": section_name,
            "status": "skipped",
            "reason": "no actionable findings",
            "findings_count": 0,
            "fixes_applied": [],
            "iterations": [],
            "iteration_count": 0,
            "max_iters": FIX_MAX_ITERS,
        }

    remaining = [dict(f) for f in confirmed_findings if isinstance(f, dict)]
    iterations = []
    stop_reason = "max_iters"
    final_judge = {"verdict": "unknown", "summary": "", "reviewed": []}

    for n in range(1, FIX_MAX_ITERS + 1):
        findings_text = json.dumps(remaining, indent=2)
        retry_blurb = ""
        if n > 1:
            retry_blurb = (
                f"This is retry iteration {n}/{FIX_MAX_ITERS}. Prior attempt(s) were "
                f"judged incomplete. Fix ONLY the findings listed below. Each may "
                f"include prior_judge_note / prior_fix_action — address those notes "
                f"with NEW evidence of the fix. Do NOT re-touch findings not listed. "
                f"Do NOT expand scope.\n\n"
            )
        fix_prompt = (
            f"Fix the following homelab issues found by the steward audit "
            f"for section '{section_name}'.\n\n"
            f"{retry_blurb}"
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
            f"- CRITICAL: Return ONLY a fenced ```json code block. No surrounding text, "
            f"no markdown tables, no explanations outside the JSON. The JSON is the ONLY output.\n\n"
            f"FINDINGS:\n{findings_text}\n\n"
            f'Return ONLY this JSON (no other text):\n'
            f'```json\n'
            f'{{"fixes_applied": [{{"finding": "...", "action": "...", '
            f'"commit": "hash or N/A", "status": "fixed"|"deferred"|"failed"}}], '
            f'"summary": "one sentence"}}\n'
            f'```'
        )
        fix_output = ""
        try:
            fix_output = _call_omp_p(fix_prompt, model=SMALL_MODEL, timeout=600, mode="json")
            fix_packet = _extract_json(fix_output, f"fix-{section_name}-i{n}")
        except Exception as e:
            fix_packet = _parse_fix_markdown_table(fix_output, section_name, str(e))

        fixes_applied = fix_packet.get("fixes_applied") or []
        if not isinstance(fixes_applied, list):
            fixes_applied = []

        fixes_json = json.dumps(fixes_applied, indent=2)
        remaining_json = json.dumps(
            [{"claim": f.get("claim") or f.get("finding") or f.get("evidence") or "",
              "prior_judge_note": f.get("prior_judge_note", "")}
             for f in remaining],
            indent=2,
        )
        judge_prompt = (
            f"Review these automated fixes for audit section '{section_name}' "
            f"(iteration {n}/{FIX_MAX_ITERS}).\n\n"
            f"For each fix, verify it was applied correctly by checking the actual "
            f"files/state. Flag any fix that was incorrect, incomplete, or overreaching.\n"
            f"Findings still in scope this iteration:\n{remaining_json}\n\n"
            f"FIXES APPLIED:\n{fixes_json}\n\n"
            f"CRITICAL: Return ONLY a fenced ```json code block. No surrounding text.\n"
            f"Use the same finding text as in FIXES APPLIED / scope list so items match.\n"
            f'```json\n'
            f'{{\n'
            f'  "verdict": "pass"|"partial"|"fail",\n'
            f'  "reviewed": [{{"finding": "...", "ok": true|false, "note": "..."}}],\n'
            f'  "summary": "one sentence"\n'
            f'}}\n'
            f'```'
        )
        judge_output = ""
        try:
            judge_output = _call_omp_p(judge_prompt, model=SMALL_MODEL, timeout=600, mode="json")
            judge_packet = _extract_json(judge_output, f"judge-fix-{section_name}-i{n}")
        except Exception as e:
            # Fail closed — never implicit-pass on prose/NDJSON/empty failures.
            judge_packet = {
                "verdict": "fail",
                "reviewed": [],
                "summary": f"judge extract/call failed: {e}"[:400],
            }

        if not isinstance(judge_packet, dict):
            judge_packet = {"verdict": "fail", "reviewed": [], "summary": "invalid judge packet"}

        next_remaining, unmatched_reviews = _remaining_after_judge(
            remaining, judge_packet, fixes_applied
        )
        iter_rec = {
            "n": n,
            "input_findings_count": len(remaining),
            "fixes_applied": fixes_applied,
            "fix_summary": fix_packet.get("summary", ""),
            "judge_verdict": judge_packet.get("verdict", "unknown"),
            "judge_summary": judge_packet.get("summary", ""),
            "judge_reviewed": judge_packet.get("reviewed") or [],
            "remaining_after": len(next_remaining),
        }
        if unmatched_reviews:
            iter_rec["unmatched_judge_reviews"] = unmatched_reviews
        iterations.append(iter_rec)
        final_judge = judge_packet

        print(
            f"    {section_name} iter {n}/{FIX_MAX_ITERS}: "
            f"judge={iter_rec['judge_verdict']} "
            f"fixes={len(fixes_applied)} remaining={len(next_remaining)}"
        )

        if str(judge_packet.get("verdict") or "").lower() == "pass":
            stop_reason = "pass"
            remaining = []
            break
        if not next_remaining:
            stop_reason = "no_remaining"
            remaining = []
            break

        prev_keys = sorted(_finding_key(f) for f in remaining)
        next_keys = sorted(_finding_key(f) for f in next_remaining)
        if next_keys == prev_keys and n > 1:
            # No progress on which items remain — stop spinning.
            stop_reason = "no_progress"
            remaining = next_remaining
            break
        # Also stop if fix agent applied nothing actionable on a retry
        if n > 1 and not fixes_applied:
            stop_reason = "empty_fix"
            remaining = next_remaining
            break

        remaining = next_remaining
    else:
        # exhausted for-loop without break
        stop_reason = "max_iters"

    merged_fixes = _merge_fixes_applied(iterations)
    last = iterations[-1] if iterations else {}
    return {
        "section": section_name,
        "status": "fixed",
        "findings_count": len(confirmed_findings),
        "actionable_count": len(confirmed_findings),
        "fixes_applied": merged_fixes,
        "fix_summary": last.get("fix_summary", ""),
        "judge_verdict": last.get("judge_verdict", final_judge.get("verdict", "unknown")),
        "judge_summary": last.get("judge_summary", final_judge.get("summary", "")),
        "judge_reviewed": last.get("judge_reviewed", final_judge.get("reviewed") or []),
        "iterations": iterations,
        "iteration_count": len(iterations),
        "max_iters": FIX_MAX_ITERS,
        "stop_reason": stop_reason,
        "remaining_unfixed": len(remaining),
    }


def phase_7b_fix(run_dir, dry_run=False):
    """Phase 7b: auto-fix confirmed findings with fix↔judge loop (capped)."""
    print("[P7b] auto-fix")
    audit_path = run_dir / "07-audit.json"
    if not audit_path.exists():
        print("  skipped — no audit data")
        write_json(run_dir / "07b-fixes.json", {"sections": [], "status": "no_audit"})
        return

    audit = read_json(audit_path)
    sections = audit.get("sections", [])

    # Collect sections with confirmed findings (DRIFT/ATTENTION only)
    to_fix = []
    for s in sections:
        verdict = s.get("verdict", "")
        if verdict not in ("DRIFT", "ATTENTION"):
            continue
        confirmed = s.get("judge_confirmed", [])
        if not confirmed:
            continue
        to_fix.append((s["name"], confirmed))

    if not to_fix:
        print("  skipped — no confirmed findings to fix")
        write_json(run_dir / "07b-fixes.json", {"sections": [], "status": "nothing_to_fix"})
        return

    print(f"  fixing {len(to_fix)} sections (max_workers={MAX_WORKERS}, "
          f"max_iters={FIX_MAX_ITERS})")
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
                iters = r.get("iteration_count") or len(r.get("iterations") or []) or 1
                stop = r.get("stop_reason", "")
                jv_disp = f"{jv}@{iters}" if iters else jv
                extra = f", stop={stop}" if stop and stop not in ("pass", "") else ""
                print(f"    {name}: {r['status']} — {applied} fixes, judge: {jv_disp}{extra}")

    # Sort to canonical section order
    order = {s["name"]: i for i, s in enumerate(AUDIT_SECTIONS)}
    fix_results.sort(key=lambda r: order.get(r.get("section", ""), 99))

    master = {"sections": fix_results, "status": "done"}
    write_json(run_dir / "07b-fixes.json", master)

    total_fixes = sum(len(r.get("fixes_applied", [])) for r in fix_results)
    judge_oks = sum(1 for r in fix_results if r.get("judge_verdict") == "pass")
    multi = sum(1 for r in fix_results if (r.get("iteration_count") or 0) > 1)
    print(f"[P7b] done -> {run_dir / '07b-fixes.json'} "
          f"({total_fixes} fixes across {len(fix_results)} sections, "
          f"{judge_oks} judge-pass, {multi} multi-iter)")
    return master

def _html_updates(applied_data):
    """Render update steps — signal only, no no-op greys."""
    steps = applied_data.get("steps", [])
    if not steps:
        if applied_data.get("dry_run"):
            return '<p style="margin:0; color:#888; font-size:13px;">Dry run — no mutations applied.</p>'
        return '<p style="margin:0; color:#888; font-size:13px;">No update steps executed.</p>'

    lines = []
    for s in steps:
        name = s.get("step", "")
        status = s.get("status", "")

        # apt_upgrade: show only if upgrades happened or failed
        if name == "apt_upgrade":
            n = s.get("upgraded_count", 0)
            if n > 0:
                lines.append(f'<p style="margin:0 0 4px; color:#2a2a36; font-size:13px;">'
                             f'apt: {n} packages upgraded</p>')
            elif status == "failed":
                lines.append(f'<p style="margin:0 0 4px; color:#c62828; font-size:13px;">'
                             f'apt: FAILED — {s.get("error","")}</p>')

        # auto_* packages: show only ok or failed
        elif name.startswith("auto_"):
            pkg = name.replace("auto_", "")
            if status == "ok":
                lines.append(f'<p style="margin:0 0 4px; color:#2a2a36; font-size:13px;">'
                             f'{pkg}: {s.get("pre_version","?")} -> {s.get("post_version","?")}</p>')
            elif status in ("failed", "error"):
                lines.append(f'<p style="margin:0 0 4px; color:#c62828; font-size:13px;">'
                             f'{pkg}: FAILED — {s.get("error","")}</p>')

        # openwebui: show only bumped/failed/error
        elif name == "openwebui":
            if status == "bumped":
                lines.append(f'<p style="margin:0 0 4px; color:#2a2a36; font-size:13px;">'
                             f'open-webui: {s.get("current_tag")} -> {s.get("latest_tag")}</p>')
            elif status in ("failed", "error"):
                lines.append(f'<p style="margin:0 0 4px; color:#c62828; font-size:13px;">'
                             f'open-webui: {status} — {s.get("error",s.get("reason",""))}</p>')

        # freshrss: show only bumped/failed/error
        elif name == "freshrss":
            if status == "bumped":
                lines.append(f'<p style="margin:0 0 4px; color:#2a2a36; font-size:13px;">'
                             f'freshrss: {s.get("current_tag")} -> {s.get("latest_tag")}</p>')
            elif status in ("failed", "error"):
                lines.append(f'<p style="margin:0 0 4px; color:#c62828; font-size:13px;">'
                             f'freshrss: {status} — {s.get("error",s.get("reason",""))}</p>')

        # herdr: show only bumped or failed/error
        elif name == "herdr_update":
            pre = str(s.get("pre_version", "?")).replace("herdr ", "")
            post = str(s.get("post_version", "?")).replace("herdr ", "")
            if status == "ok":
                lines.append(f'<p style="margin:0 0 4px; color:#2a2a36; font-size:13px;">'
                             f'herdr: {pre} -> {post}</p>')
            elif status in ("failed", "error"):
                lines.append(f'<p style="margin:0 0 4px; color:#c62828; font-size:13px;">'
                             f'herdr: {status} — {s.get("error",s.get("reason",""))}</p>')

        # Generic fallback: show any step with real change/failure
        else:
            if status in ("ok", "bumped"):
                lines.append(f'<p style="margin:0 0 4px; color:#2a2a36; font-size:13px;">'
                             f'{name}: ok</p>')
            elif status in ("failed", "error"):
                lines.append(f'<p style="margin:0 0 4px; color:#c62828; font-size:13px;">'
                             f'{name}: FAILED — {s.get("error","")}</p>')

    return "\n".join(lines) if lines else '<p style="margin:0; color:#888; font-size:13px;">No updates tonight.</p>'





def _mini_bar(pct, color="#37474f"):
    """Inline 0-100% horizontal bar, email-safe via nested table cells.
    Track is darker (#d0d4de). Min fill 2% when pct>0."""
    try:
        w = max(0.0, min(100.0, float(pct)))
    except (TypeError, ValueError):
        w = 0.0
    fill_w = max(w, 2.0) if w > 0 else 0.0
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" width="100%" '
        'style="border-collapse:collapse;"><tr>'
        f'<td width="{fill_w:.0f}%" style="background-color:{color}; height:5px; '
        f'line-height:5px; font-size:0;">&nbsp;</td>'
        f'<td width="{100-fill_w:.0f}%" style="background-color:#d0d4de; height:5px; '
        f'line-height:5px; font-size:0;">&nbsp;</td>'
        '</tr></table>'
    )


def _html_health(validation_data, hb_data):
    """Merged health section: validation checks + system status from heartbeat.
    No 30-day trends."""
    out = []

    # If heartbeat phase failed, show the error
    if hb_data.get("phase_failed"):
        err = hb_data.get("error", "unknown error")
        out.append(f'<p style="margin:0; color:#c62828; font-size:13px;">{_dot("danger")}Heartbeat failed: {err}</p>')
        return "".join(out)

    # ── A. Checks table ──
    checks = validation_data.get("checks", [])
    check_rows = []
    for c in checks:
        name = c.get("name", "")
        status = c.get("status", "")

        # Normalize label
        label_map = {
            "docker_containers": "Containers",
            "k3s_pods": "k3s pods",
            "llm_fallback": "LLM routing",
            "openwebui_image_match": "open-webui image",
        }
        label = label_map.get(name, name)
        if name.startswith("endpoint_"):
            svc = name.replace("endpoint_", "")
            label = {"tunnel-health": "CF tunnel"}.get(svc, svc)

        if status == "ok":
            chip = _chip("OK", "#2e7d32")
            detail = ""
            # Add compact detail for endpoints
            if name.startswith("endpoint_"):
                svc = name.replace("endpoint_", "")
                if svc == "tunnel-health":
                    detail = f'{c.get("active_connections", "?")} connectors'
                else:
                    code = c.get("http_code", "?")
                    detail = f'HTTP {code}'
            elif name == "llm_fallback":
                fb = c.get("fallback_active", False)
                chip = _chip("OK", "#2e7d32") if not fb else _chip("FAIL", "#c62828")
                detail = "local" if not fb else "cloud fallback"
                label = "LLM routing"
            check_rows.append((label, f'{chip} {detail}'.strip()))
        elif status in ("fail", "error"):
            chip = _chip("FAIL", "#c62828")
            detail = c.get("error", c.get("status", ""))
            check_rows.append((label, f'{chip} {detail}'.strip()))
        elif status == "warning":
            chip = _chip("WARN", "#e65100")
            check_rows.append((label, chip))

    if check_rows:
        out.append(_sub_header("Checks"))
        out.append(_kv_rows(check_rows))

    # ── B. System status (from heartbeat, no trends) ──
    sys_rows = []

    # Failed systemd units
    uf = hb_data.get("failed_units", {}) or {}
    user_f = [x for x in uf.get("user", []) if x and str(x).strip()]
    sys_f = [x for x in uf.get("system", []) if x and str(x).strip()]
    cleared = [c for c in (uf.get("cleared") or []) if c]
    missing = (hb_data.get("units", {}) or {}).get("missing", [])
    if user_f or sys_f or missing or cleared:
        parts = []
        for u in user_f:
            parts.append(_dot("danger") + html.escape(str(u).strip()))
        for u in sys_f:
            parts.append(_dot("danger") + html.escape(str(u).strip()))
        if missing:
            parts.append(_dot("warn") + "missing: " + html.escape(", ".join(map(str, missing))))
        if cleared and not (user_f or sys_f):
            # Only mention clears when nothing is still failed — avoids noise
            clr = ", ".join(
                html.escape(str(c.get("unit") or c)) for c in cleared[:4]
            )
            parts.append(_dot("ok") + f"cleared stale: {clr}")
        sys_rows.append(("Systemd units", " ".join(parts)))

    # Reboot
    rb = hb_data.get("reboot", {}) or {}
    sys_rows.append(("Reboot",
        (_dot("danger") + f'Needed — kernel {rb.get("kernel","?")}' if rb.get("needed")
         else _dot("ok") + "Not needed")))

    # Disk
    disk = hb_data.get("disk", {}) or {}
    if disk.get("df_root"):
        parts = disk["df_root"].splitlines()[-1].split()
        if len(parts) >= 5:
            used_pct = parts[4]
            sys_rows.append(("Disk", f'{used_pct} used ({parts[2]}/{parts[1]})'))

    # Memory
    mem = hb_data.get("memory", {}) or {}
    mem_avail = mem.get("available", "")
    if mem_avail:
        sys_rows.append(("Memory", mem_avail))

    # Backup
    bt = (hb_data.get("backup", {}) or {}).get("last_run", "")
    if bt:
        sys_rows.append(("Last backup", bt))

    # DNS — only if not all ok
    dns = hb_data.get("dns", {}) or {}
    if dns:
        ok = sum(1 for v in dns.values() if v.get("resolves"))
        total = len(dns)
        if ok != total:
            sys_rows.append(("DNS", _dot("warn") + f'{ok}/{total} hostnames resolve'))

    # TLS — only if any cert expires within 30 days
    tls = hb_data.get("tls_certs", {}) or {}
    if tls:
        now_dt = datetime.now()
        expiring = []
        for host, expiry in tls.items():
            dm = re.search(r"notAfter=(.+?\d{4})\s", expiry)
            if dm:
                try:
                    exp_date = datetime.strptime(dm.group(1), "%b %d %H:%M:%S %Y %Z")
                    days_left = (exp_date - now_dt).days
                    if days_left <= 30:
                        expiring.append(f'{host.split(".")[0]} ({days_left}d)')
                except ValueError:
                    pass
        if expiring:
            sys_rows.append(("TLS expiring", _dot("warn") + ", ".join(expiring)))

    # LLM routing (from heartbeat)
    fb = (hb_data.get("llm_stack", {}) or {}).get("falling_back", False)
    if fb:
        sys_rows.append(("LLM proxy", _dot("warn") + "Cloud fallback"))

    # bundle-audit — only if vulnerabilities found
    ba = hb_data.get("bundle_audit", {}) or {}
    for app, result in ba.items():
        if "no vulnerabilities" not in str(result):
            sys_rows.append((f"bundle-audit ({app})", _dot("warn") + "vulnerabilities"))

    if sys_rows:
        out.append(_sub_header("System status"))
        out.append(_kv_rows(sys_rows))

    return "".join(out) if out else '<p style="margin:0; color:#888; font-size:13px;">All health checks passed.</p>'


def _is_noop_fix(fix):
    """True when a 'fix' is just confirming something already clean/current."""
    blob = f"{fix.get('action', '')} {fix.get('finding', '')} {fix.get('status', '')}"
    return bool(re.search(
        r"(?i)no action required|no action needed|no fix needed|already clean|"
        r"nothing to|verified:.*no action|already current|is current|"
        r"confirmed current|no discrepancies|require no action|nothing to fix|"
        r"no changes? (needed|required)|all (findings? )?(current|clean|fine)",
        blob,
    ))


def _real_fixes(section_fixes):
    """Meaningful fixes only — drop no-op 'already current' rows."""
    return [
        f for f in (section_fixes or [])
        if f.get("status") in ("fixed", "failed", "deferred") and not _is_noop_fix(f)
    ]


def _section_findings_text(sec, date_str):
    """Flatten confirmed findings for summarizer input (digest-stale filtered)."""
    confirmed = sec.get("judge_confirmed", []) or sec.get("confirmed_findings", []) or []
    claims = []
    for f in confirmed:
        claim = (f.get("claim") or f.get("evidence") or "").strip()
        if not claim:
            continue
        if sec.get("name") == "digest-quality":
            dates_in_claim = re.findall(r"20\d{2}-\d{2}-\d{2}", claim)
            if dates_in_claim:
                has_recent = any(
                    (datetime.strptime(d, "%Y-%m-%d")
                     >= datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=2))
                    for d in dates_in_claim
                )
                if not has_recent:
                    continue
        claims.append(claim[:240])
    return claims


def _deterministic_section_summary(name, findings, fixes, fix_summary=""):
    """Fallback one-liner when the LLM summary call fails."""
    n_fixed = sum(1 for f in fixes if f.get("status") == "fixed")
    n_def = sum(1 for f in fixes if f.get("status") == "deferred")
    n_fail = sum(1 for f in fixes if f.get("status") == "failed")
    parts = []
    if fix_summary:
        parts.append(fix_summary.strip().rstrip(".") + ".")
    elif findings:
        parts.append(f"{len(findings)} finding(s) reviewed.")
    else:
        parts.append("Section reviewed.")
    act = []
    if n_fixed:
        act.append(f"{n_fixed} fixed")
    if n_def:
        act.append(f"{n_def} deferred")
    if n_fail:
        act.append(f"{n_fail} failed")
    if act:
        parts.append("Actions: " + ", ".join(act) + ".")
    elif not fixes:
        parts.append("No automated fixes applied.")
    return " ".join(parts)


def _summarize_audit_sections(section_payloads):
    """One LLM call → {section_name: summary_text}. Falls back per-section."""
    if not section_payloads:
        return {}

    compact = []
    for p in section_payloads:
        row = {
            "name": p["name"],
            "verdict": p["verdict"],
            "findings": p["findings"][:12],
            "fixes": [
                {
                    "status": f.get("status"),
                    "finding": (f.get("finding") or "")[:160],
                    "action": (f.get("action") or "")[:160],
                }
                for f in p["fixes"][:12]
            ],
            "fix_summary": (p.get("fix_summary") or "")[:300],
            "judge_summary": (p.get("judge_summary") or "")[:300],
        }
        iters = p.get("iteration_count") or 0
        if iters > 1:
            row["fix_iterations"] = iters
            row["fix_stop_reason"] = p.get("stop_reason") or ""
            if p.get("remaining_unfixed"):
                row["remaining_unfixed"] = p.get("remaining_unfixed")
        jv = p.get("judge_verdict") or ""
        if jv:
            row["fix_judge_verdict"] = jv
        compact.append(row)

    prompt = (
        "You summarize Homelab Steward audit+fix results for a nightly email.\n\n"
        "For EACH section below, write 1-3 plain-English sentences a human can skim.\n"
        "Cover: what was wrong (if anything), what got fixed, what remains / was deferred.\n"
        "If fix_iterations > 1, briefly note that the fix needed a retry (e.g. 'cleared on "
        "second try') — don't dwell on loop mechanics.\n"
        "No badge jargon (DRIFT/ATTENTION). No bullet lists. No filenames unless load-bearing.\n"
        "If findings are all 'already current' noise and fixes are empty, say versions/state "
        "were verified current.\n\n"
        f"SECTIONS:\n{json.dumps(compact, indent=2)}\n\n"
        "Return ONLY fenced JSON:\n"
        '```json\n'
        '{"summaries": {"section-name": "one to three sentences", "...": "..."}}\n'
        "```"
    )

    try:
        raw = _call_omp_p(prompt, model=SMALL_MODEL, timeout=120, mode="json")
        packet = _extract_json(raw, "audit-section-summaries")
        summaries = packet.get("summaries") or {}
        if not isinstance(summaries, dict):
            raise ValueError("summaries not a dict")
        # Normalize keys to bare section names
        out = {}
        for p in section_payloads:
            name = p["name"]
            text = summaries.get(name) or summaries.get(name.replace("_", " "))
            if isinstance(text, str) and text.strip():
                out[name] = text.strip()
        if out:
            return out
    except Exception as e:
        print(f"  audit section summary LLM failed: {e}")

    return {
        p["name"]: _deterministic_section_summary(
            p["name"], p["findings"], p["fixes"], p.get("fix_summary", "")
        )
        for p in section_payloads
    }


def _html_audit(audit_data, fixes_data=None):
    """Render audit sections as LLM summaries with 'N fixes' badges."""
    sections = audit_data.get("sections", []) or []
    if not sections:
        return '<p style="margin:0; color:#9aa0b2; font-size:13px;">No audit results.</p>'

    # Index fixes + fix_summary by section name
    fixes_by_section = {}
    fix_meta = {}
    if fixes_data:
        for s in fixes_data.get("sections", []):
            sname = s.get("section", "")
            fixes_by_section[sname] = s.get("fixes_applied", [])
            fix_meta[sname] = {
                "fix_summary": s.get("fix_summary", ""),
                "judge_summary": s.get("judge_summary", ""),
                "judge_verdict": s.get("judge_verdict", ""),
                "iteration_count": s.get("iteration_count") or len(s.get("iterations") or []) or 0,
                "stop_reason": s.get("stop_reason", ""),
                "remaining_unfixed": s.get("remaining_unfixed", 0),
            }

    date_str = datetime.now().strftime("%Y-%m-%d")

    # Build payloads for non-clean sections
    payloads = []
    for sec in sections:
        verdict = sec.get("verdict", "UNKNOWN")
        if verdict in ("PASS", "cached-PASS"):
            continue
        name = sec.get("name", "unknown") or "unknown"
        findings = _section_findings_text(sec, date_str)
        fixes = _real_fixes(fixes_by_section.get(name, []))
        meta = fix_meta.get(name, {})
        payloads.append({
            "name": name,
            "verdict": verdict,
            "findings": findings,
            "fixes": fixes,
            "fix_summary": meta.get("fix_summary", ""),
            "judge_summary": meta.get("judge_summary", ""),
            "judge_verdict": meta.get("judge_verdict", ""),
            "iteration_count": meta.get("iteration_count") or 0,
            "stop_reason": meta.get("stop_reason", ""),
            "remaining_unfixed": meta.get("remaining_unfixed") or 0,
            "error": (sec.get("error") or sec.get("worker_error") or "").strip(),
            "sec": sec,
        })

    if not payloads:
        return '<p style="margin:0; color:#888; font-size:13px;">All audit sections clear.</p>'

    summaries = _summarize_audit_sections(payloads)

    out = []
    for p in payloads:
        name = p["name"]
        display = name.replace("_", " ")
        verdict = p["verdict"]
        fixes = p["fixes"]
        n_fixes = len(fixes)
        n_failed = sum(1 for f in fixes if f.get("status") == "failed")
        n_deferred = sum(1 for f in fixes if f.get("status") == "deferred")

        # Badge: always "N fixes" (user-facing). Color by outcome.
        if verdict in ("collector-failed", "worker-failed"):
            chip = _chip("Check failed", "#c62828")
        elif n_failed:
            chip = _chip(f"{n_fixes} fixes", "#c62828")
        elif n_deferred and not any(f.get("status") == "fixed" for f in fixes):
            chip = _chip(f"{n_fixes} fixes", "#e65100")
        elif n_fixes:
            chip = _chip(f"{n_fixes} fixes", "#2e7d32")
        else:
            # Reviewed / noise-only — still report 0 fixes, not "Needs attention"
            chip = _chip("0 fixes", "#9aa0b2")

        out.append(
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            'style="font-size:13px; border-collapse:collapse; margin:0 0 12px;">'
            f'<tr><td style="padding:3px 0; color:#1a1a2e; font-weight:700;">{html.escape(display)}</td>'
            f'<td align="right" style="padding:3px 0; white-space:nowrap;">{chip}</td></tr>'
        )

        if verdict in ("collector-failed", "worker-failed") and p["error"]:
            err = p["error"]
            m = re.match(r"Could not extract JSON from ([^.]+)\.\s*Raw text.*", err, re.S)
            if m:
                short = f"Could not extract JSON from {m.group(1)} (agent returned prose)"
            else:
                short = err.split("\n", 1)[0][:160]
            out.append(
                f'<tr><td colspan="2" style="padding:2px 4px 2px 14px; color:#c62828; '
                f'font-size:12px;">{html.escape(short)}</td></tr>'
            )

        summary = summaries.get(name) or _deterministic_section_summary(
            name, p["findings"], fixes, p.get("fix_summary", "")
        )
        out.append(
            f'<tr><td colspan="2" style="padding:4px 4px 2px 0; color:#3a3a4a; '
            f'font-size:12px; line-height:1.45;">{html.escape(summary)}</td></tr>'
        )
        out.append('</table>')

    return "".join(out)


def _html_queue(queue_data):
    """Render work queue as grouped tables."""
    ideas = queue_data.get("ideas", {}) or {}
    plans = queue_data.get("plans", {}) or {}
    inconsistencies = queue_data.get("inconsistencies", []) or []
    reconcile = queue_data.get("status_reconcile", {}) or {}
    out = []

    # Ideas
    outstanding = ideas.get("outstanding", [])
    if outstanding:
        out.append(_sub_header(f'Ideas outstanding ({len(outstanding)})'))
        idea_rows = []
        for idea in outstanding[:10]:
            age = idea.get("age_days", "?")
            heading = idea.get("heading", "") or idea.get("file", "")
            idea_rows.append((f'{age}d', heading))
        out.append(_kv_rows(idea_rows))
    else:
        out.append('<p style="margin:0; color:#9aa0b2; font-size:13px;">No open ideas.</p>')

    # Plans
    plan_groups = [
        ("Draft", plans.get("draft", []), "#1565c0"),
        ("Approved", plans.get("approved", []), "#2e7d32"),
        ("Implementing", plans.get("implementing", []), "#e65100"),
        ("Done this week", plans.get("done_this_week", []), "#9aa0b2"),
    ]
    non_empty = [(label, items, color) for label, items, color in plan_groups if items]
    if non_empty:
        plan_rows = []
        for label, items, color in non_empty:
            for item in items[:5]:
                detail = item.get("heading", item.get("file", "")) or ""
                plan_rows.append((_chip(label, color), detail))
        out.append(_sub_header("Plans"))
        out.append(_kv_rows(plan_rows))

    # Status reconciles applied this run
    applied = [
        a for a in (reconcile.get("applied") or [])
        if a.get("status") in ("updated", "dry_run")
    ]
    if applied:
        out.append(_sub_header(f'Status updates ({len(applied)})'))
        rows = []
        for a in applied[:8]:
            label = a.get("new_status") or a.get("would_set") or a.get("status")
            detail = a.get("file", "?")
            reason = a.get("reason") or ""
            if reason:
                detail = f"{detail} — {reason[:100]}"
            rows.append((_chip(str(label), "#2e7d32"), detail))
        out.append(_kv_rows(rows))

    # Inconsistencies
    if inconsistencies:
        out.append(_sub_header("Inconsistencies"))
        inc_rows = []
        for inc in inconsistencies:
            inc_rows.append((inc["type"], inc["detail"][:200]))
        out.append(_kv_rows(inc_rows))

    return "".join(out) if out else \
        '<p style="margin:0; color:#9aa0b2; font-size:13px;">Queue empty.</p>'


def _html_usage(usage_data):
    """Render OpenCode Go usage report with higher contrast bars.

    Windows match opencode-go-proxy / ocusage: rolling is 5h (not 24h).
    Each row shows reset_in when the proxy provides it.
    """
    accounts = usage_data.get("accounts", []) or []
    out = []
    for acct in accounts:
        name = acct.get("name", "?")
        tier = acct.get("tier", "?")
        extra_parts = [tier]
        if acct.get("payg_balance") is not None:
            extra_parts.append(f'PAYG ${acct["payg_balance"]:.2f}')
        extra = " · ".join(extra_parts)
        out.append(
            f'<p style="margin:0 0 3px; font-size:13px;">'
            f'<strong style="color:#1a1a2e;">{html.escape(str(name))}</strong> '
            f'<span style="color:#9aa0b2; font-size:12px;">{html.escape(extra)}</span></p>'
        )
        rows = (
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            'style="font-size:12px; border-collapse:collapse; margin-bottom:10px;">'
        )
        for label, pct_key, reset_key, color in [
            ("5h", "rolling_pct", "rolling_reset_in", "#1a1a2e"),
            ("7d", "weekly_pct", "weekly_reset_in", "#0d47a1"),
            ("30d", "monthly_pct", "monthly_reset_in", "#4a148c"),
        ]:
            pct = acct.get(pct_key, 0)
            try:
                pct = float(pct)
            except (TypeError, ValueError):
                pct = 0.0
            fill_color = "#c62828" if pct >= 90 else color
            reset_in = (acct.get(reset_key) or "").strip()
            pct_cell = f"{pct:.0f}%"
            if reset_in:
                pct_cell += (
                    f' <span style="color:#9aa0b2; font-weight:400; font-size:11px;">'
                    f'· reset {html.escape(reset_in)}</span>'
                )
            rows += (
                '<tr>'
                f'<td width="18%" style="padding:3px 0; color:#7b7b8a; font-size:12px; '
                f'vertical-align:middle;">{label}</td>'
                f'<td width="42%" style="padding:3px 0; vertical-align:middle;">'
                f'{_mini_bar(pct, fill_color)}</td>'
                f'<td align="right" width="40%" style="padding:3px 0; color:#2a2a36; '
                f'font-weight:600; vertical-align:middle; white-space:nowrap;">{pct_cell}</td>'
                '</tr>'
            )
        rows += '</table>'
        out.append(rows)
    if usage_data.get("proxy_error"):
        out.append(
            f'<p style="margin:6px 0 0; color:#c62828; font-size:12px;">'
            f'{_dot("danger")}Proxy unreachable: '
            f'{html.escape(str(usage_data["proxy_error"]))}</p>'
        )
    if not out:
        out.append('<p style="margin:0; color:#9aa0b2; font-size:13px;">No usage data.</p>')
    return "".join(out)


def _tldr_collect_updates(applied):
    """Real package/image changes only (not already-current skips)."""
    updates = []
    n_failed = 0
    for s in applied.get("steps", []) or []:
        step = s.get("step", "")
        status = s.get("status", "")
        if status == "failed":
            n_failed += 1
            updates.append(f"{step} failed: {str(s.get('error', ''))[:80]}")
            continue
        if step == "apt_upgrade" and s.get("upgraded_count", 0) > 0:
            updates.append(f"apt: {s['upgraded_count']} packages upgraded")
        elif step.startswith("auto_") and status == "ok":
            pre, post = s.get("pre_version", "?"), s.get("post_version", "?")
            pkg = step.replace("auto_", "")
            if pre != post:
                updates.append(f"{pkg}: {pre} -> {post}")
        elif step == "openwebui" and status == "bumped":
            updates.append(f"open-webui: {s.get('current_tag')} -> {s.get('latest_tag')}")
        elif step == "freshrss" and status == "bumped":
            updates.append(f"freshrss: {s.get('current_tag')} -> {s.get('latest_tag')}")
        elif step == "herdr_update" and status == "ok":
            pre = str(s.get("pre_version", "")).replace("herdr ", "")
            post = str(s.get("post_version", "")).replace("herdr ", "")
            if pre and post and pre != post:
                updates.append(f"herdr: {pre} -> {post}")
    return updates, n_failed


def _tldr_collect_health(heartbeat, validation=None):
    """End-state host issues (empty list = healthy)."""
    issues = []
    if heartbeat.get("phase_failed"):
        issues.append(f"heartbeat failed: {str(heartbeat.get('error', ''))[:80]}")
    rb = heartbeat.get("reboot", {}) or {}
    if rb.get("needed"):
        issues.append("reboot needed")
    if (heartbeat.get("llm_stack", {}) or {}).get("falling_back", False):
        issues.append("LLM on cloud fallback")
    for c in (validation or {}).get("checks", []) or []:
        if not isinstance(c, dict):
            continue
        st = str(c.get("status") or "ok")
        if st in ("ok", "pass", "skipped", "dry-run"):
            continue
        name = c.get("name") or c.get("endpoint") or "check"
        issues.append(f"{name}: {st}")
    return issues


def _tldr_audit_end_state(audit, fixes):
    """Classify each audit section by post-P7b end state.

    Returns dict with open / cleared / failed / deferred_only lists of
    short human labels — not P7 pre-fix verdict counts.
    """
    fix_by = {}
    for s in (fixes or {}).get("sections", []) or []:
        name = s.get("section") or ""
        if name:
            fix_by[name] = s

    open_items = []
    cleared = []
    failed = []
    deferred_only = []

    for sec in (audit or {}).get("sections", []) or []:
        name = sec.get("name") or "unknown"
        display = name.replace("_", " ")
        verdict = str(sec.get("verdict") or "")
        base = verdict.removeprefix("cached-")

        if verdict.endswith("-failed") or base in ("collector-failed", "worker-failed"):
            err = (sec.get("error") or sec.get("worker_error") or "")[:120]
            failed.append({
                "section": name,
                "label": display,
                "note": err or verdict,
            })
            continue

        if base in ("PASS",) or verdict in ("PASS", "cached-PASS", "dry-run-collector-only"):
            # Clean section — only mention if P7b still ran (shouldn't) 
            continue

        fx = fix_by.get(name)
        if not fx:
            # Non-PASS audit with no fix pass (nothing_to_fix gap or skipped)
            if base in ("DRIFT", "ATTENTION", "UNVERIFIABLE"):
                open_items.append({
                    "section": name,
                    "label": display,
                    "note": f"audit {base.lower()}; no auto-fix result",
                })
            continue

        jv = str(fx.get("judge_verdict") or "").lower()
        rem = int(fx.get("remaining_unfixed") or 0)
        real = _real_fixes(fx.get("fixes_applied") or [])
        n_fixed = sum(1 for f in real if f.get("status") == "fixed")
        n_failed_fx = sum(1 for f in real if f.get("status") == "failed")
        n_def = sum(1 for f in real if f.get("status") == "deferred")
        note = (fx.get("judge_summary") or fx.get("fix_summary") or "")[:180]
        iters = fx.get("iteration_count") or len(fx.get("iterations") or []) or 0

        if jv == "pass" and rem <= 0 and n_failed_fx == 0:
            if n_fixed == 0 and n_def > 0:
                deferred_only.append({
                    "section": name,
                    "label": display,
                    "note": note or f"{n_def} deferred",
                })
            else:
                entry = {"section": name, "label": display, "fixed": n_fixed}
                if iters > 1:
                    entry["iterations"] = iters
                cleared.append(entry)
            continue

        # partial / fail / remaining / failed fixes → still open for Carter
        if jv in ("partial", "fail", "failed", "unknown") or rem > 0 or n_failed_fx:
            why = jv or "open"
            if rem > 0:
                why = f"{why}, {rem} unfixed"
            open_items.append({
                "section": name,
                "label": display,
                "note": note or why,
                "judge": jv,
                "remaining_unfixed": rem,
            })
            continue

        # dry-run / skipped / odd statuses
        if jv in ("dry-run", "skipped"):
            continue
        if n_fixed or base in ("DRIFT", "ATTENTION"):
            cleared.append({"section": name, "label": display, "fixed": n_fixed})

    return {
        "open": open_items,
        "cleared": cleared,
        "failed": failed,
        "deferred_only": deferred_only,
    }


def _build_tldr_facts(applied, audit, queue, fixes, heartbeat, validation=None):
    """End-state facts for TL;DR (LLM + deterministic)."""
    updates, n_failed_apply = _tldr_collect_updates(applied)
    health_issues = _tldr_collect_health(heartbeat, validation)
    audit_state = _tldr_audit_end_state(audit, fixes)

    n_real_fixes = 0
    for s in (fixes or {}).get("sections", []) or []:
        n_real_fixes += sum(
            1 for f in _real_fixes(s.get("fixes_applied") or [])
            if f.get("status") == "fixed"
        )

    plans = (queue or {}).get("plans", {}) or {}
    needs_carter = []
    for item in plans.get("approved", []) or []:
        needs_carter.append(
            f"approved plan: {item.get('heading') or item.get('file') or '?'}"
        )
    for item in plans.get("implementing", []) or []:
        age = item.get("age_days")
        label = item.get("heading") or item.get("file") or "?"
        if age is not None and age > 2:
            needs_carter.append(f"stale implementing plan ({age}d): {label}")

    for o in audit_state["open"]:
        needs_carter.append(
            f"audit still open — {o['label']}: {o.get('note') or o.get('judge') or 'needs review'}"
        )
    for f in audit_state["failed"]:
        needs_carter.append(f"audit check failed — {f['label']}: {f.get('note') or ''}")

    return {
        "health_ok": not health_issues,
        "health_issues": health_issues,
        "updates": updates,
        "n_failed_apply": n_failed_apply,
        "audit_open": audit_state["open"],
        "audit_cleared": audit_state["cleared"],
        "audit_failed": audit_state["failed"],
        "audit_deferred": audit_state["deferred_only"],
        "n_sections_cleared": len(audit_state["cleared"]),
        "n_sections_open": len(audit_state["open"]) + len(audit_state["failed"]),
        "n_real_fixes": n_real_fixes,
        "ideas_outstanding": (queue or {}).get("ideas", {}).get("total_outstanding", 0) or 0,
        "plans_approved": len(plans.get("approved") or []),
        "needs_carter": needs_carter,
    }


def _build_tldr(applied, audit, queue, fixes, heartbeat, date_str, session_memory="",
                validation=None):
    """Build end-state TL;DR with LLM summary + deterministic fallback.

    Facts describe how the host looks *after* the run (open vs cleared),
    not pre-fix audit process counters.
    Returns HTML-safe paragraph.
    """
    facts = _build_tldr_facts(applied, audit, queue, fixes, heartbeat, validation)

    # Compact payload for the model — end state first
    llm_facts = {
        "date": date_str,
        "health_ok": facts["health_ok"],
        "health_issues": facts["health_issues"],
        "needs_carter": facts["needs_carter"][:8],
        "updates": facts["updates"][:6],
        "audit_still_open": [
            {"section": o["label"], "detail": (o.get("note") or "")[:160]}
            for o in facts["audit_open"][:6]
        ],
        "audit_checks_failed": [
            {"section": f["label"], "detail": (f.get("note") or "")[:120]}
            for f in facts["audit_failed"][:4]
        ],
        "audit_cleared_sections": [
            {
                "section": c["label"],
                "fixes": c.get("fixed", 0),
                **({"retries": c["iterations"]} if c.get("iterations") else {}),
            }
            for c in facts["audit_cleared"][:8]
        ],
        "deferred_only_sections": [d["label"] for d in facts["audit_deferred"][:4]],
        "n_real_fixes": facts["n_real_fixes"],
        "ideas_outstanding": facts["ideas_outstanding"],
        "plans_approved": facts["plans_approved"],
    }

    try:
        prompt = (
            f"You are the Homelab Steward writing the top-of-email TL;DR for {date_str}.\n\n"
            f"END-STATE FACTS (after tonight's run — not process counters):\n"
            f"{json.dumps(llm_facts, indent=2)}\n\n"
            f"Recent session memory (optional context only):\n{session_memory}\n\n"
            "Write 2-4 short plain-English sentences about the END STATE:\n"
            "1. Lead with what still needs Carter (open audit, failed checks, health, "
            "approved plans). If nothing needs him, say the host is in good shape.\n"
            "2. Then what changed or was cleared tonight (real package bumps, sections "
            "auto-fixed). Mention a retry only if load-bearing.\n"
            "3. Skip pre-fix drift counts, badge jargon (DRIFT/ATTENTION), artifact "
            "names, and 'N audit items need attention' style process narration.\n"
            "4. No bullet lists. If truly quiet: one calm sentence.\n"
            "Return plain text only — no JSON, no markdown fences."
        )
        summary_text = _call_omp_p(prompt, model=SMALL_MODEL, timeout=90)
        # Guard: reject NDJSON / session-header bleed and empty/process junk
        summary_text = (summary_text or "").strip()
        if not summary_text:
            raise ValueError("empty summary")
        if summary_text.lstrip().startswith("{") and '"type"' in summary_text[:80]:
            raise ValueError("ndjson bleed")
        # Strip accidental fences
        if summary_text.startswith("```"):
            summary_text = re.sub(r"^```(?:\w+)?\n?", "", summary_text)
            summary_text = re.sub(r"\n?```$", "", summary_text).strip()
        if len(summary_text) < 20:
            raise ValueError("summary too short")
    except Exception:
        summary_text = _tldr_deterministic(facts)

    safe = html.escape(summary_text)
    safe = re.sub(r"\n\n+", "<br><br>", safe)
    safe = safe.replace("\n", " ")
    return safe


def _tldr_deterministic(facts):
    """End-state deterministic fallback — needs-you first, then changes/cleared."""
    parts = []

    if facts.get("health_issues"):
        parts.append("Health issues: " + "; ".join(facts["health_issues"][:3]) + ".")
    else:
        parts.append("Host healthy.")

    open_labels = [o["label"] for o in facts.get("audit_open") or []]
    failed_labels = [f["label"] for f in facts.get("audit_failed") or []]
    n_cleared = facts.get("n_sections_cleared") or 0
    if open_labels or failed_labels:
        bits = []
        if open_labels:
            bits.append("still open: " + ", ".join(open_labels[:5]))
        if failed_labels:
            bits.append("checks failed: " + ", ".join(failed_labels[:3]))
        tail = ""
        if n_cleared:
            tail = f" ({n_cleared} other section{'s' if n_cleared != 1 else ''} cleared)"
        parts.append("Audit " + "; ".join(bits) + tail + ".")
    elif n_cleared:
        parts.append(
            f"All flagged audit sections cleared ({n_cleared} auto-fixed)."
            if facts.get("n_real_fixes")
            else f"Audit clear end-of-run ({n_cleared} sections resolved)."
        )
    else:
        parts.append("Audit clear.")

    if facts.get("updates"):
        parts.append("Updates: " + "; ".join(facts["updates"][:3]) + ".")
    elif facts.get("n_failed_apply"):
        parts.append(f"{facts['n_failed_apply']} update step(s) failed.")

    if facts.get("plans_approved"):
        parts.append(f"{facts['plans_approved']} approved plan(s) waiting.")
    if facts.get("ideas_outstanding") and facts.get("n_sections_open"):
        parts.append(f"{facts['ideas_outstanding']} ideas outstanding.")

    # Quiet night compression
    if (
        facts.get("health_ok")
        and not facts.get("n_sections_open")
        and not facts.get("updates")
        and not facts.get("plans_approved")
        and not facts.get("n_sections_cleared")
    ):
        return "Quiet night — host healthy, nothing needs you."

    return " ".join(parts)



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
    fixes = read_json(run_dir / "07b-fixes.json") if (run_dir / "07b-fixes.json").exists() else {"sections": []}
    audit = read_json(run_dir / "07-audit.json") if (run_dir / "07-audit.json").exists() else {"sections": []}

    # Phase failures anywhere in the pipeline (each artifact records phase_failed)
    phase_failures = []
    for art in sorted(run_dir.glob("0*.json")):
        try:
            if read_json(art).get("phase_failed"):
                phase_failures.append(art.name)
        except Exception:
            pass

    # Build TLDR (end-state after P7b, not pre-fix process counts)
    tldr = _build_tldr(
        applied, audit, queue, fixes, heartbeat, date_str,
        session_memory=_session_memory_context(),
        validation=validation,
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
        .replace("{{HEALTH}}", _html_health(validation, heartbeat))
        .replace("{{AUDIT}}", _html_audit(audit, fixes))
        .replace("{{QUEUE}}", _html_queue(queue))
        .replace("{{USAGE}}", _html_usage(usage))
        .replace("{{FOOTER}}", footer)
    )

    email_path = run_dir / "08-email.html"
    email_path.write_text(html)
    print(f"[P8] rendered -> {email_path}")

    # Build subject
    subject = f"Homelab Steward {date_str}"

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
    lines.append(f"- Plans approved: {len(queue.get('plans', {}).get('approved', []))}")

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

    # Prune headless session dirs (sessions-automated/) older than 14 days.
    # Headless transcripts are unbounded otherwise; interactive sessions are
    # excluded here (steward's session-memory phase handles those read-only).
    cutoff = datetime.now() - timedelta(days=14)
    for d in SESSION_DIR.iterdir():
        if d.is_dir():
            try:
                mtime = datetime.fromtimestamp(d.stat().st_mtime)
                if mtime < cutoff:
                    import shutil
                    shutil.rmtree(d)
                    print(f"  pruned session: {d.name}")
            except OSError:
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
    agent_raw = _call_omp_p(agent_prompt, model=SMALL_MODEL, timeout=600, mode="json")
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
    judge_raw = _call_omp_p(judge_prompt, model=SMALL_MODEL, timeout=300, mode="json")
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


    # P0b: session memory
    try:
        if should_run("00b-session-memory.json"):
            phase_0b_session_memory(run_dir, dry_run=args.dry_run, setup=setup)
        else:
            print("[P0b] skipped (resume)")
    except Exception as e:
        print(f"[P0b] FAILED: {e}")
        write_json(run_dir / "00b-session-memory.json",
                   {"phase_failed": True, "error": str(e)})

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
            phase_5_work_queue(run_dir, dry_run=args.dry_run)
        else:
            print("[P5] skipped (resume)")
    except Exception as e:
        print(f"[P5] FAILED: {e}")
        write_json(run_dir / "05-queue.json",
                   {"phase_failed": True, "error": str(e)})

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
        if should_run("07b-fixes.json"):
            phase_7b_fix(run_dir, dry_run=args.dry_run)
        else:
            print("[P7b] skipped (resume)")
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
