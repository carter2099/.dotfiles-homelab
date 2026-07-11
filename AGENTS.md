# AGENTS.md

This file provides guidance to AI agents (pi, Claude, etc.) when working with code in this repository.

**Maintenance:** Keep this file up to date. When deploying a new app, adding a service, changing ports/IPs, or making any structural changes to the homelab, update the relevant sections here as part of that work.

## Working principles (Endler tenets)

Carter endorses the tenets in [The Best Programmers](https://endler.dev/2025/best-programmers/). The subset below is the part that applies directly to an LLM assistant and should shape every session.

- **Read the reference.** Prefer official docs, man pages, and the actual source over recall from training data. When something in this repo is in question, read the file. Training-data recall about APIs, flags, or versions is frequently stale — verify.
- **Read the error message.** Parse errors fully before reacting. The message usually names the cause; skimming past it and guessing wastes Carter's time.
- **Don't guess.** If a fact is load-bearing for the answer or action, verify it with a tool (grep, read, `--help`, a quick script) rather than asserting from memory. This is the single most important one.
- **Say "I don't know."** Uncertainty is fine and useful; confident bullshit is not. If a recommendation rests on something unverified, say so explicitly rather than smoothing it over.
- **Never blame the computer.** "Flaky test," "weird cache," "probably a transient issue" are hypotheses, not conclusions. Bugs have causes — keep investigating until the cause is named, even if the fix is a retry.
- **Break down problems.** For non-trivial work, decompose before diving in. A plan or task list beats a sprawling edit.
- **Know your tools.** Before using an unfamiliar CLI, systemd unit, k3s resource, or library in this homelab, understand it enough to predict what it'll do. Don't run commands whose effect you can't describe.
- **Get your hands dirty.** Don't refuse to engage with unfamiliar code, obscure config, or messy state. Read it, trace it, fix it.
- **Keep it simple.** Prefer the smallest change that solves the problem. This reinforces the existing "no gratuitous abstractions / no speculative features" guidance further down in this file.
- **Have patience.** Don't rush to a conclusion or a fix. Re-read, re-check, confirm before acting — especially for anything irreversible.

## Scope

Carter wants this agent framed as a **homelab assistant and general personal assistant**, not narrowly as a coding tool. Software engineering is a large part of the work, but non-code help (planning, notes, research, life admin, digests, correspondence drafting, scheduling) is equally in scope and should be treated as first-class. The same tenets about rigor, not-guessing, and admitting uncertainty apply regardless of domain.

## Overview

Single-node homelab running on Ubuntu Server (ThinkPad L14 Gen 3, AMD Ryzen 5 PRO 5675U, 16GB RAM, 500GB NVMe SSD). A k3s Kubernetes cluster routes traffic via Traefik ingress to apps running in Docker Compose on the host machine. The server uses wired ethernet (`enp3s0f0`) as its primary uplink, with static secondary IPs `192.168.4.92` (blog, delta_neutral) and `192.168.4.102` (tbitt, stickies — both not live) — all on the same physical interface. WiFi (`wlp6s0`) is disabled.

## Hardware

| Component | Details |
|---|---|
| **Model** | ThinkPad L14 Gen 3 (AMD) |
| **CPU** | AMD Ryzen 5 PRO 5675U (6C/12T, 2.3–4.3GHz) |
| **RAM** | 16GB DDR4-3200 (2x SO-DIMM slots, dual-channel) |
| **Storage** | 500GB NVMe M.2 2242 SSD (PCIe 3.0 x4) |
| **Network** | Gigabit Ethernet (Realtek RTL8111HN/EPV), Wi-Fi 6E, Bluetooth 5.1 |

**Notes:**
- The wired NIC `enp3s0f0` (Realtek) is the **primary uplink**.
- **WiFi is disabled** in netplan. The interface `wlp6s0` is down by default.
- **Network config:** `/etc/netplan/50-cloud-init.yaml` (managed by systemd-networkd)
  - `enp3s0f0`: DHCP primary (`192.168.4.100`), static secondary (`192.168.4.92/22`, `192.168.4.102/22`)
  - `wlp6s0`: Removed from netplan — disabled
- **Default route:** Via `enp3s0f0` (metric 100)
- **k3s ingress IPs:** `192.168.4.92` (blog, delta_neutral) and `192.168.4.102` (tbitt, stickies — both not live) are secondary IPs on the wired interface.

## Repository Structure

This is the home directory, managed as a bare git repo for dotfiles:
- `blog/` - Rails 8 blog app (blog.carter2099.com)
- `hub/` - React + Rails API landing page/portfolio, **not live** (carter2099.com)
- `tbitt/` - React + Express memecoin tracker, **deprecated** (tbitt.carter2099.com)
- `stickies/` - Sticky notes app, **not live** (stickiesapi.carter2099.com)
- `delta_neutral/` - Rails 8 Hyperliquid rebalancer (deltaneutral.carter2099.com)
- `homelab-backup/` - Go backup service (daily R2 backups of blog content, DBs, FreshRSS)
- `dev/dependabot-webhook/` - Go webhook receiver for automated dependabot PR handling
- `k3s/` - Kubernetes manifests organized by service
- `ddns/` - Cloudflare DDNS updater for WireGuard endpoint
- `build/` - Source builds (neovim)
- `dev/` - Scratch space for cloning GitHub repos, running tests, and doing development work
- `scripts/` - Digest run scripts (`run_<topic>_digest.sh`, `send_digest.py`)
- `notes/` - Agent-maintained markdown knowledge vault
- `digests/` - Daily digest archives (`<topic>/YYYY-MM-DD.md`)
- `agent-state/` - Cross-reboot task persistence (`pending.md`)
- `backups/` - Local backup archives (written by homelab-backup service)
- `.dotfiles-homelab/` - Bare git repo tracking dotfiles

## Dev Workflow (`dev/`)

**Hard rule:** Always develop in `~/dev/<repo>/`. Never edit files in the prod deploy folders (`/home/carter/blog/`, `/home/carter/delta_neutral/`, `/home/carter/hub/`, etc.) — those are deployment artifacts only. If a dev/ clone doesn't exist for a repo, pull a fresh one with `git clone git@github.com:carter2099/<repo>.git ~/dev/<repo>` before making changes.

The `dev/` directory is for cloning GitHub repos (via SSH: `git@github.com:carter2099/<repo>.git`), running their test suites, making changes, and pushing back. It is **not** tracked by the dotfiles bare repo.

Typical flow:
```bash
cd ~/dev
git clone git@github.com:carter2099/<repo>.git
cd <repo>
bundle install   # or npm install, etc.
bundle exec rspec  # run tests
# make changes, commit, push
```

Note: `.ruby-version` in cloned repos may request a Ruby version not installed locally. Use `RBENV_VERSION=<installed-version>` to override for testing if the patch version difference is minor, or install the exact version with `rbenv install <version>`.

## Skills

Always use the `/create-skill` skill when creating a new user-level skill. Writing a skill file directly (under `~/.pi/agent/skills/*/SKILL.md`) skips the `dotfiles add` + commit + push step, leaving the skill untracked and at risk of being lost if homelab storage is wiped. The skill bakes in the VCS step.

## Dotfiles Management

```bash
# The 'dotfiles' alias manages the bare repo
dotfiles status
dotfiles add <file>
dotfiles commit -m "message"
dotfiles push
```

Alias defined in `.zshrc`: `dotfiles='/usr/bin/git --git-dir="$HOME/.dotfiles-homelab/" --work-tree="$HOME"'`

**⚠️ Always use targeted `dotfiles add <path>` — never bare `dotfiles add -A` or `dotfiles add .`.** Since the work-tree is `$HOME`, an unqualified `add -A` would stage everything in `/home/carter/` that isn't gitignored. Scope adds to the specific file(s) being tracked.

```bash
dotfiles add .zshrc                                 # single file
dotfiles add .config/systemd/user/homelab-backup.*  # glob pattern for related files
dotfiles add -A .pi/                                # OK when scoped to a directory path
```

## App Deployment Pattern
All apps follow the same deploy flow:
1. `release.sh` - pulls latest code, tears down containers, removes old images, calls `up.sh`
2. `up.sh` - starts Docker Compose in detached mode with production config

**Docker daemon:** apt-installed and sole (`docker.service` + `docker.socket`, data root `/var/lib/docker`, boot-enabled). `systemctl status/restart docker`, `journalctl -u docker`, `docker ps`, and `/var/run/docker.sock` all mean the obvious thing — no snap indirection. (The former snap docker was removed 2026-07-10; see `~/plans/docker-daemon-split-RUNBOOK.md` for the migration history.) `RAILS_MASTER_KEY` note: `sudo` strips env vars, so pass it through with `sudo env RAILS_MASTER_KEY=$(cat config/master.key) docker compose ...` or use the repo's `up.sh`/`release.sh` which set it inline.

Rails apps (blog, hub) pass `RAILS_MASTER_KEY` from `config/master.key` at startup.

### Commit before deploy

Always commit and push before deploying any homelab app. Never run `release.sh` (or trigger the `deploy-app` skill) while there are uncommitted changes in the app's repo, even though the docker build uses local files and would technically pick them up.

**Why:** Carter wants the deployed state to match `origin/main` exactly. If the source on disk diverges from git, the next deploy from a fresh clone (or anyone else looking at GitHub) will see the wrong code, and rollbacks via git become unreliable. Deploys must be reproducible from the remote.

**How to apply:** Before invoking the `deploy-app` skill or running `release.sh`, check `git status` in the app repo. If there are uncommitted changes related to the deploy, commit and push them first, then deploy. If there are unrelated dirty files, surface them and ask before proceeding.

### Orphaned docker-proxy / port-in-use on restart

Containers occasionally crash (exit 255, no stack trace in logs — typical of SIGKILL / OOM) and leave orphaned `docker-proxy` processes holding the host port, causing `up.sh` to fail with `address already in use`. This is also common **after a host reboot** — new `docker-proxy` PIDs come up early, but a container crash later in the session can leave them stranded.

Diagnosis:
```bash
docker ps -a                          # container shows Exited (255)
ps aux | grep docker-proxy            # look for proxy on the stuck port
sudo ss -tlnp | grep <port>           # confirm proxy is the LISTEN holder
```

Fix:
```bash
sudo kill <proxy-pid(s)>              # free the port
docker rm <container-name>            # remove the Exited container
bash up.sh                            # start fresh
```

### "Missing feature" symptom = almost always a cache hit, not a code regression

If someone sees stale content or a missing feature after deploy, **do not assume a code regression**. The likely cause is Cloudflare serving cached HTML while the origin is down (orphaned docker-proxy or Exited container). Before touching code: confirm the container is running, verify the feature exists on disk and in the image, and curl the origin. If origin is healthy, fix with a hard-refresh or CF cache purge — not a redeploy, which risks another exit-255 during restart.

### Exit 255 is a known intermittent on this host

Documented for visibility: `blog-web` and other containers on this 16GB host occasionally exit 255 without warning, no stack trace in `docker logs`. Likely causes are OOM (check `docker inspect <container> --format '{{.State.OOMKilled}}'` next time it happens) or SIGKILL from a competing deploy. Don't rebuild in response — just restart with the existing image unless there's evidence the image itself is bad.

### Never run aa-remove-unknown

Never run `sudo aa-remove-unknown` on this host. It can delete AppArmor profiles that containerd/Docker or other services depend on, causing crashloops. (Historically this braked snap Docker via the `snap.docker.dockerd` profile; that snap is now gone, but the caution stands — clearing "unknown" profiles is never safe here.) If Docker containers can't be stopped/killed due to AppArmor "permission denied" errors, fix by reloading AppArmor and restarting the relevant services, not by clearing profiles.

## Kubernetes (k3s)

```bash
k get pods          # 'k' is aliased to 'kubectl'
k get svc
k logs -n <namespace> -l app=<appname>
k delete pod <name>  # k3s auto-recreates
```

**Architecture pattern:** Two deployment models coexist:
- **Self-developed webapps** (blog, hub, tbitt, stickies, delta_neutral) run on the host in Docker Compose. K3s uses ExternalService + Endpoints to route Traefik ingress to host IPs (blog/delta_neutral at 192.168.4.92, tbitt/stickies at 192.168.4.102). Note: hub and stickies are not currently live; tbitt is deprecated.
- **Third-party services** (grafana, prometheus, node-exporter, freshrss, uptime-kuma, traefik) run natively as k3s Deployments/DaemonSets.

Each service in `k3s/` has its own directory with granular YAML manifests (deployment, service, ingress, etc.).

**k3s server config:** `/etc/rancher/k3s/config.yaml` (tracked copy: `~/k3s/config.yaml`). **Critical:** `flannel-iface` must match the active network interface. WiFi (`wlp6s0`) is disabled — flannel must use `enp3s0f0`. If k3s crashloops with `"flannel exited: failed to find the interface wlp6s0: No IPv4 address found"`, this config regressed. The node IP is `192.168.4.92` (static secondary on the wired interface).

**Pod ↔ host networking requires ufw rules.** Pods reach the API server / kube-dns by DNATing to the host's own addresses, which lands in the host `INPUT` chain. `ufw` defaults to **deny incoming**; without explicit allow-rules for the CNI interfaces, pod→host traffic is dropped (Traefik can't reach the API → loads no Ingresses → 404 on every k3s-routed hostname; metrics-server/coredns/local-path-provisioner CrashLoop). Persistent rules are in `/etc/ufw/user.rules` (`ufw allow in on cni0` + `ufw allow in on flannel.1`). **If pods suddenly can't reach ClusterIPs after a reboot/docker restart/ufw reload, check these first** — a `docker compose down/up` or ufw reset can silently drop the `INPUT` accept and recreate this failure.

