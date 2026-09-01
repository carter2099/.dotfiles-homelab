"""Confirmed audit finding remediation and fix/judge loops."""
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
from .audit import (
    AUDIT_SECTIONS,
    _AUDIT_VERDICTS,
    _REAL_VERDICTS,
    _SKIP_SCANNER_FILES,
    _apply_deterministic_audit_guards,
    _audit_artifact_cacheable,
    _audit_collector_1_agents_md,
    _audit_collector_2_versions,
    _audit_collector_3_digest_quality,
    _audit_collector_4_security,
    _audit_collector_5_config_drift,
    _audit_collector_6_notes_resources,
    _audit_collector_7_agent_fleet,
    _audit_collector_8_docs_accuracy,
    _final_audit_verdict,
    _gather_repo_secrets,
    _prepare_audit_worker_packet,
    _run_audit_agent_pair,
    _session_memory_context,
    _validate_audit_judge_packet,
    _validate_prepared_audit_worker_packet,
    phase_7_audit,
)

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
            f"- For code fixes, run the focused existing tests and a syntax/build check before "
            f"committing; include the commands and results in the reported action.\n"
            f"- If scripts/digest_runner.py or its helpers change, REQUIRED validation is "
            f"`python3 -m py_compile scripts/digest_runner.py` plus "
            f"`python3 scripts/test_digest_pipeline.py`; do not commit on failure.\n"
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
            f"For each fix, verify it was applied correctly by checking actual files/state "
            f"and running focused behavioral tests for code changes. If digest_runner.py "
            f"changed, independently run its py_compile and test_digest_pipeline.py suite. "
            f"Flag any fix that was incorrect, incomplete, untested, or overreaching.\n"
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


def _confirmed_worker_findings(section):
    """Return worker findings whose stable IDs the judge confirmed."""
    confirmed_ids = {
        finding.get("id")
        for finding in section.get("judge_confirmed", [])
        if isinstance(finding, dict) and finding.get("id")
    }
    return [
        finding
        for finding in section.get("worker_findings", [])
        if isinstance(finding, dict) and finding.get("id") in confirmed_ids
    ]


def _p7b_fix_candidates(sections):
    """Split auto-fixable findings from sections that are report-only by policy."""
    to_fix = []
    report_only = []
    for section in sections:
        verdict = str(section.get("verdict", "")).removeprefix("cached-")
        confirmed = section.get("judge_confirmed", [])
        if verdict not in ("DRIFT", "ATTENTION") or not confirmed:
            continue
        name = section.get("name", "")
        if name in P7B_REPORT_ONLY_SECTIONS:
            report_only.append({
                "section": name,
                "status": "report-only",
                "findings_count": len(confirmed),
            })
            continue
        actionable = _confirmed_worker_findings(section)
        if actionable:
            to_fix.append((name, actionable))
    return to_fix, report_only


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
    to_fix, report_only = _p7b_fix_candidates(sections)

    if not to_fix:
        print("  skipped — no confirmed auto-fixable findings")
        result = {
            "sections": [],
            "report_only": report_only,
            "status": "nothing_to_fix",
        }
        write_json(run_dir / "07b-fixes.json", result)
        return result

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

    master = {"sections": fix_results, "report_only": report_only, "status": "done"}
    write_json(run_dir / "07b-fixes.json", master)

    total_fixes = sum(len(r.get("fixes_applied", [])) for r in fix_results)
    judge_oks = sum(1 for r in fix_results if r.get("judge_verdict") == "pass")
    multi = sum(1 for r in fix_results if (r.get("iteration_count") or 0) > 1)
    print(f"[P7b] done -> {run_dir / '07b-fixes.json'} "
          f"({total_fixes} fixes across {len(fix_results)} sections, "
          f"{judge_oks} judge-pass, {multi} multi-iter)")
    return master

