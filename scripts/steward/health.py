"""Validation, remediation, and heartbeat health checks."""
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
from .updates import (
    FRESHRSS_DEPLOYMENT,
    _OMP_PKG,
    _RIG_HOST_KEY_PATTERNS,
    _RIG_TIMEOUT_PATTERNS,
    _RIG_WINDOWS_PATTERNS,
    _omp_smoke_ok,
    _p1_apt_upgrade,
    _p1_auto_pkgs,
    _p1_deploy_step_ok,
    _p1_docker_assert,
    _p1_freshrss_update,
    _p1_gamingrig_maintenance,
    _p1_herdr_update,
    _p1_llama_cpp_update,
    _p1_omp_update,
    _p1_openwebui,
    _p1_searxng_update,
    _release_is_mature,
    _release_time,
    _rig_apt_upgrade,
    _rig_arm_bootnext,
    _rig_boot_id,
    _rig_command_result,
    _rig_disk_health,
    _rig_failed_units_health,
    _rig_has_failure,
    _rig_health_checks,
    _rig_herdr_update,
    _rig_model_ids_from_response,
    _rig_omp_tag,
    _rig_omp_update,
    _rig_platform_probe,
    _rig_probe_reason,
    _rig_reboot,
    _rig_reboot_required,
    _rig_ssh,
    _rig_ssh_command,
    _rig_tail,
    _rig_wait_for_health,
    _rig_wait_for_linux,
    _rig_windows_health_corroboration,
    _searxng_tag_date,
    _select_mature_llama_release,
    _select_mature_searxng_tag,
    _wait_docker_stack_ready,
    _wait_searxng_healthy,
    phase_1_apply,
)

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

    # Check for P1 mutations — any update step that actually changed state.
    # Covers apt auto-pkgs, freshrss/open-webui tag bumps, and the herdr/omp/searxng
    # self-updates so the troubleshooting fallback fires if one of them regresses.
    mutations = sum(1 for s in applied.get("steps", []) if _p1_deploy_step_ok(s))
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
        "freshrss.carter2099.com", "hooks.carter2099.com",
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
    for app_name, gemfile_lock in [
        ("blog", HOME / "blog" / "blog" / "Gemfile.lock"),
        ("delta_neutral", HOME / "dev" / "delta_neutral" / "Gemfile.lock"),
    ]:
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

