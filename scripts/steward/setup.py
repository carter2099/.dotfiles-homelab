"""Setup and interactive-session memory maintenance."""
from __future__ import annotations

from .config import (
    AUTO_PKGS,
    DEFAULT_TEMPLATE,
    DEPENDABOT_UNIT,
    DIGEST_SCRIPT,
    ENDPOINTS,
    FIX_MAX_ITERS,
    GH_API,
    HOME,
    IDEAS_DIR,
    K3S,
    LLAMA_CPP_RELEASES_API,
    LLAMA_CPP_UPDATE_SCRIPT,
    MAX_WORKERS,
    OMP_JSON_MODEL,
    OMP_JSON_TIMEOUT,
    OPENWEBUI_COMPOSE,
    P7B_REPORT_ONLY_SECTIONS,
    PENDING_PATH,
    PLANS_DIR,
    PROXY_HEALTH,
    Path,
    RIG_APT_TIMEOUT,
    RIG_BOOT_ENTRY,
    RIG_DISK_MAX_PERCENT,
    RIG_MODEL_ENDPOINT,
    RIG_REBOOT_WAIT_TIMEOUT,
    RIG_REMOTE_PATH,
    RIG_REQUIRED_MODEL_IDS,
    RIG_SSH_ALIAS,
    RIG_SSH_COMMAND_TIMEOUT,
    RIG_SSH_CONNECT_TIMEOUT,
    RIG_UPDATE_TIMEOUT,
    RUNS_LOG,
    RUN_DIR_BASE,
    SEARXNG_TAGS_API,
    SECRET_PATTERNS,
    SESSION_ACTIVE_MINUTES,
    SESSION_DIR,
    SESSION_INTERACTIVE_DIR,
    SESSION_MEMOIR_DIR,
    SESSION_MEMORY_CONTEXT_DAYS,
    SESSION_MEMORY_CONTEXT_MAX,
    SMALL_MODEL,
    STEWARD_MODEL,
    STEWARD_PATH,
    TEMPLATE_PATH,
    ThreadPoolExecutor,
    UPDATE_MIN_AGE_DAYS,
    WORKFLOW_POLICY_VERSION,
    WORKFLOW_SCHEMA_VERSION,
    _FNM_NODE_DIRS,
    argparse,
    as_completed,
    datetime,
    hashlib,
    html,
    json,
    os,
    re,
    shlex,
    subprocess,
    sys,
    time,
    timedelta,
    timezone,
    urllib,
)
from .runtime import (
    atomic_write_text,
    _assistant_text_from_message,
    _balanced_json_slice,
    _call_omp_p,
    _call_omp_p_json,
    _date_context,
    _evidence_hash,
    _extract_json,
    _load_prev_artifact,
    _message_error_str,
    _ndjson_looks_like_event_stream,
    _reboot_if_needed,
    apt_installed_version,
    apt_upgradable,
    extract_from_ndjson,
    parse_previous_summary,
    prev_workday,
    read_json,
    run,
    run_capture,
    run_capture_ok,
    run_ok,
    user_env,
    write_json,
)

def phase_0_setup(args, run_dir=None):
    """Create run dir, snapshot usage, stop dependabot, load prev-summary delta."""
    current = datetime.now()
    if run_dir is None:
        run_dir = RUN_DIR_BASE / current.strftime("%Y-%m-%d")
    else:
        run_dir = Path(run_dir)
    date_str = run_dir.name
    run_date = datetime.strptime(date_str, "%Y-%m-%d")
    run_dir.mkdir(parents=True, exist_ok=True)

    prev_date = prev_workday(run_date)
    prev_date_str = prev_date.strftime("%Y-%m-%d")
    prev_md = RUN_DIR_BASE / prev_date_str / "summary.md"
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
                "usage_fresh": bool(acct.get("usage_fresh")),
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
        extra = "" if a.get("usage_fresh") else ", usage API STALE"
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
    atomic_write_text(path, fm + h1 + "\n\n" + body.strip() + "\n")


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