## App Details

### Blog (Rails 8 + SQLite)
- Port: 3099 (internal) / 33099 (exposed)
- Markdown-based content in `app/posts/` and `app/reviews/` (git-ignored)
- Obsidian vault image syntax (`![[file.jpg]]`) converted automatically
- Has its own `AGENTS.md` with detailed architecture docs
- Ruby 3.4.3, Propshaft, Importmap, Turbo/Stimulus

### Hub (React + Rails API) - Not live
- Code remains on disk but the service is not running. Previously planned for carter2099.com.
- Client (if deployed): React 19 + TypeScript + Vite (port 3000/13000)
- Server (if deployed): Rails 8 API-only (port 3001/13001)
- Hyperliquid SDK for crypto price data

### Tbitt (React + Express) - Deprecated
- Client: React 18 + TypeScript (port 3000/13000)
- Server: Express + TypeScript + PostgreSQL (port 3001/13001)
- Deprecated Aug 2025 (Jupiter API discontinued)

### Stickies - Not live
- Previously a sticky notes app at stickiesapi.carter2099.com
- Code remains on disk but the service is not running

### Delta Neutral (Rails 8 + SQLite)
- Port: 80 (internal) / 43080 (exposed)
- Automated rebalancer for Hyperliquid short hedges on Uniswap V3 positions
- Background jobs via Solid Queue (in-process with Puma via `SOLID_QUEUE_IN_PUMA=1`)
- Env vars in `config/master.key` (credentials) + `.env.production` (API keys/SMTP)
- Required env vars: `HYPERLIQUID_PRIVATE_KEY`, `HYPERLIQUID_WALLET_ADDRESS`, `UNISWAP_SUBGRAPH_URL`, `THEGRAPH_API_KEY`
- Dockerfile requires extra build deps: `autoconf automake libtool libsecp256k1-dev libssl-dev` (for `rbsecp256k1` gem)
- Ruby 3.4.8, Thruster, Propshaft, Tailwind

