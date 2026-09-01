"""HTML report rendering, TL;DR generation, and archive handling."""
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
from .fixes import (
    _confirmed_worker_findings,
    _finding_key,
    _fix_one_section,
    _index_by_finding_key,
    _is_unfixable_note,
    _merge_fixes_applied,
    _p7b_fix_candidates,
    _parse_fix_markdown_table,
    _remaining_after_judge,
    phase_7b_fix,
)

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

def _html_gamingrig_update(step):
    """Render only actionable gaming-rig changes and failures."""
    lines = []
    host = html.escape(str(step.get("host") or RIG_SSH_ALIAS))
    status = step.get("status", "")
    if status == "skipped":
        reason = html.escape(str(step.get("reason") or "not attempted"))
        return (
            f'<p style="margin:0 0 4px; color:#e65100; font-size:13px;">'
            f'{host}: SKIPPED — {reason}</p>'
        )

    def row(text, color="#2a2a36"):
        lines.append(
            f'<p style="margin:0 0 4px; color:{color}; font-size:13px;">'
            f'{text}</p>'
        )

    for sub in step.get("substeps", []) or []:
        name = sub.get("step", "")
        sub_status = sub.get("status", "")
        if name == "apt_upgrade":
            count = sub.get("upgraded_count", 0)
            if count:
                row(f'{host} apt: {html.escape(str(count))} packages upgraded')
            if sub_status in ("failed", "error"):
                row(
                    f'{host} apt: FAILED — '
                    f'{html.escape(str(sub.get("error") or ""))}',
                    "#c62828",
                )
        elif name == "herdr_update":
            if sub_status == "ok":
                row(
                    f'{host} herdr: '
                    f'{html.escape(str(sub.get("pre_version", "?")))} -> '
                    f'{html.escape(str(sub.get("post_version", "?")))}'
                )
            elif sub_status in ("failed", "error"):
                row(
                    f'{host} herdr: FAILED — '
                    f'{html.escape(str(sub.get("error") or sub.get("reason") or ""))}',
                    "#c62828",
                )
        elif name == "omp_update":
            if sub_status == "ok":
                row(
                    f'{host} omp: '
                    f'{html.escape(str(sub.get("pre_version", "?")))} -> '
                    f'{html.escape(str(sub.get("post_version", "?")))}'
                )
            elif sub_status == "reverted":
                row(
                    f'{host} omp: update rolled back to '
                    f'{html.escape(str(sub.get("reverted_to", "?")))} '
                    '(post-update check failed)',
                    "#e65100",
                )
            elif sub_status in ("failed", "error"):
                row(
                    f'{host} omp: FAILED — '
                    f'{html.escape(str(sub.get("error") or ""))}',
                    "#c62828",
                )
        elif name in ("health", "post_reboot_health"):
            check_failures = []
            for check in sub.get("checks", []) or []:
                check_status = check.get("status", "")
                if check_status in ("failed", "error", "warning"):
                    detail = check.get("error") or check.get("reason") or check_status
                    check_failures.append(
                        f'{check.get("step", "check")}: {detail}'
                    )
            if name == "post_reboot_health" and sub_status in ("failed", "error"):
                row(
                    f'{host} post_reboot_health: FAILED — '
                    f'{html.escape(str(sub.get("error") or "health gate failed"))}',
                    "#c62828",
                )
            for failure in check_failures:
                color = "#e65100" if "warning" in failure else "#c62828"
                row(
                    f'{host} {html.escape(failure.split(":", 1)[0])}: '
                    f'{html.escape(failure.split(":", 1)[1].strip() if ":" in failure else failure)}',
                    color,
                )
            if sub_status in ("failed", "error") and not check_failures:
                row(
                    f'{host} health: FAILED — '
                    f'{html.escape(str(sub.get("error") or sub.get("reason") or sub_status))}',
                    "#c62828",
                )
        elif name == "reboot" and sub_status == "ok":
            if step.get("rebooted") and step.get("post_reboot_health_passed"):
                row(f'{host}: rebooted; Linux SSH return and post-reboot health were validated')
            elif step.get("rebooted"):
                row(f'{host}: rebooted; post-reboot health gate did not pass', "#e65100")
            elif step.get("reboot_requested"):
                row(f'{host}: reboot requested; Linux SSH return was not validated', "#e65100")
        elif name in (
            "reboot_required", "pre_reboot_boot_id", "bootnext",
            "reboot", "ssh_return",
        ) and sub_status in ("failed", "error"):
            row(
                f'{host} {html.escape(name)}: FAILED — '
                f'{html.escape(str(sub.get("error") or sub.get("reason") or sub_status))}',
                "#c62828",
            )

    if status in ("failed", "error") and not lines:
        row(
            f'{host}: FAILED — {html.escape(str(step.get("error") or ""))}',
            "#c62828",
        )
    return "\n".join(lines)

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
        # gamingrig_maintenance owns its own nested signal rendering; do not
        # collapse a no-op remote health pass into a generic "ok" row.
        if name in ("gamingrig_maintenance", "gamingrig_linux", "gamingrig"):
            rendered = _html_gamingrig_update(s)
            if rendered:
                lines.append(rendered)

        elif name == "llama_cpp":
            pre = s.get("pre_version")
            post = s.get("post_version")
            if status == "ok" and pre != post:
                lines.append(
                    f'<p style="margin:0 0 4px; color:#2a2a36; font-size:13px;">'
                    f'llama.cpp: {html.escape(str(pre or "?"))} -> '
                    f'{html.escape(str(post or "?"))}</p>'
                )
            elif status == "reverted":
                lines.append(
                    f'<p style="margin:0 0 4px; color:#e65100; font-size:13px;">'
                    f'llama.cpp: update rolled back to '
                    f'{html.escape(str(s.get("reverted_to") or "?"))}</p>'
                )
            elif status in ("failed", "error"):
                lines.append(
                    f'<p style="margin:0 0 4px; color:#c62828; font-size:13px;">'
                    f'llama.cpp: FAILED — '
                    f'{html.escape(str(s.get("error") or s.get("reason") or ""))}</p>'
                )

        # apt_upgrade: show only if upgrades happened or failed
        elif name == "apt_upgrade":

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

        # Open WebUI updates are manual; surface availability and check failures.
        elif name == "openwebui":
            if status == "available":
                lines.append(f'<p style="margin:0 0 4px; color:#e65100; font-size:13px;">'
                             f'open-webui update available: {s.get("current_tag")} -> '
                             f'{s.get("latest_tag")} — run /update-openweb-ui</p>')
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

        # omp: show only updated, reverted, or failed/error
        elif name == "omp_update":
            pre = str(s.get("pre_version", "?")).replace("omp/", "")
            post = str(s.get("post_version", "?")).replace("omp/", "")
            if status == "ok":
                lines.append(f'<p style="margin:0 0 4px; color:#2a2a36; font-size:13px;">'
                             f'omp: {pre} -> {post}</p>')
            elif status == "reverted":
                lines.append(f'<p style="margin:0 0 4px; color:#e65100; font-size:13px;">'
                             f'omp: update rolled back to {s.get("reverted_to","?")} '
                             f'(post-update check failed)</p>')
            elif status in ("failed", "error"):
                lines.append(f'<p style="margin:0 0 4px; color:#c62828; font-size:13px;">'
                             f'omp: {status} — {s.get("error",s.get("reason",""))}</p>')

        # searxng: show only updated or failed/error
        elif name == "searxng":
            if status == "ok":
                lines.append(f'<p style="margin:0 0 4px; color:#2a2a36; font-size:13px;">'
                             f'searxng: pulled latest :latest image</p>')
            elif status in ("failed", "error"):
                lines.append(f'<p style="margin:0 0 4px; color:#c62828; font-size:13px;">'
                             f'searxng: {status} — {s.get("error",s.get("reason",""))}</p>')

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
        if not acct.get("usage_fresh"):
            extra_parts.append("usage API stale")
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


