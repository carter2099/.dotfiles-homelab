"""Local and gaming-rig update operations."""
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
    """Report newer stable Open WebUI releases without mutating production."""
    print("  [1f] open-webui stable release check (report-only)")
    if not OPENWEBUI_COMPOSE.exists():
        return {"step": "openwebui", "status": "skipped",
                "reason": f"compose file not found: {OPENWEBUI_COMPOSE}"}

    compose_text = OPENWEBUI_COMPOSE.read_text()
    current_m = re.search(r"ghcr\.io/open-webui/open-webui:([^\s\"']+)", compose_text)
    current_tag = current_m.group(1) if current_m else None
    if not current_tag:
        return {"step": "openwebui", "status": "skipped",
                "reason": "could not parse current tag from compose file"}

    try:
        req = urllib.request.Request(GH_API, headers={"Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            release = json.loads(resp.read().decode())
    except Exception as e:
        return {"step": "openwebui", "status": "error",
                "reason": f"GitHub API unreachable: {e}", "current_tag": current_tag}

    latest_tag = release.get("tag_name", "").lstrip("v")
    if not latest_tag:
        return {"step": "openwebui", "status": "error",
                "reason": "no tag_name in GitHub release", "current_tag": current_tag}
    if release.get("draft") or release.get("prerelease"):
        return {"step": "openwebui", "status": "error",
                "reason": f"latest release is not stable: {latest_tag}",
                "current_tag": current_tag}

    current_m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", current_tag)
    latest_m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", latest_tag)
    if not current_m or not latest_m:
        return {"step": "openwebui", "status": "error",
                "reason": "current or latest tag is not numeric semver",
                "current_tag": current_tag, "latest_tag": latest_tag}

    current_version = tuple(int(part) for part in current_m.groups())
    latest_version = tuple(int(part) for part in latest_m.groups())
    common = {
        "step": "openwebui",
        "current_tag": current_tag,
        "latest_tag": latest_tag,
        "release_url": release.get("html_url", ""),
        "published_at": release.get("published_at", ""),
        "local_mutation": False,
    }
    if latest_version <= current_version:
        return {**common, "status": "current"}

    print(f"    update available: {current_tag} -> {latest_tag} "
          "(manual: /update-openweb-ui)")
    return {
        **common,
        "status": "available",
        "reason": "manual guarded update required: /update-openweb-ui",
    }


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


def _p1_deploy_step_ok(step):
    """True when a P1 step actually mutated state."""
    if step.get("local_mutation") is False:
        return False
    name = step.get("step", "")
    status = step.get("status", "")
    if name.startswith("auto_") and status == "ok":
        return True
    if name == "freshrss" and status == "bumped":
        return True
    if name in (
        "herdr_update", "omp_update", "searxng",
    ) and status == "ok":
        return True
    return False


_P1_RETRYABLE_STATUSES = frozenset({"failed", "error", "timeout"})
_P1_DEGRADED_STATUSES = frozenset({"reverted", "warning", "degraded"})


def _p1_status_packets(value, path=""):
    """Yield bounded status packets, including nested remote substeps."""
    if isinstance(value, dict):
        status = str(value.get("status") or "").strip().lower()
        if status in _P1_RETRYABLE_STATUSES | _P1_DEGRADED_STATUSES:
            yield path, value
        for key in ("steps", "substeps", "checks"):
            children = value.get(key)
            if isinstance(children, list):
                for index, child in enumerate(children):
                    child_path = f"{path}.{key}[{index}]" if path else f"{key}[{index}]"
                    yield from _p1_status_packets(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            child_path = f"{path}[{index}]" if path else f"[{index}]"
            yield from _p1_status_packets(child, child_path)


def _p1_packet_detail(path, packet):
    detail = packet.get("error") or packet.get("reason") or packet.get("step")
    if not detail:
        detail = packet.get("status") or "unknown failure"
    label = f"{path}: " if path else ""
    return f"{label}{str(detail)[:400]}"


def _finish_p1(run_dir, data):
    """Persist P1's durable outcome while retaining every step packet."""
    packets = list(_p1_status_packets(data.get("steps", [])))
    retryable = [
        _p1_packet_detail(path, packet)
        for path, packet in packets
        if str(packet.get("status") or "").lower() in _P1_RETRYABLE_STATUSES
    ]
    degraded = [
        _p1_packet_detail(path, packet)
        for path, packet in packets
        if str(packet.get("status") or "").lower() in _P1_DEGRADED_STATUSES
    ]
    if retryable:
        data.update({
            "phase_status": "failed",
            "phase_failed": True,
            "reason": "retryable P1 step failure: " + "; ".join(retryable[:8]),
        })
    elif degraded:
        data.update({
            "phase_status": "degraded",
            "reason": "P1 completed with degraded step(s): " + "; ".join(degraded[:8]),
        })
    else:
        data.setdefault("phase_status", "succeeded")
    write_json(run_dir / "01-applied.json", data)
    return data


def _release_time(value):
    """Parse an upstream UTC timestamp into an aware datetime."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _release_is_mature(value, now=None):
    """True once an upstream release has existed for the safety window."""
    now = now or datetime.now(timezone.utc)
    return now - _release_time(value) >= timedelta(days=UPDATE_MIN_AGE_DAYS)


def _searxng_tag_date(tag):
    match = re.fullmatch(r"(\d{4})\.(\d{1,2})\.(\d{1,2})-[0-9a-f]+", tag or "")
    return tuple(int(part) for part in match.groups()) if match else None


def _select_mature_searxng_tag(tags, current_tag, now=None):
    """Newest immutable SearXNG tag old enough to deploy, never a downgrade."""
    current_item = next((item for item in tags if item.get("name") == current_tag), None)
    current_time = _release_time(current_item["last_updated"]) if current_item else None
    current_date = _searxng_tag_date(current_tag)
    candidates = []
    for item in tags:
        name = item.get("name", "")
        published = item.get("last_updated", "")
        digest = item.get("digest", "")
        tag_date = _searxng_tag_date(name)
        if not tag_date or not digest or not published:
            continue
        try:
            published_time = _release_time(published)
        except ValueError:
            continue
        if not _release_is_mature(published, now=now):
            continue
        if current_time and published_time <= current_time:
            continue
        if not current_time and current_date and tag_date <= current_date:
            continue
        candidates.append((published_time, item))
    return max(candidates, key=lambda pair: pair[0])[1] if candidates else None


def _select_mature_llama_release(releases, current_tag, now=None):
    """Newest non-draft llama.cpp release old enough to deploy."""
    match = re.fullmatch(r"b(\d+)", current_tag or "")
    if not match:
        return None
    current_build = int(match.group(1))
    candidates = []
    for release in releases:
        tag = release.get("tag_name", "")
        published = release.get("published_at", "")
        tag_match = re.fullmatch(r"b(\d+)", tag)
        if (release.get("draft") or release.get("prerelease") or not tag_match
                or not published):
            continue
        try:
            mature = _release_is_mature(published, now=now)
        except ValueError:
            continue
        build = int(tag_match.group(1))
        if mature and build > current_build:
            candidates.append((build, release))
    return max(candidates, key=lambda pair: pair[0])[1] if candidates else None


def _wait_searxng_healthy(timeout_s=45):
    """Require SearXNG's JSON search API, not merely an open TCP port."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            req = urllib.request.Request(ENDPOINTS["searxng"])
            with urllib.request.urlopen(req, timeout=8) as response:
                payload = json.loads(response.read().decode())
            if isinstance(payload, dict) and isinstance(payload.get("results"), list):
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def _p1_searxng_update(tags=None, now=None):
    """Advance the immutable image pin after seven days; roll back on failure."""
    print("  [1h] searxng mature-image update")
    compose = HOME / "searxng" / "docker-compose.yml"
    if not compose.exists():
        return {"step": "searxng", "status": "skipped",
                "reason": f"compose file not found: {compose}"}

    compose_text = compose.read_text()
    image_match = re.search(
        r"docker\.io/searxng/searxng@sha256:[0-9a-f]{64}", compose_text)
    if not image_match:
        return {"step": "searxng", "status": "failed",
                "reason": "could not parse immutable image pin"}
    old_ref = image_match.group(0)
    current_tag = run_capture([
        "docker", "inspect", "searxng", "--format",
        '{{index .Config.Labels "org.opencontainers.image.version"}}',
    ], timeout=30)
    if not _searxng_tag_date(current_tag):
        return {"step": "searxng", "status": "failed", "image_ref": old_ref,
                "reason": f"could not parse running version label: {current_tag!r}"}

    if tags is None:
        try:
            req = urllib.request.Request(
                SEARXNG_TAGS_API, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as response:
                tags = json.loads(response.read().decode()).get("results", [])
        except Exception as exc:
            return {"step": "searxng", "status": "error",
                    "current_tag": current_tag,
                    "reason": f"Docker Hub unreachable: {exc}"}

    target = _select_mature_searxng_tag(tags, current_tag, now=now)
    if not target:
        return {"step": "searxng", "status": "skipped",
                "current_tag": current_tag,
                "reason": f"no newer release is {UPDATE_MIN_AGE_DAYS} days old"}

    target_tag = target["name"]
    target_ref = f"docker.io/searxng/searxng@{target['digest']}"
    if target_ref == old_ref:
        return {"step": "searxng", "status": "skipped",
                "current_tag": current_tag, "target_tag": target_tag,
                "reason": "eligible release already pinned"}

    new_text = compose_text.replace(old_ref, target_ref, 1)
    print(f"    bumping searxng: {current_tag} -> {target_tag}")
    try:
        run(["docker", "pull", target_ref], capture_output=True, text=True, timeout=300)
        compose.write_text(new_text)
        run(["docker", "compose", "-f", str(compose), "up", "-d"],
            cwd=compose.parent, capture_output=True, text=True, timeout=180)
        if not _wait_searxng_healthy():
            raise RuntimeError("JSON search health check timed out")
        return {"step": "searxng", "status": "ok",
                "pre_version": current_tag, "post_version": target_tag,
                "pre_image": old_ref, "post_image": target_ref,
                "release_age_days": (
                    (now or datetime.now(timezone.utc))
                    - _release_time(target["last_updated"])
                ).days}
    except Exception as exc:
        compose.write_text(compose_text)
        rollback_error = ""
        try:
            run(["docker", "compose", "-f", str(compose), "up", "-d"],
                cwd=compose.parent, capture_output=True, text=True, timeout=180)
            if not _wait_searxng_healthy():
                raise RuntimeError("restored pin failed JSON search health check")
        except Exception as rollback_exc:
            rollback_error = str(rollback_exc)
        return {"step": "searxng",
                "status": "failed" if rollback_error else "reverted",
                "pre_version": current_tag, "target_version": target_tag,
                "reverted_to": old_ref, "error": str(exc),
                "rollback_error": rollback_error}

def _p1_llama_cpp_update(releases=None, now=None):
    """Build a seven-day-old llama.cpp release on Linux and atomically deploy it."""
    print("  [1i] llama.cpp mature-release update")
    try:
        current_path = run_capture(
            _rig_ssh_command(["readlink", "-f", "/usr/local/bin/llama-server"]),
            timeout=20,
        )
    except Exception as exc:
        return {
            "step": "llama_cpp",
            "status": "skipped",
            "reason": "gaming rig Linux unavailable or SSH probe failed",
            "error": str(exc)[:300],
        }
    current_match = re.search(r"/opt/llama\.cpp/(b\d+)/bin/llama-server$", current_path)
    if not current_match:
        return {"step": "llama_cpp", "status": "skipped",
                "reason": "gaming rig Linux unavailable or current build path unreadable",
                "current_path": current_path}
    current_tag = current_match.group(1)

    if releases is None:
        try:
            req = urllib.request.Request(
                LLAMA_CPP_RELEASES_API,
                headers={"Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=20) as response:
                releases = json.loads(response.read().decode())
        except Exception as exc:
            return {"step": "llama_cpp", "status": "error",
                    "current_tag": current_tag,
                    "reason": f"GitHub API unreachable: {exc}"}

    target = _select_mature_llama_release(releases, current_tag, now=now)
    if not target:
        return {"step": "llama_cpp", "status": "skipped",
                "current_tag": current_tag,
                "reason": f"no newer release is {UPDATE_MIN_AGE_DAYS} days old"}
    target_tag = target["tag_name"]
    if not LLAMA_CPP_UPDATE_SCRIPT.exists():
        return {"step": "llama_cpp", "status": "failed",
                "current_tag": current_tag, "target_tag": target_tag,
                "reason": f"update helper missing: {LLAMA_CPP_UPDATE_SCRIPT}"}

    stdout, stderr, code = run_capture_ok(
        _rig_ssh_command(["bash", "-s", "--", target_tag]),
        input=LLAMA_CPP_UPDATE_SCRIPT.read_text(),
        timeout=1800,
    )
    detail = f"{stdout}\n{stderr}".strip()
    common = {
        "step": "llama_cpp",
        "pre_version": current_tag,
        "target_version": target_tag,
        "release_age_days": (
            (now or datetime.now(timezone.utc))
            - _release_time(target["published_at"])
        ).days,
        "output_tail": detail[-1000:],
    }
    if code == 0 and f"UPDATE_OK {target_tag}" in stdout:
        return {**common, "status": "ok", "post_version": target_tag}
    if "ROLLBACK_OK" in detail:
        return {**common, "status": "reverted", "reverted_to": current_tag,
                "error": f"deployment failed with exit {code}"}
    return {**common, "status": "failed",
            "error": f"deployment or rollback failed with exit {code}"}


_OMP_PKG = "@oh-my-pi/pi-coding-agent"   # bun global package backing ~/.bun/bin/omp


def _omp_smoke_ok(env):
    """Cheap deterministic binary check: `omp -p` with empty input.

    An empty headless prompt has nothing to run, so it exits 0 in ~1s without
    any LLM call — it only fails on a broken/partial install, not on API
    outages (which must not trigger a revert).
    """
    o, _, c = run_capture_ok(["omp", "-p"], env=env, timeout=60, input="")
    return c == 0


def _p1_omp_update():
    """Self-update the omp CLI; verify the binary still works; revert on breakage.

    omp is the steward's own engine (P5/P7/P7b/P9b spawn `omp -p`), so a bad
    `omp update` is caught here deterministically — snapshot version -> upgrade
    -> `omp -p` smoke check -> if broken, `bun add -g` back to the snapshot —
    rather than surfacing later as failed agent phases with no working fallback.
    """
    print("  [1i] omp update")
    env = user_env()
    pre_ver = run_capture(["omp", "--version"], env=env, timeout=30)
    pre_tag = pre_ver.replace("omp/", "").strip() if pre_ver else ""
    if not pre_tag:
        return {"step": "omp_update", "status": "failed",
                "pre_version": pre_ver, "post_version": pre_ver,
                "error": "could not snapshot pre-update version"}

    stdout, stderr, code = run_capture_ok(["omp", "update"], env=env, timeout=600)
    out = f"{stdout}\n{stderr}".strip()
    post_ver = run_capture(["omp", "--version"], env=env, timeout=30)
    works = _omp_smoke_ok(env)

    if works and post_ver and post_ver != pre_ver:
        return {"step": "omp_update", "status": "ok",
                "pre_version": pre_ver, "post_version": post_ver,
                "output_tail": out[-500:]}
    if works and code == 0:
        return {"step": "omp_update", "status": "skipped",
                "pre_version": pre_ver, "post_version": post_ver,
                "reason": "already current", "output_tail": out[-500:]}

    # install failed or the new binary is broken — revert to the snapshot
    msg = f"post-update check failed; reverting to {pre_tag}" if not works \
        else (out[-500:] or f"exit {code}")
    r_stdout, r_stderr, r_code = run_capture_ok(
        ["bun", "add", "-g", f"{_OMP_PKG}@{pre_tag}"], env=env, timeout=300)
    r_out = f"{r_stdout}\n{r_stderr}".strip()
    rev_ver = run_capture(["omp", "--version"], env=env, timeout=30)
    reverted = bool(rev_ver) and rev_ver == pre_ver and _omp_smoke_ok(env)
    return {"step": "omp_update",
            "status": "reverted" if reverted else "failed",
            "pre_version": pre_ver, "post_version": post_ver,
            "reverted_to": rev_ver, "error": msg,
            "revert_detail": r_out[-500:]}


def _rig_ssh_command(remote_args):
    """Build an injection-safe SSH argv for the pinned Linux rig alias.

    The host key is intentionally checked by the user's SSH configuration.
    ``BatchMode`` makes unknown/mismatched keys fail closed instead of asking
    the unattended timer to accept one.  Every command also has both an SSH
    connection bound and a subprocess timeout.
    """
    if isinstance(remote_args, (str, bytes)):
        raise TypeError("remote_args must be a sequence, not a command string")
    return [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={max(1, int(RIG_SSH_CONNECT_TIMEOUT))}",
        "-o", "ConnectionAttempts=1",
        "-o", "ServerAliveInterval=5",
        "-o", "ServerAliveCountMax=2",
        "-o", "StrictHostKeyChecking=yes",
        RIG_SSH_ALIAS,
        "env", f"PATH={RIG_REMOTE_PATH}",
        *[shlex.quote(str(arg)) for arg in remote_args],
    ]


def _rig_ssh(remote_args, timeout=RIG_SSH_COMMAND_TIMEOUT, input_data=None):
    """Run one bounded command through the pinned rig SSH alias."""
    kwargs = {"timeout": max(1, int(timeout))}
    if input_data is not None:
        kwargs["input"] = input_data
    return run_capture_ok(_rig_ssh_command(remote_args), **kwargs)


def _rig_tail(stdout="", stderr="", limit=700):
    """Keep remote command evidence bounded in JSON/email artifacts."""
    detail = "\n".join(part for part in (stdout or "", stderr or "") if part)
    return detail[-limit:]


def _rig_command_result(step, remote_args, timeout=RIG_SSH_COMMAND_TIMEOUT,
                        input_data=None, capture_full=False):
    """Return the normal P1 result shape for one remote command.

    ``capture_full`` is an internal escape hatch for parsers that must inspect
    the complete command response (for example JSON or apt's final count).
    The full fields are transient; callers must consume and remove them before
    storing the result in an artifact.
    """
    stdout, stderr, code = _rig_ssh(
        remote_args, timeout=timeout, input_data=input_data)
    result = {
        "step": step,
        "status": "ok" if code == 0 else "failed",
        "exit_code": code,
    }
    if stdout:
        result["stdout_tail"] = stdout[-700:]
    if stderr:
        result["stderr_tail"] = stderr[-700:]
    if capture_full:
        result["_full_stdout"] = stdout or ""
        result["_full_stderr"] = stderr or ""
    if code != 0:
        result["error"] = _rig_tail(stdout, stderr) or f"exit {code}"
    return result


_RIG_HOST_KEY_PATTERNS = (
    "host key verification failed",
    "remote host identification has changed",
    "offending ed25519 key",
    "offending ecdsa key",
    "no ed25519 host key is known",
    "no ecdsa host key is known",
    "host key is not known",
    "man-in-the-middle",
)
_RIG_WINDOWS_PATTERNS = (
    "windows",
    "windows_nt",
    "microsoft",
    "mingw",
    "msys",
    "cygwin",
    "not recognized as an internal or external command",
    "operable program or batch file",
    "the term 'uname' is not recognized",
    "uname : the term",
)
_RIG_TIMEOUT_PATTERNS = (
    "connection timed out",
    "operation timed out",
    "connect_timeout",
    "timed out",
    "no route to host",
    "connection refused",
    "network is unreachable",
)


def _rig_probe_reason(stdout, stderr, code):
    """Classify a failed Linux probe without attempting wake/OS switching."""
    detail = _rig_tail(stdout, stderr, limit=280).replace("\n", " ").strip()
    lower = detail.lower()
    stdout_lower = (stdout or "").lower()
    stderr_lower = (stderr or "").lower()
    if any(pattern in lower for pattern in _RIG_HOST_KEY_PATTERNS):
        return "host key mismatch: SSH refused the pinned gamingrig-linux key"
    if any(pattern in stdout_lower for pattern in _RIG_WINDOWS_PATTERNS):
        return "Windows host: Linux SSH probe command is unavailable"
    if any(
        pattern in stderr_lower
        for pattern in _RIG_WINDOWS_PATTERNS
        if pattern != "windows"
    ):
        return "Windows host: Linux SSH probe command is unavailable"
    if any(pattern in lower for pattern in _RIG_TIMEOUT_PATTERNS):
        if "refused" in lower:
            return "offline or sleeping: SSH connection refused"
        if "no route" in lower or "unreachable" in lower:
            return "offline or sleeping: no route to gamingrig-linux"
        return "offline or sleeping: SSH connection timed out"
    if detail:
        return f"remote platform unavailable: {detail[:220]}"
    return f"remote platform probe failed (exit {code})"


def _rig_windows_health_corroboration():
    """Confirm a Windows probe through the trusted local llm-proxy health API."""
    endpoint = ENDPOINTS["llm-proxy"]
    try:
        request = urllib.request.Request(endpoint)
        with urllib.request.urlopen(request, timeout=8) as response:
            payload = json.loads(response.read().decode())
    except Exception as exc:
        return {
            "status": "failed",
            "endpoint": endpoint,
            "error": f"trusted llm-proxy health unavailable: {exc}",
        }
    if not isinstance(payload, dict):
        return {
            "status": "failed",
            "endpoint": endpoint,
            "error": "trusted llm-proxy health returned a non-object JSON value",
        }
    rig_os = str(payload.get("rig_os") or "").strip().lower()
    if rig_os != "windows":
        return {
            "status": "failed",
            "endpoint": endpoint,
            "rig_os": rig_os or "missing",
            "error": "trusted llm-proxy health did not report rig_os=windows",
        }
    return {
        "status": "ok",
        "endpoint": endpoint,
        "rig_os": "windows",
    }


def _rig_platform_probe():
    """Probe only; no wake-on-LAN, firmware, or Windows switching is allowed."""
    stdout, stderr, code = _rig_ssh(
        ["uname", "-s"], timeout=RIG_SSH_CONNECT_TIMEOUT)
    platform = (stdout or "").strip()
    if code == 0 and platform.lower() == "linux":
        return {
            "step": "platform_probe",
            "status": "ok",
            "os": "Linux",
            "host": RIG_SSH_ALIAS,
        }, True

    reason = _rig_probe_reason(stdout, stderr, code)
    base = {
        "step": "platform_probe",
        "host": RIG_SSH_ALIAS,
        "reason": reason,
        "exit_code": code,
        "output_tail": _rig_tail(stdout, stderr, limit=500),
    }
    if reason.startswith("offline or sleeping:"):
        base["status"] = "skipped"
        return base, False
    if (
        reason.startswith("Windows host:")
        or reason.startswith("host key mismatch:")
    ):
        corroboration = _rig_windows_health_corroboration()
        base["windows_corroboration"] = corroboration
        if corroboration["status"] == "ok":
            base["status"] = "skipped"
            base["os"] = "Windows"
            base["reason"] = (
                "Windows host: trusted llm-proxy health corroborated "
                "rig_os=windows"
            )
        else:
            base["status"] = "failed"
            if reason.startswith("Windows host:"):
                base["error"] = (
                    "Windows probe was not corroborated by trusted llm-proxy "
                    f"health: {corroboration.get('error', 'unknown error')}"
                )
            else:
                base["error"] = (
                    f"{reason}; trusted llm-proxy did not corroborate Windows: "
                    f"{corroboration.get('error', 'unknown error')}"
                )
        return base, False
    base["status"] = "failed"
    base["error"] = reason
    return base, False


def _rig_apt_upgrade():
    """Run unattended apt update/upgrade and report planned and applied counts."""
    update = _rig_command_result(
        "apt_update",
        [
            "sudo", "-n", "env", "DEBIAN_FRONTEND=noninteractive",
            "apt-get", "update",
        ],
        timeout=RIG_APT_TIMEOUT,
    )
    if update["status"] != "ok":
        return {
            "step": "apt_upgrade",
            "status": "failed",
            "planned_count": 0,
            "upgraded_count": 0,
            "substeps": [update],
            "output_tail": _rig_tail(
                update.get("stdout_tail", ""), update.get("stderr_tail"),
                limit=1400,
            ),
            "error": update.get("error", "apt update failed"),
        }

    plan = _rig_command_result(
        "apt_upgrade_plan", ["apt-get", "--simulate", "upgrade"],
        timeout=RIG_APT_TIMEOUT,
    )
    plan_output = _rig_tail(
        plan.get("stdout_tail", ""), plan.get("stderr_tail"), limit=1400)
    match = re.search(r"(?im)(\d+)\s+upgraded\b", plan_output)
    planned = int(match.group(1)) if match else 0
    if plan["status"] != "ok" or match is None:
        plan["status"] = "failed"
        plan["error"] = plan.get("error") or (
            "apt simulation did not report an upgrade count")
        return {
            "step": "apt_upgrade",
            "status": "failed",
            "planned_count": planned,
            "upgraded_count": 0,
            "substeps": [update, plan],
            "output_tail": plan_output,
            "error": plan["error"],
        }

    upgrade = _rig_command_result(
        "apt_upgrade_apply",
        [
            "sudo", "-n", "env", "DEBIAN_FRONTEND=noninteractive",
            "apt-get", "-y", "-o", "Dpkg::Use-Pty=0", "upgrade",
        ],
        timeout=RIG_APT_TIMEOUT,
        capture_full=True,
    )
    # Parse the complete apply response before discarding the private capture.
    # Only bounded tails remain in the artifact/substep.
    apply_stdout = upgrade.pop("_full_stdout", "")
    apply_stderr = upgrade.pop("_full_stderr", "")
    if not apply_stdout and not apply_stderr:
        apply_stdout = upgrade.get("stdout_tail", "")
        apply_stderr = upgrade.get("stderr_tail", "")
    apply_output = _rig_tail(apply_stdout, apply_stderr, limit=1400)
    applied_match = re.search(r"(?im)(\d+)\s+upgraded\b", apply_stdout + "\n" + apply_stderr)
    applied = int(applied_match.group(1)) if applied_match else 0

    output = _rig_tail(
        update.get("stdout_tail", ""), update.get("stderr_tail", ""),
        limit=1400,
    ) + "\n" + apply_output
    status = "ok" if upgrade["status"] == "ok" else "failed"
    result = {
        "step": "apt_upgrade",
        "status": status,
        "planned_count": planned,
        "upgraded_count": applied if status == "ok" else 0,
        "substeps": [update, plan, upgrade],
        "output_tail": output[-1800:],
    }
    if status == "ok" and applied_match is None:
        result["status"] = "failed"
        result["upgraded_count"] = 0
        result["error"] = "apt upgrade did not report an applied upgrade count"
    elif status == "failed":
        result["error"] = upgrade.get("error", "apt upgrade failed")
    return result


def _rig_herdr_update():
    """Update Herdr as the rig user; refuse to block on an interactive session."""
    pre_out, pre_err, pre_code = _rig_ssh(
        ["herdr", "--version"], timeout=30)
    pre_ver = pre_out.strip()
    if pre_code != 0 or not pre_ver:
        return {
            "step": "herdr_update",
            "status": "failed",
            "pre_version": pre_ver,
            "error": _rig_tail(pre_out, pre_err) or f"version probe exit {pre_code}",
        }

    stdout, stderr, code = _rig_ssh(
        ["herdr", "update"], timeout=RIG_UPDATE_TIMEOUT)
    detail = _rig_tail(stdout, stderr)
    if (
        "outside herdr" in detail.lower()
        or "inside a herdr session" in detail.lower()
        or "already in a herdr session" in detail.lower()
    ):
        return {
            "step": "herdr_update",
            "status": "skipped",
            "pre_version": pre_ver,
            "reason": "refused inside a Herdr session",
            "output_tail": detail,
        }

    post_out, post_err, post_code = _rig_ssh(
        ["herdr", "--version"], timeout=30)
    post_ver = post_out.strip()
    if code == 0 and post_code == 0 and post_ver and post_ver != pre_ver:
        return {
            "step": "herdr_update",
            "status": "ok",
            "pre_version": pre_ver,
            "post_version": post_ver,
            "output_tail": detail,
        }
    if code == 0 and post_code == 0 and post_ver == pre_ver:
        return {
            "step": "herdr_update",
            "status": "skipped",
            "pre_version": pre_ver,
            "post_version": post_ver,
            "reason": "already current",
            "output_tail": detail,
        }
    return {
        "step": "herdr_update",
        "status": "failed",
        "pre_version": pre_ver,
        "post_version": post_ver,
        "error": detail or _rig_tail(post_out, post_err) or f"update exit {code}",
    }


def _rig_omp_tag(version):
    """Extract a safe package version from ``omp --version`` output."""
    value = (version or "").strip()
    match = re.search(
        r"\bomp[/\s]+([vV]?[0-9][A-Za-z0-9._+-]*)",
        value,
        re.I,
    )
    if match:
        return match.group(1).lstrip("vV")
    match = re.fullmatch(r"[vV]?([0-9][A-Za-z0-9._+-]*)", value)
    return match.group(1) if match else ""


def _rig_omp_update():
    """Update the rig's OMP CLI, smoke it, and Bun-rollback a broken install."""
    pre_out, pre_err, pre_code = _rig_ssh(
        ["omp", "--version"], timeout=30)
    pre_ver = pre_out.strip()
    pre_tag = _rig_omp_tag(pre_ver)
    if pre_code != 0 or not pre_ver or not pre_tag:
        return {
            "step": "omp_update",
            "status": "failed",
            "pre_version": pre_ver,
            "error": _rig_tail(pre_out, pre_err)
                     or f"could not snapshot pre-update version (exit {pre_code})",
        }

    stdout, stderr, code = _rig_ssh(
        ["omp", "update"], timeout=RIG_UPDATE_TIMEOUT)
    detail = _rig_tail(stdout, stderr)
    post_out, post_err, post_code = _rig_ssh(
        ["omp", "--version"], timeout=30)
    post_ver = post_out.strip()
    smoke_out, smoke_err, smoke_code = _rig_ssh(
        ["omp", "-p"], timeout=60, input_data="")
    smoke_ok = smoke_code == 0

    common = {
        "step": "omp_update",
        "pre_version": pre_ver,
        "post_version": post_ver,
        "smoke_ok": smoke_ok,
        "smoke_output_tail": _rig_tail(smoke_out, smoke_err, limit=500),
        "output_tail": detail,
    }
    if code == 0 and post_code == 0 and smoke_ok and post_ver and post_ver != pre_ver:
        return {**common, "status": "ok"}
    if code == 0 and post_code == 0 and smoke_ok and post_ver == pre_ver:
        return {**common, "status": "skipped", "reason": "already current"}

    # The only dynamic argument is a strictly validated package version.
    rollback_args = [
        "bun", "add", "-g", f"{_OMP_PKG}@{pre_tag}",
    ]
    r_stdout, r_stderr, r_code = _rig_ssh(
        rollback_args, timeout=RIG_UPDATE_TIMEOUT)
    rev_out, rev_err, rev_code = _rig_ssh(
        ["omp", "--version"], timeout=30)
    rev_ver = rev_out.strip()
    rev_smoke_out, rev_smoke_err, rev_smoke_code = _rig_ssh(
        ["omp", "-p"], timeout=60, input_data="")
    reverted = (
        r_code == 0 and rev_code == 0 and rev_ver == pre_ver
        and rev_smoke_code == 0
    )
    return {
        **common,
        "status": "reverted" if reverted else "failed",
        "reverted_to": rev_ver,
        "rollback_ok": reverted,
        "rollback_exit_code": r_code,
        "rollback_output_tail": _rig_tail(
            r_stdout, r_stderr, limit=700),
        "rollback_smoke_output_tail": _rig_tail(
            rev_smoke_out, rev_smoke_err, limit=500),
        "error": (
            f"post-update check failed; reverted to {pre_tag}"
            if not smoke_ok else detail or f"update exit {code}"
        ),
    }


def _rig_disk_health():
    result = _rig_command_result("disk", ["df", "-P", "/"], timeout=30)
    if result["status"] != "ok":
        return result
    lines = [line.split() for line in
             (result.get("stdout_tail") or "").splitlines()
             if line.strip()]
    row = lines[-1] if lines else []
    percent = next((part for part in row if part.endswith("%")), "")
    if not percent:
        return {
            **result,
            "status": "failed",
            "error": "df output did not contain a root filesystem percentage",
        }
    try:
        used = int(percent.rstrip("%"))
    except ValueError:
        return {**result, "status": "failed",
                "error": f"invalid root filesystem percentage: {percent}"}
    result["used_percent"] = used
    if used > RIG_DISK_MAX_PERCENT:
        result["status"] = "failed"
        result["error"] = (
            f"root filesystem {used}% used (limit {RIG_DISK_MAX_PERCENT}%)")
    elif used >= 90:
        result["status"] = "warning"
        result["reason"] = f"root filesystem {used}% used"
    return result


def _rig_failed_units_health():
    result = _rig_command_result(
        "failed_units",
        ["systemctl", "--failed", "--no-legend", "--no-pager", "--plain"],
        timeout=30,
    )
    output = result.get("stdout_tail", "")
    units = []
    for line in output.splitlines():
        clean = line.strip()
        if not clean or re.match(r"^\d+\s+loaded units? listed", clean, re.I):
            continue
        if clean.lower().startswith("unit "):
            continue
        units.append(clean)
    if units:
        result["status"] = "failed"
        result["error"] = "failed systemd units: " + "; ".join(units[:8])
    elif (
        result["exit_code"] not in (0, 1)
        or (result["exit_code"] == 1 and result.get("stderr_tail"))
    ):
        result["status"] = "failed"
        result["error"] = result.get("error", "systemctl failed-unit query failed")
    else:
        result["status"] = "ok"
    if result["status"] == "ok" and result["exit_code"] == 1:
        result.pop("error", None)
    return result


def _rig_model_ids_from_response(raw):
    """Validate a complete OpenAI-compatible ``/v1/models`` response."""
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"malformed model endpoint JSON: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("model endpoint response is missing a data list")

    model_ids = []
    for index, model in enumerate(payload["data"]):
        if not isinstance(model, dict):
            raise ValueError(f"model endpoint entry {index} is not an object")
        model_id = model.get("id")
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValueError(f"model endpoint entry {index} is missing a non-empty id")
        model_ids.append(model_id)

    expected = set(RIG_REQUIRED_MODEL_IDS)
    actual = set(model_ids)
    if (
        len(model_ids) != len(RIG_REQUIRED_MODEL_IDS)
        or len(actual) != len(model_ids)
        or actual != expected
    ):
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        raise ValueError(
            "model endpoint IDs do not match retained registry"
            f" (missing={missing}, unexpected={unexpected}, count={len(model_ids)})"
        )
    return model_ids


def _rig_health_checks():
    """Collect non-mutating rig health checks in a stable order."""
    checks = [
        _rig_disk_health(),
        _rig_failed_units_health(),
        _rig_command_result(
            "nvidia_smi",
            [
                "nvidia-smi", "--query-gpu=name,memory.used,memory.total,"
                "utilization.gpu,temperature.gpu", "--format=csv,noheader",
            ],
            timeout=30,
        ),
        _rig_command_result(
            "llama_swap",
            ["systemctl", "is-active", "--quiet", "llama-swap.service"],
            timeout=30,
        ),
    ]
    model_check = _rig_command_result(
        "model_endpoint",
        [
            "curl", "-fsS", "--connect-timeout", "3", "--max-time", "8",
            RIG_MODEL_ENDPOINT,
        ],
        timeout=20,
        capture_full=True,
    )
    model_output = model_check.pop("_full_stdout", "")
    model_check.pop("_full_stderr", None)
    if model_check["status"] == "ok":
        try:
            model_check["model_ids"] = _rig_model_ids_from_response(
                model_output or model_check.get("stdout_tail", "")
            )
        except ValueError as exc:
            model_check["status"] = "failed"
            model_check["error"] = str(exc)
    checks.append(model_check)

    # ``nvidia-smi`` and curl are successful only when they return evidence.
    for check in checks:
        if check["step"] == "nvidia_smi":
            if check["status"] == "ok" and not check.get("stdout_tail"):
                check["status"] = "failed"
                check["error"] = "command returned no health evidence"
    statuses = [check["status"] for check in checks]
    if any(status == "failed" for status in statuses):
        status = "failed"
    elif any(status == "warning" for status in statuses):
        status = "warning"
    else:
        status = "ok"
    result = {
        "step": "health",
        "status": status,
        "checks": checks,
    }
    if status in ("failed", "warning"):
        result["error"] = "; ".join(
            f"{check['step']}: {check.get('error') or check.get('reason') or check['status']}"
            for check in checks if check.get("status") in ("failed", "error", "warning")
        )
    return result


def _rig_reboot_required():
    """Detect reboot-required without treating the normal false result as fail."""
    result = _rig_command_result(
        "reboot_required",
        ["test", "-f", "/var/run/reboot-required"],
        timeout=20,
    )
    code = result.get("exit_code")
    if code == 1:
        result["status"] = "skipped"
        result["required"] = False
        result["reason"] = "reboot not required"
        result.pop("error", None)
    elif code == 0:
        result["status"] = "ok"
        result["required"] = True
    else:
        result["status"] = "failed"
        result["required"] = False
        result["error"] = result.get("error", "could not inspect reboot-required flag")
    return result

def _rig_boot_id(step="boot_id"):
    """Read and validate the current Linux boot identifier."""
    result = _rig_command_result(
        step, ["cat", "/proc/sys/kernel/random/boot_id"], timeout=20)
    boot_id = (result.get("stdout_tail") or "").strip()
    if (
        result["status"] != "ok"
        or not re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            boot_id,
            re.I,
        )
    ):
        result["status"] = "failed"
        result["error"] = result.get("error") or "invalid Linux boot ID"
        return result
    result["boot_id"] = boot_id
    return result


def _rig_arm_bootnext():
    """Re-arm Ubuntu's established firmware entry before a rig reboot."""
    arm = _rig_command_result(
        "bootnext_arm",
        ["sudo", "-n", "efibootmgr", "--bootnext", RIG_BOOT_ENTRY],
        timeout=30,
    )
    verify = _rig_command_result(
        "bootnext_verify", ["sudo", "-n", "efibootmgr"], timeout=30)
    verified = (
        verify["status"] == "ok"
        and bool(re.search(
            rf"(?im)^\s*BootNext:\s*{re.escape(RIG_BOOT_ENTRY)}\b",
            verify.get("stdout_tail", ""),
        ))
    )
    result = {
        "step": "bootnext",
        "status": "ok" if arm["status"] == "ok" and verified else "failed",
        "entry": RIG_BOOT_ENTRY,
        "substeps": [arm, verify],
    }
    if not verified:
        result["error"] = (
            verify.get("error")
            or f"efibootmgr did not report BootNext: {RIG_BOOT_ENTRY}"
        )
    return result


def _rig_reboot():
    """Request reboot; a connection close/reset is expected on success."""
    stdout, stderr, code = _rig_ssh(
        ["sudo", "-n", "systemctl", "reboot"], timeout=20)
    detail = _rig_tail(stdout, stderr)
    lower = detail.lower()
    disconnected = (
        "connection reset" in lower
        or "broken pipe" in lower
        or bool(re.search(r"connection(?: to .+)? closed", lower))
    )
    if code == 0 or disconnected:
        return {
            "step": "reboot",
            "status": "ok",
            "requested": True,
            "connection_lost": bool(code != 0),
            "output_tail": detail,
        }
    return {
        "step": "reboot",
        "status": "failed",
        "requested": False,
        "error": detail or f"reboot command exit {code}",
    }


def _rig_wait_for_linux(previous_boot_id, timeout_s=RIG_REBOOT_WAIT_TIMEOUT):
    """Wait for pinned Linux SSH to return on a demonstrably new boot."""
    deadline = time.monotonic() + max(0, int(timeout_s))
    attempts = 0
    last = {}
    while True:
        attempts += 1
        stdout, stderr, code = _rig_ssh(
            ["cat", "/proc/sys/kernel/random/boot_id"],
            timeout=RIG_SSH_CONNECT_TIMEOUT,
        )
        boot_id = (stdout or "").strip()
        last = {
            "stdout": stdout[-180:] if stdout else "",
            "stderr": stderr[-280:] if stderr else "",
            "exit_code": code,
            "boot_id": boot_id,
        }
        if code == 0 and boot_id and boot_id != previous_boot_id:
            return {
                "step": "ssh_return",
                "status": "ok",
                "attempts": attempts,
                "os": "Linux",
                "boot_id": boot_id,
            }
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(2, remaining))
    reason = (
        "Linux SSH returned but the boot ID did not change"
        if last.get("exit_code") == 0 and last.get("boot_id") == previous_boot_id
        else _rig_probe_reason(
            last.get("stdout", ""),
            last.get("stderr", ""),
            last.get("exit_code", -1),
        )
    )
    return {
        "step": "ssh_return",
        "status": "failed",
        "attempts": attempts,
        "error": (
            f"new Linux boot did not appear within {int(timeout_s)}s: {reason}"
        ),
        "last_probe": last,
    }


def _rig_wait_for_health(timeout_s=RIG_REBOOT_WAIT_TIMEOUT):
    """Poll all rig health/readiness checks until the new boot is healthy."""
    deadline = time.monotonic() + max(0, int(timeout_s))
    attempts = 0
    last = {
        "step": "health",
        "status": "failed",
        "checks": [],
        "error": "no post-reboot health result",
    }
    while True:
        attempts += 1
        last = _rig_health_checks()
        if last.get("status") == "ok":
            gate = dict(last)
            gate["step"] = "post_reboot_health"
            gate["attempts"] = attempts
            return gate
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(2, remaining))

    gate = dict(last)
    gate["step"] = "post_reboot_health"
    gate["status"] = "failed"
    gate["attempts"] = attempts
    detail = str(last.get("error") or last.get("status") or "unhealthy")
    gate["error"] = (
        f"post-reboot health did not pass within {int(timeout_s)}s: {detail[:300]}"
    )
    return gate


def _rig_has_failure(value):
    """Recursively detect a failed command/health result."""
    if isinstance(value, dict):
        if value.get("status") in ("failed", "error"):
            return True
        return any(_rig_has_failure(item) for item in value.get("substeps", []))
    if isinstance(value, list):
        return any(_rig_has_failure(item) for item in value)
    return False


def _p1_gamingrig_maintenance(dry_run=False):
    """Maintain gamingrig-linux without waking it or switching its operating system."""
    result = {
        "step": "gamingrig_maintenance",
        "host": RIG_SSH_ALIAS,
        "local_mutation": False,
        "substeps": [],
        "reboot_requested": False,
        "new_boot_observed": False,
        "rebooted": False,
        "post_reboot_health_passed": False,
    }
    if dry_run:
        result.update({
            "status": "skipped",
            "reason": "dry-run: remote maintenance not attempted",
        })
        return result

    try:
        probe, linux = _rig_platform_probe()
        result["substeps"].append(probe)
        if not linux:
            result.update({
                "status": "skipped" if probe.get("status") == "skipped" else "failed",
                "reason": probe.get("reason", "Linux platform unavailable"),
            })
            if probe.get("status") != "skipped":
                result["error"] = probe.get("error") or probe.get("reason")
            return result
        result["os"] = "Linux"

        # Keep independent maintenance steps running so one remote failure is
        # evidence in the artifact, not an exception that skips later checks.
        result["substeps"].append(_rig_apt_upgrade())
        result["substeps"].append(_rig_herdr_update())
        result["substeps"].append(_rig_omp_update())

        health = _rig_health_checks()
        result["substeps"].append(health)
        result["health"] = health

        reboot = _rig_reboot_required()
        result["substeps"].append(reboot)
        if reboot.get("required"):
            boot_id = _rig_boot_id("pre_reboot_boot_id")
            result["substeps"].append(boot_id)
            if boot_id["status"] == "ok":
                bootnext = _rig_arm_bootnext()
                result["substeps"].append(bootnext)
                if bootnext["status"] == "ok":
                    reboot_result = _rig_reboot()
                    result["substeps"].append(reboot_result)
                    if (
                        reboot_result["status"] == "ok"
                        and reboot_result.get("requested", True)
                    ):
                        result["reboot_requested"] = True
                        returned = _rig_wait_for_linux(boot_id["boot_id"])
                        result["substeps"].append(returned)
                        if returned["status"] == "ok":
                            result["new_boot_observed"] = True
                            result["rebooted"] = True
                            post_health = _rig_wait_for_health()
                            result["substeps"].append(post_health)
                            result["post_reboot_health"] = post_health
                            result["post_reboot_health_passed"] = (
                                post_health.get("status") == "ok"
                            )

        result["rebooted"] = (
            result["reboot_requested"] and result["new_boot_observed"]
        )
        result["status"] = (
            "failed" if _rig_has_failure(result["substeps"]) else "ok"
        )
        if result["status"] == "failed":
            failures = [
                sub.get("error", sub.get("reason", sub.get("step", "failure")))
                for sub in result["substeps"]
                if _rig_has_failure(sub)
            ]
            result["error"] = "; ".join(str(item) for item in failures)
        return result
    except Exception as exc:
        # A malformed local invocation must still leave the rest of P1 alive.
        result.update({
            "status": "failed",
            "error": f"remote maintenance exception: {exc}",
        })
        return result


def phase_1_apply(run_dir, dry_run=False):
    """Phase 1: apply safe updates. Skip if --dry-run."""
    # Remote maintenance is first so local P1 failures cannot skip it.
    steps = [_p1_gamingrig_maintenance(dry_run=dry_run)]
    if dry_run:
        print("[P1] DRY RUN — skipping all mutations")
        data = {"dry_run": True, "steps": steps}
        return _finish_p1(run_dir, data)

    print("[P1] applying safe updates")

    # 1a: apt upgrade
    result = _p1_apt_upgrade()
    steps.append(result)
    if result["status"] == "failed":
        print(f"  FAILED: apt upgrade — {result.get('error')}")
        data = {"steps": steps}
        return _finish_p1(run_dir, data)

    # 1b: auto-apply docker + cloudflared
    auto_results = _p1_auto_pkgs()
    steps.extend(auto_results)
    for r in auto_results:
        if r["status"] == "failed":
            print(f"  FAILED: {r['step']} — {r.get('error')}")
            data = {"steps": steps}
            return _finish_p1(run_dir, data)

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

    # 1h: searxng — advance seven-day-old immutable pins with rollback
    steps.append(_p1_searxng_update())

    # 1i: llama.cpp — build seven-day-old Linux releases with atomic rollback
    steps.append(_p1_llama_cpp_update())

    # 1j: omp self-update (last local mutation; swaps the steward's own agents)
    steps.append(_p1_omp_update())


    data = {"steps": steps}
    data = _finish_p1(run_dir, data)
    n_ok = sum(1 for s in steps if s["status"] == "ok")
    n_bumped = sum(1 for s in steps if s["status"] == "bumped")
    n_available = sum(1 for s in steps if s["status"] == "available")
    n_skipped = sum(1 for s in steps if s["status"] == "skipped")
    n_failed = sum(1 for s in steps if s["status"] == "failed")
    print(f"[P1] done -> {run_dir / '01-applied.json'}")
    print(f"  {n_ok} ok, {n_bumped} bumped, {n_available} available, "
          f"{n_skipped} skipped, {n_failed} failed")
    return data