### Homelab Backup (Go)
- Runs daily at 03:00 UTC via systemd user timer (`homelab-backup.service`/`.timer`)
- Backs up to Cloudflare R2 bucket (`homelab-backup`) with 14-day daily + 1 monthly + 1 yearly retention
- Targets: blog posts, reviews, images, blog SQLite DB, FreshRSS SQLite DB + config, Open WebUI `webui.db` (`/var/lib/docker/volumes/open-webui_open-webui/_data/webui.db`)
- Local archives written to `~/backups/`; R2 credentials via env vars `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY`
- Use the `backup-health` skill to check last run status, next scheduled run, and R2 bucket contents

### Dependabot Webhook (Go)
- Always-on systemd user service (`dependabot-webhook.service`) listening on `localhost:9099`
- Receives GitHub `pull_request` webhooks via Cloudflare tunnel at `hooks.carter2099.com/webhook`
- Verifies HMAC-SHA256 signature, then spawns a sandboxed **Pi agent (Qwen 3.7 Max)** to handle bundler bumps
- Agent runs with a narrow permission sandbox (`pi-sandbox.ts` + `--tools` flag) — default-deny bash floor + git/bundle/gh/rake allowlist; sudo/docker/systemctl/curl/wget/rm/release.sh/up.sh denied. Verified via 4-test battery (allow, block, tool restriction, dry run).
- 90-second coalesce window so a burst of PRs is handled in one agent run
- Source: `~/dev/dependabot-webhook/`; config (with webhook secret): `~/.config/dependabot-webhook/env`
- Logs: `journalctl --user -u dependabot-webhook -f`
- Release: `cd ~/dev/dependabot-webhook && bash release.sh`