def _tldr_collect_gamingrig_updates(step):
    """Flatten nested gaming-rig changes/failures for the TLDR."""
    updates = []
    n_failed = 0
    host = step.get("host") or RIG_SSH_ALIAS
    emitted_failure = False
    if step.get("status") == "skipped":
        updates.append(
            f"{host} skipped: {str(step.get('reason') or 'not attempted')[:140]}"
        )
        return updates, n_failed

    for sub in step.get("substeps", []) or []:
        name = sub.get("step", "")
        status = sub.get("status", "")
        if name == "apt_upgrade":
            count = sub.get("upgraded_count", 0)
            if count:
                updates.append(f"{host} apt: {count} packages upgraded")
            if status in ("failed", "error"):
                n_failed += 1
                emitted_failure = True
                updates.append(
                    f"{host} apt failed: {str(sub.get('error') or '')[:120]}"
                )
        elif name == "herdr_update":
            if status == "ok":
                pre = str(sub.get("pre_version") or "")
                post = str(sub.get("post_version") or "")
                if pre and post and pre != post:
                    updates.append(f"{host} herdr: {pre} -> {post}")
            elif status in ("failed", "error"):
                n_failed += 1
                emitted_failure = True
                updates.append(
                    f"{host} herdr failed: {str(sub.get('error') or '')[:120]}"
                )
        elif name == "omp_update":
            if status == "ok":
                pre = str(sub.get("pre_version") or "")
                post = str(sub.get("post_version") or "")
                if pre and post and pre != post:
                    updates.append(f"{host} omp: {pre} -> {post}")
            elif status == "reverted":
                n_failed += 1
                emitted_failure = True
                updates.append(
                    f"{host} omp update rolled back to "
                    f"{sub.get('reverted_to') or '?'} (post-update check failed)"
                )
            elif status in ("failed", "error"):
                n_failed += 1
                emitted_failure = True
                updates.append(
                    f"{host} omp failed: {str(sub.get('error') or '')[:120]}"
                )
        elif name in ("health", "post_reboot_health"):
            check_failures = []
            for check in sub.get("checks", []) or []:
                check_status = check.get("status", "")
                if check_status in ("failed", "error", "warning"):
                    if check_status in ("failed", "error"):
                        n_failed += 1
                        emitted_failure = True
                    detail = check.get("error") or check.get("reason") or check_status
                    check_failures.append(
                        f"{host} {check.get('step') or 'health'} "
                        f"{check_status}: {str(detail)[:120]}"
                    )
            if name == "post_reboot_health" and status in ("failed", "error"):
                if check_failures:
                    updates.append(
                        f"{host} post_reboot_health failed: "
                        f"{str(sub.get('error') or 'health gate failed')[:120]}"
                    )
                else:
                    n_failed += 1
                    emitted_failure = True
                    updates.append(
                        f"{host} post_reboot_health failed: "
                        f"{str(sub.get('error') or '')[:120]}"
                    )
            updates.extend(check_failures)
            if (
                status in ("failed", "error")
                and not (sub.get("checks") or [])
                and name != "post_reboot_health"
            ):
                n_failed += 1
                emitted_failure = True
                updates.append(
                    f"{host} health failed: {str(sub.get('error') or '')[:120]}"
                )
        elif name in (
            "reboot_required", "pre_reboot_boot_id", "bootnext",
            "reboot", "ssh_return",
        ) and status in ("failed", "error"):
            n_failed += 1
            updates.append(
                f"{host} {name} failed: "
                f"{str(sub.get('error') or sub.get('reason') or status)[:120]}"
            )
        elif name == "reboot" and status == "ok":
            if step.get("rebooted") and step.get("post_reboot_health_passed"):
                updates.append(f"{host} rebooted and Linux health was rechecked")
            elif step.get("rebooted"):
                updates.append(f"{host} rebooted; post-reboot health gate did not pass")
            elif step.get("reboot_requested"):
                updates.append(
                    f"{host} reboot requested; Linux SSH return was not validated"
                )

    if step.get("status") in ("failed", "error") and not emitted_failure:
        n_failed += 1
        updates.append(
            f"{host} maintenance failed: {str(step.get('error') or '')[:120]}"
        )
    return updates, n_failed


