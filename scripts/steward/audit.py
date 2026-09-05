"""Evidence collectors and worker/judge audit workflow."""
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
        "llama_cpp": "not-collected — worker verifies read-only via `ssh gamingrig-linux`",
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
                    "attention_artifact": "02b-attention.json" in artifacts,
                    "standfirst_artifact": "07-standfirst.json" in artifacts,
                })
                evidence["placeholder_leakage"] += placeholder_count
        evidence["topics"][topic] = tev
    # Published-site contract: every completed date has one durable JSON artifact
    # per category, an atomically activated static build, one mail marker, and a
    # configured target in the existing R2 backup.
    news_root = HOME / "digests" / "news"
    publications_dir = news_root / "publications"
    publication_dates = []
    if publications_dir.exists():
        dated_dirs = [
            path for path in sorted(publications_dir.iterdir(), reverse=True)
            if path.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", path.name)
        ]
        for date_dir in dated_dirs[:2]:
            entries = {}
            for slug in ("ai-tech", "agents", "ai-hardware", "gaming", "world"):
                path = date_dir / f"{slug}.json"
                entry = {"exists": path.exists(), "valid": False, "stories": 0}
                if path.exists():
                    try:
                        publication_text = path.read_text()
                        publication = json.loads(publication_text)
                        stories = (
                            publication.get("fresh", [])
                            + publication.get("ongoing", [])
                        )
                        standfirst = publication.get("standfirst", "")
                        attention_path = (
                            news_root / "attention" / date_dir.name / f"{slug}.json"
                        )
                        entry.update({
                            "valid": (
                                publication.get("date") == date_dir.name
                                and publication.get("slug") == slug
                                and publication.get("schema_version") == 2
                                and (
                                    date_dir.name < "2026-08-25"
                                    or publication.get("ranking_schema_version") == 3
                                )
                            ),
                            "status": publication.get("status", ""),
                            "stories": len(stories),
                            "ranking_schema_version": publication.get(
                                "ranking_schema_version"
                            ),
                            "significance_complete": all(
                                story.get("editorial_significance")
                                in {"high", "medium", "low"}
                                for story in stories
                            ),
                            "high_evidence_complete": (
                                date_dir.name < "2026-08-25"
                                or all(
                                    story.get("editorial_significance") != "high"
                                    or (
                                        isinstance(story.get("significance_evidence"), dict)
                                        and story.get("significance_validation", {}).get("status")
                                        == "accepted"
                                    )
                                    for story in (
                                        publication.get("fresh", [])
                                        + publication.get("ongoing", [])
                                    )
                                )
                            ),
                            "priority_complete": all(
                                isinstance(story.get("priority_score"), (int, float))
                                for story in stories
                            ),
                            "attention_complete": (
                                date_dir.name < "2026-08-25"
                                or all(
                                    isinstance(story.get("attention"), dict)
                                    and story["attention"].get("status")
                                    in {"ok", "no_matches", "unavailable", "out_of_scope"}
                                    for story in stories
                                )
                            ),
                            "attention_failure_semantics_valid": (
                                date_dir.name < "2026-08-25"
                                or all(
                                    (
                                        story.get("attention", {}).get("status") != "no_matches"
                                        or (
                                            story["attention"].get("attention_now") == 0
                                            and story["attention"].get("digest_prominence") == 0
                                        )
                                    )
                                    and (
                                        story.get("attention", {}).get("status") != "unavailable"
                                        or story["attention"].get("confidence") == 0
                                    )
                                    for story in stories
                                )
                            ),
                            "attention_artifact_exists": (
                                date_dir.name < "2026-08-25" or attention_path.exists()
                            ),
                            "standfirst_complete": (
                                isinstance(standfirst, str)
                                and 40 <= len(standfirst) <= 900
                                and bool(re.search(r"""[.!?…]["'’”)]*$""", standfirst))
                                and not bool(re.search(
                                    r"today[’']s digest|digest leads|read on|also in focus",
                                    standfirst,
                                    re.IGNORECASE,
                                ))
                            ),
                            "placeholder_leaks": len(re.findall(
                                r"\{\{[A-Z_]+\}\}|https?://example\.com\b",
                                publication_text,
                            )),
                        })
                    except (json.JSONDecodeError, OSError) as error:
                        entry["error"] = str(error)
                entries[slug] = entry
            publication_dates.append({
                "date": date_dir.name,
                "categories": entries,
                "summary_email_sent": (
                    news_root / "mail" / f"{date_dir.name}.sent.json"
                ).exists(),
            })
    current_site = news_root / "current"
    build_path = current_site / "build.json"
    try:
        build = json.loads(build_path.read_text()) if build_path.exists() else {}
    except (json.JSONDecodeError, OSError):
        build = {}
    backup_config_path = HOME / "homelab-backup" / "config.yaml"
    try:
        backup_config = backup_config_path.read_text()
    except OSError:
        backup_config = ""
    evidence["publication"] = {
        "contract_started": "2026-08-25",
        "dates": publication_dates,
        "site_current_is_symlink": current_site.is_symlink(),
        "site_index_exists": (current_site / "index.html").exists(),
        "build": build,
        "r2_target_configured": (
            "name: daily-news-data" in backup_config
            and "source: /home/carter/digests/news" in backup_config
        ),
        "backup_config_mtime": (
            datetime.fromtimestamp(
                backup_config_path.stat().st_mtime, timezone.utc
            ).isoformat()
            if backup_config_path.exists() else ""
        ),
    }

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
    incident_doc = HOME / "notes" / "docs" / "homelab" / "opencode-go-proxy.md"
    incident_read_error = ""
    try:
        incident_text = incident_doc.read_text()
    except OSError as error:
        incident_text = ""
        incident_read_error = str(error)
    if "Known credential incident: unresolved." in incident_text:
        incident_status = "unresolved"
    elif "Known credential incident: resolved." in incident_text:
        incident_status = "resolved"
    else:
        incident_status = "unverifiable"
    known_credential_incident = {
        "status": incident_status,
        "source": str(incident_doc),
        "read_error": incident_read_error,
        "detail": (
            "A credential remains in public git history unless explicit provider "
            "revocation/cancellation is recorded. Do not inspect or reproduce it."
        ),
    }

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
        "known_credential_incident": known_credential_incident,
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
    # Scan the whole current boot's kernel log, not a rolling 24h window:
    # a daily steward run can otherwise miss an OOM event that happened
    # >24h before it (e.g. Aug 05 17:52 UTC event unseen by Aug 06 21:00 run).
    oom_hunt = run_capture(
        ["journalctl", "-k", "-b", "--no-pager", "-q"])
    oom_count = oom_hunt.lower().count("out of memory") if oom_hunt else 0
    exit_255 = run_capture(
        ["docker", "ps", "-a", "--filter", "status=exited",
         "--format", "{{.Names}}\t{{.Status}}\t{{.Image}}"])

    # R2 size
    r2_list = run_capture(
        [str(HOME / "homelab-backup" / "homelab-backup"), "list"])
    news_state_sizes = {}
    news_root = HOME / "digests" / "news"
    for name in ("publications", "attention", "mail"):
        root = news_root / name
        total = 0
        if root.exists():
            for path in root.rglob("*"):
                try:
                    if path.is_file():
                        total += path.stat().st_size
                except OSError:
                    pass
        news_state_sizes[name] = total

    return {
        "disk_df": run_capture(["df", "-h", "/"]),
        "docker_system_df": run_capture(["docker", "system", "df"]),
        "oom_count": oom_count,
        "exit_255_containers": exit_255,
        "r2_list_tail": "\n".join(r2_list.splitlines()[-20:]) if r2_list else "",
        "daily_news_state_bytes": news_state_sizes,
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
            "semantic facts: IP roles (.100 DHCP/default, .92 k3s+blog ingress), "
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
            "Compare current versions (in evidence) against latest upstream stable for components NOT "
            "auto-updated by P1: k3s, Go, Node, Ruby (rbenv), neovim, and the traefik docker image. "
            "Do NOT report freshrss, open-webui, herdr, omp, the pinned searxng image, or llama.cpp "
            "on the gaming rig — P1 updates those earlier in the same run after their safety gates. "
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
            "Judge Daily News over the last 48 hours plus systemic regressions: completeness, "
            "freshness, duplication, tracker hygiene, five schema-v2/ranking-v3 publications, "
            "complete standfirsts, and active front page. Every high significance must have "
            "accepted source-grounded evidence; routine deprecations without demonstrated broad "
            "impact must be downgraded. `no_matches` must score attention/prominence 0; "
            "`unavailable` must have confidence 0 and editorial-only priority. Check deterministic "
            "priority fields, durable attention records, one mail marker after the 2026-08-25 "
            "cutover, and the daily-news-data R2 target. Do not reopen known historical empty days. "
            "Sample up to 3 source links read-only."
        ),
    },
    {
        "name": "security-posture",
        "collector": _audit_collector_4_security,
        "artifact": "07-audit-4-security.json",
        "timeout": 600,
        "guidance": (
            "Judge the security posture from the evidence: listening sockets vs the documented set "
            "(loopback-only: open-webui 48100, searxng 8080, prompt-guard 8090, news 30144, herdr-web 30145, "
            "beatz 30142, blog 33099; "
            "ufw-gated: llm-proxy 8081, opencode-go-proxy 8082), ufw ruleset intact "
            "(cni0/flannel.1/docker bridges), unattended-upgrades active, carter2099.com RDAP expiry "
            "(>30d out = ok), CF tunnel ingress vs expected hostnames (chat, hooks, freshrss, blog, "
            "omp, ssh, beatz, rig, news, remote), SSH failed-password volume. Flag anything unexpected. "
            "For repo_secrets: working_tree_issues means secret-pattern files are uncommitted in a "
            "repo — flag each as ATTENTION; commit_issues means a secret-pattern string appeared in "
            "recent diffs — flag as ATTENTION with the commit SHA. No findings = PASS for this sub-check."
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
            "should be clean; deploy dirs (blog, homelab-backup) should match origin/main "
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
            "Interpret the resource evidence: disk usage/growth, docker reclaimable space, journal "
            "size, R2 archive growth, Daily News publication/attention/mail growth, OOM kills, and "
            "exited containers (known intermittent exit-255; flag repeats on one container). Report "
            "ATTENTION only for actionable trends such as disk >80%, sustained week-over-week "
            "attention-history growth that threatens R2 limits, or recurring OOM."
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
            "hyperliquid-sdk.md, dependabot-webhook.md, open-webui.md, searxng.md, "
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


_AUDIT_VERDICTS = {"PASS", "DRIFT", "ATTENTION", "UNVERIFIABLE"}


def _final_audit_verdict(worker_verdict, judge_packet):
    """Use the judge's explicit verdict; never hide confirmed problems behind PASS."""
    if judge_packet.get("judge_error"):
        return "judge-failed"
    candidate = str(judge_packet.get("verdict") or "").upper()
    if candidate not in _AUDIT_VERDICTS:
        return "UNVERIFIABLE"
    return candidate

def _validate_prepared_audit_worker_packet(packet):
    """Validate steward-assigned finding IDs and mutation-relevant worker fields."""
    if not isinstance(packet, dict) or packet.get("verdict") not in _AUDIT_VERDICTS:
        raise ValueError("invalid prepared audit worker packet")
    findings = packet.get("findings")
    if not isinstance(findings, list):
        raise ValueError("prepared audit worker findings must be a list")
    for index, item in enumerate(findings, 1):
        if not isinstance(item, dict) or item.get("id") != f"finding-{index}":
            raise ValueError("prepared audit worker IDs must be unique and sequential")
        for key in ("claim", "evidence", "fix"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise ValueError(f"prepared audit worker finding missing {key}")
    return packet


def _prepare_audit_worker_packet(packet):
    """Validate worker JSON and assign stable per-packet finding IDs."""
    if not isinstance(packet, dict):
        raise ValueError("audit worker packet must be an object")
    verdict = packet.get("verdict")
    findings = packet.get("findings")
    if verdict not in _AUDIT_VERDICTS or not isinstance(findings, list):
        raise ValueError("audit worker packet has invalid verdict/findings")
    prepared = dict(packet)
    prepared_findings = []
    for index, finding in enumerate(findings, 1):
        if not isinstance(finding, dict):
            raise ValueError("audit worker findings must be objects")
        item = dict(finding)
        for key in ("claim", "evidence", "fix"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise ValueError(f"audit worker finding missing {key}")
        item["id"] = f"finding-{index}"
        prepared_findings.append(item)
    prepared["findings"] = prepared_findings
    return _validate_prepared_audit_worker_packet(prepared)


def _validate_audit_judge_packet(packet, worker_packet):
    """Require a one-to-one judge disposition for worker-assigned finding IDs."""
    if not isinstance(packet, dict):
        raise ValueError("audit judge packet must be an object")
    _validate_prepared_audit_worker_packet(worker_packet)
    verdict = packet.get("verdict")
    if verdict not in _AUDIT_VERDICTS:
        raise ValueError(f"invalid audit judge verdict: {verdict!r}")
    worker_by_id = {
        item["id"]: item for item in worker_packet.get("findings", [])
        if isinstance(item, dict) and item.get("id")
    }
    seen = set()
    for key in ("confirmed", "rejected"):
        value = packet.get(key)
        if not isinstance(value, list):
            raise ValueError(f"audit judge {key} must be a list")
        for item in value:
            if not isinstance(item, dict):
                raise ValueError(f"audit judge {key} items must be objects")
            finding_id = item.get("id")
            worker_finding = worker_by_id.get(finding_id)
            if worker_finding is None or finding_id in seen:
                raise ValueError(f"audit judge returned invalid/duplicate id: {finding_id!r}")
            if item.get("claim") != worker_finding.get("claim"):
                raise ValueError(f"audit judge claim mismatch for {finding_id}")
            if key == "confirmed" and item.get("fix") != worker_finding.get("fix"):
                raise ValueError(f"audit judge fix mismatch for {finding_id}")
            detail_key = "evidence" if key == "confirmed" else "reason"
            if not isinstance(item.get(detail_key), str) or not item[detail_key].strip():
                raise ValueError(f"audit judge {key} item missing {detail_key}")
            seen.add(finding_id)
    if seen != set(worker_by_id):
        raise ValueError("audit judge did not disposition every worker finding")
    confirmed_count = len(packet["confirmed"])
    if verdict in ("PASS", "UNVERIFIABLE") and confirmed_count:
        raise ValueError(f"audit judge {verdict} cannot confirm problems")
    if verdict in ("DRIFT", "ATTENTION") and not confirmed_count:
        raise ValueError(f"audit judge {verdict} requires a confirmed problem")
    return packet


def _apply_deterministic_audit_guards(section_name, evidence, verdict, confirmed):
    """Surface persistent manual incidents even when an LLM misses them."""
    confirmed = list(confirmed or [])
    if verdict.endswith("-failed"):
        return verdict, confirmed
    incident = evidence.get("known_credential_incident", {})
    if (
        section_name == "security-posture"
        and incident.get("status") in ("unresolved", "unverifiable")
    ):
        claim = "Known public-history credential incident remains unresolved"
        if not any(
            item.get("claim") == claim for item in confirmed if isinstance(item, dict)
        ):
            confirmed.append({
                "claim": claim,
                "evidence": (
                    f"Nonsecret status is unresolved in {incident.get('source', 'runbook')}; "
                    "provider revocation/cancellation is not verified."
                ),
            })
        return "ATTENTION", confirmed
    return verdict, confirmed


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
- If the investigation is incomplete or you must stop early, your FINAL turn must still
  emit the fenced ```json packet — verdict "UNVERIFIABLE" with a finding describing what
  could not be verified. Never end on prose.

{_date_context()}

COLLECTED EVIDENCE:
{json.dumps(evidence, indent=2, default=str)[:8000]}

RECENT SESSION MEMORY (Carter's recent interactive omp sessions — context for interpreting homelab state):
{session_memory}
"""

    def _run_worker(prompt_text, label):
        raw = _call_omp_p(
            prompt_text, model=SMALL_MODEL, timeout=section["timeout"], mode="json"
        )
        return _prepare_audit_worker_packet(_extract_json(raw, label))

    try:
        worker_packet = _run_worker(worker_prompt, f"worker-{section_name}")
    except Exception as e:
        # Worker ended without a JSON packet (truncated run / prose-only ack).
        # Retry once with a packet-only continuation so an interrupted worker
        # still yields a verdict — never persist worker-failed on the first miss.
        retry_prompt = f"""
Your audit run for section '{section_name}' ended without the required fenced
```json packet. Emit ONLY the fenced ```json packet now, reflecting whatever you
verified:

{{"verdict": "PASS"|"DRIFT"|"ATTENTION"|"UNVERIFIABLE",
 "findings": [{{"claim": "...", "evidence": "...", "fix": "..."}}]}}

- If the investigation was incomplete, verdict "UNVERIFIABLE" with a finding noting
  what could not be verified.
- This turn contains the JSON packet and nothing else.
"""
        try:
            worker_packet = _run_worker(retry_prompt, f"worker-{section_name}-retry")
        except Exception as e2:
            return {
                "name": section_name,
                "verdict": "worker-failed",
                "error": f"{e}; retry also failed: {e2}",
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
{{"verdict": "PASS"|"DRIFT"|"ATTENTION"|"UNVERIFIABLE",
 "confirmed": [{{"id": "finding-1", "claim": "...", "evidence": "...", "fix": "..."}}],
 "rejected": [{{"id": "finding-2", "claim": "...", "reason": "..."}}]}}
- Return every worker finding ID exactly once across `confirmed` and `rejected`.
- A confirmed finding must copy the worker's `claim` and `fix` verbatim.
- `confirmed` contains unresolved problem findings only, never healthy-state confirmations.
- `PASS` requires an empty `confirmed` list. Use `ATTENTION` for unresolved manual/security
  action and `DRIFT` for a concrete state/config mismatch.
- CRITICAL: every yielding turn must include the fenced ```json block. If the advisor
  requests changes, emit a REVISED ```json packet — never a prose-only ack.
"""
    try:
        judge_text = _call_omp_p(judge_prompt, timeout=section["timeout"], mode="json")
        judge_packet = _validate_audit_judge_packet(
            _extract_json(judge_text, f"judge-{section_name}"),
            worker_packet,
        )
    except Exception as e:
        judge_packet = {
            "confirmed": [],
            "rejected": [],
            "judge_error": str(e),
        }

    confirmed = judge_packet.get("confirmed", [])
    rejected = judge_packet.get("rejected", [])
    worker_verdict = worker_packet.get("verdict", "UNVERIFIABLE")
    final_verdict = _final_audit_verdict(worker_verdict, judge_packet)
    final_verdict, confirmed = _apply_deterministic_audit_guards(
        section_name, evidence, final_verdict, confirmed
    )
    return {
        "name": section_name,
        "verdict": final_verdict,
        "worker_verdict": worker_verdict,
        "judge_verdict": judge_packet.get("verdict", ""),
        "judge_error": judge_packet.get("judge_error", ""),
        "evidence_hash": current_hash,
        "worker_findings": worker_packet.get("findings", []),
        "judge_confirmed": confirmed,
        "judge_rejected": rejected,
    }


def _audit_artifact_cacheable(artifact):
    """Cache only artifacts with a complete, provenance-valid judge disposition."""
    if artifact.get("judge_error"):
        return False
    base_verdict = str(artifact.get("verdict", "")).removeprefix("cached-")
    if base_verdict not in _REAL_VERDICTS:
        return False
    worker_packet = {
        "verdict": artifact.get("worker_verdict"),
        "findings": artifact.get("worker_findings", []),
    }
    judge_packet = {
        "verdict": artifact.get("judge_verdict"),
        "confirmed": artifact.get("judge_confirmed", []),
        "rejected": artifact.get("judge_rejected", []),
    }
    try:
        _validate_prepared_audit_worker_packet(worker_packet)
        _validate_audit_judge_packet(judge_packet, worker_packet)
    except ValueError:
        return False
    return _final_audit_verdict(worker_packet["verdict"], judge_packet) == base_verdict


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
            if prev_hash == current_hash and _audit_artifact_cacheable(prev_artifact):
                print(f"    delta-gate: unchanged -> cached-{base_verdict}")
                result = {
                    "name": section_name,
                    "verdict": f"cached-{base_verdict}",
                    "worker_verdict": prev_artifact.get("worker_verdict", ""),
                    "judge_verdict": prev_artifact.get("judge_verdict", ""),
                    "judge_error": "",
                    "evidence_hash": current_hash,
                    "worker_findings": prev_artifact.get("worker_findings", []),
                    "judge_confirmed": prev_artifact.get("judge_confirmed", []),
                    "judge_rejected": prev_artifact.get("judge_rejected", []),
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