### Hyperliquid SDK Maintenance (systemd timer)
- Runs Mon/Thu at 04:00 ET via systemd user timer (`hyperliquid-sdk.service`/`.timer`)
- Spawns `pi -p --model opencode-go/qwen3.7-max` executing the `hyperliquid-run` skill
- Script: `~/scripts/run_hyperliquid_sdk.sh`; timeout: 30 min

### Open WebUI (Homelab Chat)
- ChatGPT/Claude-style self-hosted chat UI at `https://chat.carter2099.com`. Not an agent — a general chat front-end.
- Docker Compose in `~/open-webui/` (pinned tag, currently `ghcr.io/open-webui/open-webui:v0.10.2` — the nightly update runner bumps it), bound **`127.0.0.1:48100`** (loopback-only).
- **Backend = the OpenCode Go endpoint** (`OPENAI_API_BASE_URL=https://opencode.ai/zen/go/v1`) so chat usage rides the **flat-sub session-cap billing**, NOT `zen/v1` pay-as-you-go. The 18 Go models populate automatically; a few (e.g. `qwen3.7-max`) 401 as "not supported for format oa-compat" and are opencode-native-only — just pick another. (See the Zen-vs-Go endpoint note: same account key, the base URL picks product/billing.)
- Secrets (`OPENAI_API_KEY` = the Go key, `WEBUI_SECRET_KEY`) in gitignored `~/open-webui/.env` (600). Compose + `up.sh` are tracked; `.env` is not.
- **Routing: direct-tunnel pattern** (like pi-web/dependabot, NOT Traefik) — tunnel ingress `chat.carter2099.com → http://localhost:48100`; proxied CNAME `chat` → `<tunnel-id>.cfargotunnel.com`. Loopback bind = off the LAN, only reachable via the tunnel.
- **Auth: two layers.** CF Access (edge SSO, manually configured in Zero Trust) + Open WebUI's own login (`WEBUI_AUTH=True`, `ENABLE_SIGNUP=False`).
- Manage: `cd ~/open-webui && bash up.sh` (pull + restart); `docker compose -f ~/open-webui/docker-compose.yml logs -f`.
- **Web search:** Configured in-app via Admin Settings → Web Search: engine `searxng`, query URL `http://searxng:8080/search` (these live in the webui.db config table, not env). Reaches the SearXNG container over a **shared external Docker network `homelab-chat-search`** declared in both `~/open-webui/docker-compose.yml` and `~/searxng/docker-compose.yml` so the `searxng` hostname resolves. No external API key needed. (Previous env-var approach + `open-webui_default`-only network no longer apply.)


### SearXNG (Self-hosted search backend)
- **Port:** 8080 (internal) / loopback-only (`127.0.0.1:8080`), **not exposed** to LAN or tunnel.
- **Purpose:** Metasearch backend for `rpiv-web-tools` `web_search` (pi agent + daily
  email digests). Replaces Brave Search API to eliminate per-query billing. Aggregates
  Google/Bing/DDG/etc.; JSON API at `GET /search?q=…&format=json`.
- **Docker Compose:** `~/searxng/` (`searxng/searxng:latest`). Single container, no
  Valkey (limiter disabled). `restart: unless-stopped` survives reboots.
  Attached to the `homelab-chat-search` external network so Open WebUI can resolve
  the `searxng` hostname (see Open WebUI section).
- **Config source-of-truth:** `~/searxng/settings.yml` (tracked). Runtime copy with the
  real `secret_key` lives in gitignored `~/searxng/core-config/` (generated by `up.sh`).
- **Manage:** `cd ~/searxng && bash up.sh` (pull + restart). Logs:
  `docker compose -f ~/searxng/docker-compose.yml logs -f`.
- **pi provider config:** `~/.config/rpiv-web-tools/config.json` → `"provider": "searxng"`,
  `"baseUrls": {"searxng": "http://localhost:8080"}`. Brave key retained as one-line
  rollback (`"provider": "brave"`).