def _tldr_collect_gamingrig_failures(step):
    """Return remote apply failures for TLDR health and Carter's action list."""
    host = step.get("host") or RIG_SSH_ALIAS
    failures = []
    for sub in step.get("substeps", []) or []:
        name = sub.get("step") or "maintenance"
        status = sub.get("status", "")
        if name in ("health", "post_reboot_health"):
            checks = sub.get("checks") or []
            check_failures = [
                check for check in checks
                if check.get("status") in ("failed", "error")
            ]
            for check in check_failures:
                detail = check.get("error") or check.get("reason") or check.get("status")
                failures.append(
                    f"{host} {name}/{check.get('step') or 'check'}: {str(detail)[:180]}"
                )
            if status in ("failed", "error") and not check_failures:
                failures.append(
                    f"{host} {name}: "
                    f"{str(sub.get('error') or 'health gate failed')[:180]}"
                )
        elif status in ("failed", "error", "reverted"):
            failures.append(
                f"{host} {name}: "
                f"{str(sub.get('error') or sub.get('reason') or status)[:180]}"
            )
    if step.get("status") in ("failed", "error") and not failures:
        failures.append(
            f"{host} maintenance: "
            f"{str(step.get('error') or step.get('reason') or 'failed')[:180]}"
        )
    return failures


def _tldr_collect_updates(applied):
    """Real local changes plus actionable remote-maintenance signals."""
    updates = []
    n_failed = 0
    for s in applied.get("steps", []) or []:
        step = s.get("step", "")
        if step in ("gamingrig_maintenance", "gamingrig_linux", "gamingrig"):
            rig_updates, rig_failed = _tldr_collect_gamingrig_updates(s)
            updates.extend(rig_updates)
            n_failed += rig_failed
            continue

        status = s.get("status", "")
        if step == "llama_cpp":
            pre = s.get("pre_version")
            post = s.get("post_version")
            if status == "ok" and pre != post:
                updates.append(f"llama.cpp: {pre or '?'} -> {post or '?'}")
            elif status == "reverted":
                n_failed += 1
                updates.append(
                    f"llama.cpp update rolled back to "
                    f"{s.get('reverted_to') or '?'}"
                )
            elif status in ("failed", "error"):
                n_failed += 1
                updates.append(
                    f"llama.cpp failed: {str(s.get('error') or s.get('reason') or '')[:120]}"
                )
            continue

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
        elif step == "openwebui" and status == "available":
            updates.append(
                f"open-webui update available: {s.get('current_tag')} -> "
                f"{s.get('latest_tag')} (manual: /update-openweb-ui)"
            )
        elif step == "freshrss" and status == "bumped":
            updates.append(f"freshrss: {s.get('current_tag')} -> {s.get('latest_tag')}")
        elif step == "herdr_update" and status == "ok":
            pre = str(s.get("pre_version", "")).replace("herdr ", "")
            post = str(s.get("post_version", "")).replace("herdr ", "")
            if pre and post and pre != post:
                updates.append(f"herdr: {pre} -> {post}")
        elif step == "omp_update" and status == "ok":
            pre = str(s.get("pre_version", "")).replace("omp/", "")
            post = str(s.get("post_version", "")).replace("omp/", "")
            if pre and post and pre != post:
                updates.append(f"omp: {pre} -> {post}")
        elif step == "omp_update" and status == "reverted":
            updates.append(f"omp update rolled back to {s.get('reverted_to','?')} "
                           f"(post-update check failed)")
        elif step == "searxng" and status == "ok":
            updates.append("searxng: pulled latest image")
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
    """End-state facts for TLDR (LLM + deterministic)."""
    updates, n_failed_apply = _tldr_collect_updates(applied)
    rig_apply_failures = []
    for step in applied.get("steps", []) or []:
        if step.get("step") in ("gamingrig_maintenance", "gamingrig_linux", "gamingrig"):
            rig_apply_failures.extend(_tldr_collect_gamingrig_failures(step))
    health_issues = (
        rig_apply_failures + _tldr_collect_health(heartbeat, validation)
    )
    audit_state = _tldr_audit_end_state(audit, fixes)

    n_real_fixes = 0
    for s in (fixes or {}).get("sections", []) or []:
        n_real_fixes += sum(
            1 for f in _real_fixes(s.get("fixes_applied") or [])
            if f.get("status") == "fixed"
        )

    plans = (queue or {}).get("plans", {}) or {}
    needs_carter = [
        f"gaming-rig apply failure — {failure}"
        for failure in rig_apply_failures
    ]
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
        "rig_apply_failures": rig_apply_failures,
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
        rig_failures = facts.get("rig_apply_failures") or []
        other_issues = facts["health_issues"][len(rig_failures):]
        issue_text = rig_failures + other_issues[:3]
        parts.append("Health issues: " + "; ".join(issue_text) + ".")
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
        atomic_write_text(TEMPLATE_PATH, DEFAULT_TEMPLATE)
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
    atomic_write_text(email_path, html)
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
        except Exception as error:
            # A rendered report is not a successful phase if delivery failed.
            # Let the workflow record a failed, retryable attempt.
            raise RuntimeError(f"email send failed: {error}") from error
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
        "## Update Status",
    ]
    for s in applied.get("steps", []):
        if s.get("dry_run"):
            lines.append("- Dry run — no mutations")
            break
        name = s.get("step", "")
        status = s.get("status", "")
        if name in ("gamingrig_maintenance", "gamingrig_linux", "gamingrig"):
            for update in _tldr_collect_gamingrig_updates(s)[0]:
                lines.append(f"- {update}")
            continue

        if name == "llama_cpp":
            for update in _tldr_collect_updates({"steps": [s]})[0]:
                lines.append(f"- {update}")
            continue

        if name.startswith("auto_"):
            pkg = name.replace("auto_", "")
            if status == "ok":
                lines.append(f"- {pkg}: {s.get('pre_version')} -> {s.get('post_version')}")
            elif status == "skipped":
                lines.append(f"- {pkg}: already current ({s.get('pre_version')})")
            else:
                lines.append(f"- {pkg}: FAILED")
        elif name == "openwebui":
            if status == "available":
                lines.append(
                    f"- open-webui: {s.get('current_tag')} -> {s.get('latest_tag')} "
                    "(available; run `/update-openweb-ui`)"
                )
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
    atomic_write_text(run_dir / "summary.md", md_content)

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