- **Resource:** ~256–512 MB RAM, I/O-bound CPU, ~300 MB image.

### Cloudflare API Access
- Account-owned API token at `~/.config/cloudflare/api-token` (gitignored, 600 perms)
- Scopes: Cloudflare Tunnel:Edit (account), DNS:Edit (carter2099.com zone). **No Zero Trust / Access scope** — so Access apps/policies (the SSO gate in front of tunneled hostnames) must be configured **manually in the Zero Trust dashboard**; the API token returns 403 on `/access/apps`. To automate Access too, add "Access: Apps and Policies: Edit" (Account) to the token.
- Supporting IDs in `~/.config/cloudflare/`: `account-id`, `zone-id`, `homelab-tunnel-id`
- Env vars (`CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_ZONE_ID`, `CLOUDFLARE_HOMELAB_TUNNEL_ID`) exported from `.zshrc`

## Email Digests

Four daily HTML email digests are produced by a **deterministic 9-phase Python workflow** (`~/scripts/digest_runner.py`) that breaks the task into focused sub-prompts the local Qwen Q6 can handle reliably. The old single-prompt `pi -p` approach (one monster prompt doing research → curate → write → send in one context window) was replaced July 2026 because the local model struggled with the context load.

### Architecture

```
Phase 1: Research (3 sequential pi -p calls)  →  web_search for stories
Phase 2: Judge research (direct API)           →  filter by date/relevance/source
Phase 3: Rank URLs (Python, no LLM)            →  sort by importance, cap at top N
Phase 4: Fetch & Summarize (sequential pi -p)  →  web_fetch each article
Phase 5: Judge summaries (direct API)          →  verify accuracy/faithfulness
Phase 6: Curate (direct API)                   →  dedupe, cross-ref, rank, gaps,
                                                   update stories-in-flight
Phase 7: Write HTML (direct API)               →  fill the shared template
Phase 8: Send & Archive (Python, no LLM)       →  email via send_digest.py,
                                                   archive, write stories-in-flight
Phase 9: Summary (direct API)                  →  write .md for future dedup
```

Each phase output is saved to `~/digests/<topic>/YYYY-MM-DD/` for auditability and idempotent resume (if a phase output exists, it's skipped on re-run). Phases that need tools (web_search, web_fetch) use `pi -p`; phases that only transform structured data use direct llm-proxy API calls. All phases use the reasoning model (`qwen-3.6-35b-q6`). Calls are sequential because llama.cpp is single-request.

Digest `pi -p` calls use `--session-dir ~/.pi/agent/sessions-automated` so automated sessions don't pollute `/resume` in interactive Pi sessions. The hyperliquid SDK runner and dependabot webhook do the same. A migration script (`~/scripts/migrate-pi-sessions.py`) handles one-time cleanup of old automated sessions.

### Stories-in-flight (cross-day story tracking)

A `stories-in-flight.json` file in each digest directory tracks evolving stories across days. The Phase 6 curation agent reads it, updates stories with new developments (resetting the `last_updated` clock), and adds new evolving stories. Two Python-side rules handle pruning deterministically:
- **Auto-cool (7 days):** Stories with no updates in 7+ days → status set to "cooled" (excluded from Recent & Relevant)
- **Auto-prune (14 days):** Cooled stories with no updates in 14+ days → removed from tracker entirely

The LLM can revive cooled stories by updating `last_updated` when new developments appear.

### Schedule

All four digests run sequentially via a single systemd timer to avoid conflicts with gaming (the llm-proxy kills the LLM when gaming is detected).

| Timer | Fires (UTC) | Fires (ET) |
|---|---|---|
| `homelab-backup` | 03:00 | 11:00 PM (prev. day) |
| `update-check` | 05:00 | 1:00 AM |
| `digests-daily` | 08:00 | 4:00 AM |
| `hyperliquid-sdk` | 08:00 Mon/Thu | 4:00 AM Mon/Thu |

Service unit: `digests-daily.service` runs `~/scripts/run_all_digests.sh`, which calls `digest_runner.py` for each topic in order: ai-tech → agentic-platform → gaming → world. Total runtime: ~2.5-3 hours, done by ~7 AM ET.

The old individual timers (`ai-tech-digest`, `agentic-digest`, `gaming-digest`, `world-digest`) are **disabled**. The old per-topic bash scripts (`~/scripts/run_<topic>_digest.sh`) still exist but are not used by the new system.

### Digest topics

| Topic | Category dir | Recipients |
|---|---|---|
| AI & tech | `ai-tech/` | carter2099@pm.me |
| Agentic platforms | `agentic-platform/` | carter2099@pm.me + CC from `~/.scripts/.smtp_config` |
| Gaming | `gaming-digest/` | carter2099@pm.me |
| World / U.S. events | `world-digest/` | carter2099@pm.me |

### Key files

- `~/scripts/digest_runner.py` — the 9-phase workflow orchestrator (topic-agnostic; topic configs are defined in the `TOPICS` dict at the top)
- `~/scripts/run_all_digests.sh` — sequential wrapper that calls `digest_runner.py` for all 4 topics
- `~/scripts/send_digest.py` — SMTP sender (reads `~/.scripts/.smtp_config` for credentials)
- `~/digests/template.html` — shared HTML template with `{{DIGEST_TITLE}}`, `{{DATE}}`, `{{INTRO}}`, `{{FRESH_STORIES}}`, `{{RECENT_STORIES}}` placeholders
- `~/.config/systemd/user/digests-daily.{service,timer}` — systemd units

### Quality infrastructure

Each run writes a full phase trail (`01-research-raw.json` through `summary.md`) in the dated run directory. This enables retrospective audits — if a story was missed, trace it from research through each judgment gate to understand why.

### Debugging

```bash
# Check timer status
systemctl --user status digests-daily.timer

# Run a single topic manually (dry-run to skip email)
python3 ~/scripts/digest_runner.py ai-tech --dry-run

# Run all topics
bash ~/scripts/run_all_digests.sh

# Check the latest run's artifacts
ls ~/digests/ai-tech/$(date +%Y-%m-%d)/

# View the log
cat ~/digests/.digests.log

# Check stories-in-flight
cat ~/digests/ai-tech/stories-in-flight.json | python3 -m json.tool
```

## Homelab Update Agent

Nightly maintenance runs at **1:00 AM ET** (05:00 UTC) via `update-check.timer`. The agent is a **deterministic Python orchestrator** (`~/scripts/update_runner.py`) — zero LLM in the loop.

### Architecture

```
Phase 0: Setup (run dir, previous-summary delta detection)
Phase 1: Apply safe updates (apt upgrade, Docker engine/plugins, cloudflared, k3s restarts, open-webui stable bump)
Phase 2: Validate (docker ps, k3s pods, 5 localhost curls, tunnel reachability, LLM fallback check)
Phase 7: Auto-rollback (conditional — reverts to captured pre-versions on pi-web/tunnel failure)
Phase 3: Audit (apt upgradable, snap list, image ages, runtimes, reboot-required)
Phase 4: open-webui tag check (no-op if already bumped in Phase 1)
Phase 5: Heartbeat (failed systemd units, LLM stack health + fallback flag, backup recency, k3s node)
Phase 6: Write HTML (pure Python string templating, no LLM)
Phase 8: Send + Archive (SMTP via send_digest.py, 30-day pruning)
Phase 9: Summary (.md for next-day delta detection)
```

### Security layer

**unattended-upgrades** is installed, enabled, and runs daily — it handles `noble`, `noble-security`, and ESM pockets. This is the Ubuntu security layer. The update agent handles the non-security `noble-updates` pocket (not in unattended-upgrades allowlist) plus third-party packages (Docker, cloudflared, open-webui).

### Auto-apply scope

| Layer | Auto-apply? | Mechanism | Rollback? |
|---|---|---|---|
| Ubuntu noble-updates | ✅ yes | `apt upgrade` | n/a |
| Docker engine + plugins | ✅ yes | `apt install --only-upgrade` | ✅ capture pre-ver, downgrade on pi-web/tunnel fail |
| cloudflared | ✅ yes | `apt install --only-upgrade` + restart | ✅ capture pre-ver, downgrade on tunnel fail |
| open-webui (stable tag) | ✅ yes | GitHub releases/latest → edit compose tag → up -d | ✅ revert compose tag |
| k3s workload images | ✅ yes | rollout restart (existing) | n/a (self-healing) |
| Ubuntu security | ✅ unattended-upgrades | automatic | — |
| snap packages | ✅ snapd auto-refresh | every 6h | — |
| Manual only | ❌ no | surfaced in email | — |

### Safety rules

- Never `sudo apt dist-upgrade` — only `upgrade` / `--only-upgrade`
- Never `sudo aa-remove-unknown` — can delete load-bearing AppArmor profiles
- Docker engine lives in the apt repo; `apt install --only-upgrade docker-*` is the auto-apply path (its postinst restarts `docker.service`, the sole daemon — no `snap refresh docker` anymore, snap docker is gone).
- After a `docker-*` upgrade, assert the daemon is the expected one before declaring success: `docker info --format '{{.DockerRootDir}}'` must equal `/var/lib/docker` (guards against a second daemon creeping back in).
- Stop on first auto-apply failure — don't continue to next step
- After Docker daemon restart, verify containers came back before proceeding
- Rollback is status-code-driven, not LLM-judgment-driven: reversion fires on pi-web or tunnel unhealthy after auto-apply

### Rollback

If Phase 2 validation finds pi-web or the tunnel unhealthy AND Phase 1 auto-applied something:
1. Revert each auto-applied apt package to its captured pre-version (`--allow-downgrades`)
2. Revert open-webui compose tag edit
3. Restart Docker + cloudflared
4. Re-validate — if healthy, report "rolled back and healthy"; if still unhealthy, report "ROLLBACK FAILED" with container states + docker journal tail

SMTP is Docker-independent (`send_digest.py` talks to the mail server directly), so the failure-red email still goes out even if Docker is down.

### Manual run / debugging

```bash
# Dry run (skip mutations + email, still audit + archive)
python3 ~/scripts/update_runner.py --dry-run

# Resume from a failed run (skip phases with existing artifacts)
python3 ~/scripts/update_runner.py --resume

# Check timer status
systemctl --user status update-check.timer

# View the latest run's artifacts
ls ~/digests/updates/$(date +%Y-%m-%d)/

## Remote Agent Operations

This homelab runs an **always-on pi-web agent** accessible from any browser at `https://pi.carter2099.com`. It runs `pi-web` (installed via `npm install -g @jmfederico/pi-web`) as two systemd user services with `loginctl enable-linger` so they survive reboots. It is **intentionally full-privilege** (no command denylist, no `NoNewPrivileges`); the trust anchor is **Cloudflare Access**.

- **Services:** `pi-web-sessiond.service` (session daemon) + `pi-web.service` (web/API at `127.0.0.1:8504`). **Loopback-only bind on purpose** — the sole ingress is the CF tunnel; it is NOT reachable on the LAN (so there's no path that bypasses Cloudflare Access).
- **Access URL:** `https://pi.carter2099.com` (browser → CF Access SSO → pi-web UI). The old `opencode.carter2099.com` hostname also routes to the same service.
- **Auth:** Cloudflare Access (identity gate at the CF edge) — unauthenticated requests get a 302 to `carter2099.cloudflareaccess.com` and never reach the host. Policy is managed in the CF Zero Trust dashboard. No secondary password layer (unlike opencode-web which had `OPENCODE_SERVER_PASSWORD`).
- **Routing:** direct-tunnel pattern — tunnel ingress `pi.carter2099.com → http://localhost:8504` (cloudflared runs on the host and reaches loopback). No k3s manifest, no ExternalService/Endpoints, no Traefik hop. DNS: proxied CNAME `pi` → `<tunnel-id>.cfargotunnel.com`.
- **Config:** `~/.config/pi-web/config.json` (host, port, allowedHosts).
- **Logs:** `journalctl --user -u pi-web -f` or `pi-web logs`
- **Restart:** `systemctl --user restart pi-web pi-web-sessiond` or `pi-web restart`
### Debugging from an interactive SSH session

If pi-web is misbehaving or unreachable, an interactive agent SSH'd into the box diagnoses it. **First thing every SSH session should do** before `systemctl --user ...` commands:

```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u)   # required for systemctl --user to reach the user bus
```

Standard diagnosis sequence:

```bash
pi-web status                                            # services running?
journalctl --user -u pi-web -n 100 --no-pager            # why it failed
journalctl --user -u pi-web-sessiond -n 100 --no-pager   # sessiond logs
ls -la ~/agent-state/                                    # pending reboot context, etc
cat ~/.config/pi-web/config.json                         # config
```

### Reboot protocol

Never `sudo reboot` directly. Use the `homelab-reboot` skill, which:
1. Writes `~/agent-state/pending.md` with timestamp, reason, and a one-paragraph summary of the current in-flight work.
2. Only then issues `sudo systemctl reboot`.

This guarantees the next session has context for what happened. If the skill isn't available for some reason, do the two steps manually in that order.

### Startup check (cross-reboot continuity)

At the start of every interactive session, check `~/agent-state/pending.md`. If it exists:
1. Read it.
2. If `mtime` is within the last 30 minutes, summarize its contents to the user up front ("Last reboot was at X for reason Y; in-flight task was Z").
3. Delete the file (`rm ~/agent-state/pending.md`) once acknowledged so it doesn't re-surface next session.
4. If mtime is older than 30 min, the file is stale — surface it briefly and delete.

This is the mechanism by which tasks survive reboots. It is the *only* expectation of cross-reboot continuity.

## Persistent Memory (`~/notes/`)

The `~/notes/` vault is the homelab's long-term knowledge base — a standalone git repo of reference notes, session memoirs, and cross-referenced context.

### For agents

**Before starting work on a known topic**, grep the vault for relevant context:
```bash
rg -l "search term" ~/notes/
```
This is opt-in — only do it when past context would materially help the current task. Don't load entire files into context preemptively.

**After significant sessions**, write a brief session memoir. "Significant" means: architectural decisions, system state changes, or context a future agent would need. Routine checks and quick Q&A don't need one.

Write to `~/notes/sessions/YYYY-MM-DD.md` using this exact format:
```markdown
# Session: YYYY-MM-DD
**Topics:** comma-separated list
**Decisions:**
- decision 1
- decision 2
**State changes:**
- what was modified on the system
**Context for next time:** 1-2 sentences a future agent should know
```

Session memoirs are NOT formal notes — don't use `/note-save` or full frontmatter for them. They're quick context dumps for cross-session continuity. Formal reference notes use `/note-save` when the user explicitly asks.

### Vault structure

- `~/notes/INDEX.md` — index of all formal reference notes (maintained by `/note-save`)
- `~/notes/<topic>/` — reference notes organized by topic
- `~/notes/sessions/` — session memoirs (YYYY-MM-DD.md)
- The vault is a standalone git repo (not the dotfiles bare repo) — `/note-save` handles commits

## Gaming Rig (Windows 11)

Carter's gaming PC — a Windows 11 Home machine (`DESKTOP-KQHLUCL`, user `carte`) on the LAN.

- **IP:** `192.168.4.103` (reserved DHCP lease)
- **Host alias:** `gamingrig` — resolves via `/etc/hosts` and `~/.ssh/config`
- **SSH access:** `ssh gamingrig` (key-based auth with `~/.ssh/id_ed25519`, user `carte`)
- **SSH config** (`~/.ssh/config`): hostname, user, and identity file pre-configured
- **Windows OpenSSH:** Server installed, service set to auto-start. Uses `administrators_authorized_keys` (not the user profile path) because the `carte` account is an Administrator — the standard Windows OpenSSH quirk.
- **ICMP blocked** by Windows Firewall — ping won't work, but SSH does.

SSH from this homelab can run arbitrary PowerShell commands on the gaming rig. Use it for remote administration, file transfers, or automation tasks.

### Local LLM Server (llama-swap + llm-proxy)

The gaming rig runs **llama-swap** on top of llama.cpp's `llama-server.exe`, serving GGUF models from `C:\llm\`. The homelab runs **llm-proxy** (`~/dev/llm-proxy/`), a Go reverse proxy that handles WoL wake-on-demand, gaming-aware auto-pause, SSH lifecycle management, and transparent cloud fallback when the rig is unavailable.

- **API endpoint for clients:** `http://localhost:8081/v1` (llm-proxy on the homelab)
- **Backend (do not hit directly):** `http://192.168.4.103:8080/v1` (llama-swap on gaming rig)
- **Health check:** `curl http://localhost:8081/health`
- **Model list:** `curl http://localhost:8081/v1/models`
- **Model files:** `C:\llm\*.gguf` on the gaming rig
- **Config:** `C:\llm\config.yaml` on the gaming rig
- **Proxy config:** `~/.config/llm-proxy/env` on the homelab
- **llama.cpp build:** b9870

#### How the proxy works

```
Client → llm-proxy:8081 (homelab) → gamingrig:8080 healthy? → forward
                │                            ↓ no
                │                     gaming? → fallback to cloud (deepseek-v4-flash)
                │                            ↓ no
                │                     SSH reachable? → start llama-swap, wait 45s
                │                            ↓ no            ↓ up
                │                     send WoL → wait → start    → forward
                │                            ↓ still down
                │                     fallback to cloud
                │
                └─ Background: check encoder sessions every 10s
                   → gaming detected? kill LLM to free VRAM
                   → gaming ended? restart LLM
```

The proxy replaces three old components: `llama-server.service`, `llama-gaming-proxy.timer`, and `~/.local/bin/llama-gaming-proxy.sh`.

#### Available models

Only one model file on disk. Two server variants via `--reasoning` flag:

| Model ID | Alias | Context | Thinking | Use |
|---|---|---|---|---|
| `qwen-3.6-35b-q6` | `qwen-3.6-35b-q6` | 128K | ON (budget 1024) | **General use** — default for most tasks. Reasoning budget caps at 1024 tokens. |
| `qwen-3.6-35b-q6-fast` | `qwen-3.6-35b-q6-fast` | 128K | OFF | **Fallback** — use when reasoning eats the token budget, breaks tool calling, or causes other issues. |

Key flags: `-c 131072`, `-ctk q8_0 -ctv q8_0`, `--cache-ram 2048`, `--prio 2`, `--temp 0.5 --top-k 20 --min-p 0.1`.

#### Service management

- **Service:** `llm-proxy.service` (`~/.config/systemd/user/llm-proxy.service`)
- **Binary:** `~/.local/bin/llm-proxy`
- **Source:** `~/dev/llm-proxy/`
- **Logs:** `journalctl --user -u llm-proxy -f`
- **Restart:** `systemctl --user restart llm-proxy`
- **Deploy:** `cd ~/dev/llm-proxy && bash release.sh`

#### Cloud fallback

When `FALLBACK_API_KEY` is set in `~/.config/llm-proxy/env`, requests that can't reach the gaming rig are transparently proxied to OpenCode Go (deepseek-v4-flash). The `X-Fallback: true` response header signals when fallback was used. The proxy waits up to `STARTUP_GRACE` (45s) for the rig to wake before falling back.

| Env Var | Default | Description |
|---|---|---|
| `FALLBACK_BASE_URL` | `https://opencode.ai/zen/go` | Cloud fallback API base |
| `FALLBACK_API_KEY` | (required) | API key for fallback provider |
| `FALLBACK_MODEL` | `deepseek-v4-flash` | Model to use during fallback |

#### Troubleshooting

**Gaming rig went to sleep** — The proxy sends WoL automatically on next request. Wake + llama-swap startup takes ~30-60s. Requests block until the rig is ready (up to 45s), then serve locally. If the rig doesn't come up in time, the request falls back to cloud.

**Zone Identifier blocking execution** — If llama-swap fails with "The system cannot execute the specified program", the EXE is marked as downloaded from the internet. The proxy runs `Unblock-File` automatically on startup, but you can also fix manually:
```powershell
powershell -Command Unblock-File C:\llm\llama-swap.exe
```

**Port 8080 in use** — An orphaned llama-swap process may hold the port. The proxy kills stale processes before starting. If stuck: `ssh gamingrig "taskkill /f /im llama-swap.exe"`.

## Environment

- **Shell:** zsh with vim keybindings
- **Editor:** neovim (built from source in `build/neovim/`)
- **Ruby:** managed via rbenv
- **Node:** managed via fnm
- **Tmux prefix:** Ctrl+Space
- **Git user:** carter2099 <carter2099@pm.me>
- **GitHub CLI:** `gh` authenticated as carter2099 (HTTPS, broad scopes)
- **Client topology:** Carter develops from a Mac and SSHs into the homelab. When he mentions file paths like `/Users/carterbrown/...`, those are on his Mac and **not reachable** from this session. Don't try to read Mac paths directly — they'll 404. For screenshots or files on his Mac, suggest `scp`-ing to the homelab first, or ask him to describe the content in words. Everything under `/home/carter/` is local and readable.
