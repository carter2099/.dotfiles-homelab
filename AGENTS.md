# AGENTS.md

This file provides guidance to omp agents when working on this homelab.

**Maintenance:** Keep this file up to date. When deploying a new app, adding a service, changing ports/IPs, or making any structural changes to the homelab, update the relevant sections here as part of that work. Deep-dive architecture for some subsystems lives in `~/notes/homelab/` (see "Where the deep dives live" at the bottom) — keep AGENTS.md as the always-loaded operational reference and update the relevant note when those subsystems change.

## Working principles (Endler tenets)

Carter endorses the tenets in [The Best Programmers](https://endler.dev/2025/best-programmers/). The subset below is the part that applies directly to an LLM assistant and should shape every session.

- **Read the reference.** Prefer official docs (local or web), man pages, and the actual source over recall from training data. When something in this repo is in question, read the file. Training-data recall about APIs, flags, or versions is frequently stale — verify.
- **Read the error message.** Parse errors fully before reacting. The message usually names the cause; skimming past it and guessing wastes Carter's time.
- **Don't guess.** If a fact is load-bearing for the answer or action, verify it with a tool (grep, read, `--help`, a quick script) rather than asserting from memory. This is the single most important one.
- **Say "I don't know."** Uncertainty is fine and useful; confident bullshit is not. If a recommendation rests on something unverified, say so explicitly rather than smoothing it over.
- **Never blame the computer.** "Flaky test," "weird cache," "probably a transient issue" are hypotheses, not conclusions. Bugs have causes — keep investigating until the cause is named, even if the fix is a retry.
- **Keep it simple.** Prefer the smallest change that solves the problem. This reinforces the existing "no gratuitous abstractions / no speculative features" guidance further down in this file.
- **Have patience.** Don't rush to a conclusion or a fix. Re-read, re-check, confirm before acting — especially for anything irreversible.

## Scope

Carter wants this agent framed as a **homelab assistant and general personal assistant**, not narrowly as a coding tool. Software engineering is a large part of the work, but non-code help (planning, notes, research, life admin, digests, correspondence drafting, scheduling) is equally in scope and should be treated as first-class. The same tenets about rigor, not-guessing, and admitting uncertainty apply regardless of domain.

## Overview

Single-node homelab running on Ubuntu Server (ThinkPad L14 Gen 3, AMD Ryzen 5 PRO 5675U, 16GB RAM, 500GB NVMe SSD). A k3s Kubernetes cluster routes traffic via Traefik ingress to apps running in Docker Compose on the host machine. The server uses wired ethernet (`enp3s0f0`) as its primary uplink, with static secondary IPs `192.168.4.92` (k3s node IP; blog + delta_neutral ingress) — all on the same physical interface. WiFi (`wlp6s0`) is disabled.

## Hardware

| Component | Details |
|---|---|
| **Model** | ThinkPad L14 Gen 3 (AMD) |
| **CPU** | AMD Ryzen 5 PRO 5675U (6C/12T, 2.3–4.3GHz) |
| **RAM** | 16GB DDR4-3200 (2x SO-DIMM slots, dual-channel) |
| **Storage** | 500GB NVMe M.2 2242 SSD (PCIe 3.0 x4) |
| **Network** | Gigabit Ethernet (Realtek RTL8111HN/EPV), Wi-Fi 6E, Bluetooth 5.1 |

**Notes:**
- The wired NIC `enp3s0f0` (Realtek) is the **primary uplink**; WiFi (`wlp6s0`) is down by default.
- **Network config:** `/etc/netplan/50-cloud-init.yaml` (systemd-networkd). `enp3s0f0` carries `.100` (DHCP, default-route source) and `.92` (k3s node IP + blog/delta_neutral ingress). Run `ip -4 addr show enp3s0f0` for live state.

## Repository Structure

This is the home directory, managed as a bare git repo for dotfiles:
- `blog/` - Rails 8 blog app (blog.carter2099.com). Deploy wrapper at `~/blog/`; app nested at `~/blog/blog/`.
- `delta_neutral/` - Rails 8 Hyperliquid rebalancer (deltaneutral.carter2099.com). Deploy wrapper at `~/delta_neutral/`; app nested at `~/delta_neutral/delta_neutral/`.
- `homelab-backup/` - Go backup service source (daily R2 backups of blog content, DBs, FreshRSS). Deployed in place at `~/homelab-backup/`; dev clone at `~/dev/homelab-backup/`.
- `dev/dependabot-webhook/` - Go webhook receiver for automated dependabot PR handling
- `k3s/` - Kubernetes manifests organized by service
- `ddns/` - Cloudflare DDNS updater for WireGuard endpoint
- `build/` - Source builds (neovim)
- `dev/` - Scratch space for cloning GitHub repos, running tests, and doing development work
- `scripts/` - Digest + steward orchestrators (`digest_runner.py`, `steward_runner.py`, `run_all_digests.sh`, `send_digest.py`)
- `notes/` - Agent-maintained markdown knowledge vault (standalone git repo)
- `digests/` - Daily digest archives (`<topic>/YYYY-MM-DD/`)
- `agent-state/` - Cross-reboot task persistence (`pending.md`)
- `backups/` - Local backup archives (written by homelab-backup service)
- `ideas/` - Random ideas, sparks, and half-baked thoughts (plain markdown)
- `.dotfiles-homelab/` - Bare git repo tracking dotfiles

## Dev Workflow (`dev/`)

**Hard rule:** Always develop in `~/dev/<repo>/`. Never edit files in the prod deploy folders (`/home/carter/blog/`, `/home/carter/delta_neutral/`, etc.) — those are deployment artifacts only. If a dev/ clone doesn't exist for a repo, pull a fresh one with `git clone git@github.com:carter2099/<repo>.git ~/dev/<repo>` before making changes.

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

Note: `.ruby-version` in cloned repos may request a Ruby not installed locally. Check `rbenv versions`; use `RBENV_VERSION=<installed-version>` to override for testing if the patch difference is minor, or `rbenv install <version>` for the exact one.

## Skills

Always use the `/create-skill` skill when creating a new user-level skill. Writing a skill file directly (under `~/.omp/agent/skills/*/SKILL.md`) skips the `dotfiles add` + commit + push step, leaving the skill untracked and at risk of being lost if homelab storage is wiped. The skill bakes in the VCS step.

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
dotfiles add -A .omp/agent/skills/                   # OK when scoped to a directory path
```

## App Deployment Pattern
All apps follow the same deploy flow:
1. `release.sh` - pulls latest code, tears down containers, removes old images, calls `up.sh`
2. `up.sh` - starts Docker Compose in detached mode with production config

**Docker daemon:** apt-installed and sole (`docker.service` + `docker.socket`, data root `/var/lib/docker`, boot-enabled). `systemctl status/restart docker`, `journalctl -u docker`, `docker ps`, and `/var/run/docker.sock` all mean the obvious thing — no snap indirection. (The former snap docker was removed 2026-07-10; see `~/plans/done/docker-daemon-split-RUNBOOK.md` for the migration history.) `RAILS_MASTER_KEY` note: `sudo` strips env vars, so pass it through with `sudo env RAILS_MASTER_KEY=$(cat config/master.key) docker compose ...` or use the repo's `up.sh`/`release.sh` which set it inline.

Rails apps (blog, delta_neutral) pass `RAILS_MASTER_KEY` from `config/master.key` at startup.

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

`k` is aliased to `kubectl`; `KUBECONFIG=~/.kube/config` is exported in `.zshrc`.

```bash
k get pods
k get svc
k logs -n <namespace> -l app=<appname>
k delete pod <name>  # k3s auto-recreates
```

**Architecture pattern:** Two deployment models coexist:
- **Self-developed webapps** (blog, delta_neutral) run on the host in Docker Compose. K3s uses ExternalService + Endpoints to route Traefik ingress to host IPs (blog/delta_neutral at 192.168.4.92).
- **Third-party services** (freshrss, traefik) run natively as k3s Deployments/DaemonSets. (uptime-kuma was removed 2026-07-20 — the steward replaces it.) Manifests for grafana, prometheus, and node-exporter exist in `k3s/` but are **not currently deployed** (no pods/deployments) — ignore unless re-deploying.

Each service in `k3s/` has its own directory with granular YAML manifests (deployment, service, ingress, etc.). Live state: `k get nodes` / `k3s --version`.

**k3s server config:** `/etc/rancher/k3s/config.yaml` (tracked copy: `~/k3s/config.yaml`). **Critical:** `flannel-iface` must match the active network interface. WiFi (`wlp6s0`) is disabled — flannel must use `enp3s0f0` (verified current). If k3s crashloops with `"flannel exited: failed to find the interface wlp6s0: No IPv4 address found"`, this config regressed. The node IP is `192.168.4.92`.

**Pod ↔ host networking requires ufw rules.** Pods reach the API server / kube-dns by DNATing to the host's own addresses, which lands in the host `INPUT` chain. `ufw` defaults to **deny incoming**; without explicit allow-rules for the CNI interfaces, pod→host traffic is dropped (Traefik can't reach the API → loads no Ingresses → 404 on every k3s-routed hostname; metrics-server/coredns/local-path-provisioner CrashLoop). Persistent rules are in `/etc/ufw/user.rules` (`ufw allow in on cni0` + `ufw allow in on flannel.1` — both verified present). **If pods suddenly can't reach ClusterIPs after a reboot/docker restart/ufw reload, check these first** — a `docker compose down/up` or ufw reset can silently drop the `INPUT` accept and recreate this failure.

## App Details

### Blog (Rails 8 + SQLite)
- Deploy dir `~/blog/` wraps `release.sh`/`up.sh`; the app is nested at **`~/blog/blog/`** (own `AGENTS.md` + `CLAUDE.md`). Content in `app/posts/` + `app/reviews/` (git-ignored).
- Rails 8 + SQLite (Propshaft/Importmap/Turbo). Port 3099 internal / 33099 exposed; live container name via `docker ps` (compose v2 appends a `-1` suffix).
- Obsidian vault image syntax (`![[file.jpg]]`) auto-converted.

### Delta Neutral (Rails 8 + SQLite)
- Deploy dir `~/delta_neutral/` wraps `release.sh`/`up.sh`; the app is nested at **`~/delta_neutral/delta_neutral/`** (own `AGENTS.md` + `CLAUDE.md`).
- Rails 8 + SQLite (Thruster/Propshaft/Tailwind). Port 80 internal / 43080 exposed; live container name via `docker ps`.
- Automated rebalancer for Hyperliquid short hedges on Uniswap V3 positions; Solid Queue in-process with Puma (`SOLID_QUEUE_IN_PUMA=1`).
- Secrets in `config/master.key` + `.env.production` (API keys/SMTP — see the file for the required list).
- Dockerfile needs extra build deps for `rbsecp256k1`: `autoconf automake libtool libsecp256k1-dev libssl-dev`.

### Homelab Backup (Go)
- Go service at `~/homelab-backup/`; daily 03:00 UTC via systemd user timer `homelab-backup.{service,.timer}`. Dest: Cloudflare R2 bucket `homelab-backup`; local archives in `~/backups/`. Source: `~/dev/homelab-backup/`.
- 23 targets in `~/homelab-backup/config.yaml`: app content + DBs, FreshRSS, Open WebUI (db only), k3s/backup config, **secrets** (rails master.keys, open-webui/.env, cloudflare/dependabot/llm-proxy/searxng envs, smtp config — **unencrypted in R2 by design**), host `/etc` (netplan/k3s/ufw via `ExecStartPre=pre-collect.sh` + sudo), and a package manifest. R2 creds via `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` (`~/homelab-backup/.env`, not in the bucket).
- `OnFailure=homelab-backup-notify.service` **emails Carter the journal tail on failure** (failure-only; SMTP via `~/scripts/send_digest.py`).
- Subcommands: `run [--local-only]`, `list` (R2 objects — no aws CLI on host), `verify <archive>` (integrity_check every DB), `latest <dest>` (newest R2 download). Restore playbook: `~/homelab-backup/RESTORE.md`.
- **Monthly restore drill:** `homelab-backup-restore-drill.timer` (1st, 12:00 UTC) downloads the newest R2 backup, runs `verify`, emails PASS/FAIL.
- Use the `backup-health` skill for last-run status, next run, and R2 listing. **Full architecture (target taxonomy, pre-collection, retention, subcommands, drill, debug) lives in [`~/notes/homelab/homelab-backup.md`](notes/homelab/homelab-backup.md).**

### Dependabot Webhook (Go)
- Always-on systemd user service (`dependabot-webhook.service`) listening on `localhost:9099`
- Receives GitHub `pull_request` webhooks via Cloudflare tunnel at `hooks.carter2099.com/webhook`
- Verifies HMAC-SHA256 signature, then spawns a sandboxed **omp agent (DeepSeek v4 Pro)** to handle bundler bumps
- Agent runs with a narrow permission sandbox (`omp-sandbox.ts` + `--tools` flag) — default-deny bash floor + git/bundle/gh/rake allowlist; sudo/docker/systemctl/curl/wget/rm/release.sh/up.sh denied. Verified via 4-test battery (allow, block, tool restriction, dry run).
- 5-minute coalesce window so a burst of PRs is handled in one agent run
- Source: `~/dev/dependabot-webhook/`; config (with webhook secret): `~/.config/dependabot-webhook/env`
- Logs: `journalctl --user -u dependabot-webhook -f`
- Release: `cd ~/dev/dependabot-webhook && bash release.sh`

### Hyperliquid SDK Maintenance (systemd timer)
- Runs Mon/Thu at 4:00 AM ET via systemd user timer (`hyperliquid-sdk.service`/`.timer`, `OnCalendar` uses `America/New_York` so it shifts with DST: 08:00 UTC summer / 09:00 UTC winter — one of two timers that are ET-locked rather than UTC-locked; the other is `homelab-steward` at 5:00 PM ET)
- Spawns `omp -p --model opencode-go/glm-5.2` executing the `hyperliquid-run` skill
- Script: `~/scripts/run_hyperliquid_sdk.sh`; timeout: 30 min

### Open WebUI (Homelab Chat)
- ChatGPT-style self-hosted chat UI at `https://chat.carter2099.com`. Not an agent — a general chat front-end.
- Docker Compose in `~/open-webui/` (pinned tag — the nightly update runner bumps it; see the compose file), bound **`127.0.0.1:48100`** (loopback-only).
- **Backend = the OpenCode Go endpoint** (compose sets `OPENAI_API_BASE_URL=https://opencode.ai/zen/go/v1`, but the webui.db override below is what actually takes effect) so chat usage rides the **flat-sub session-cap billing**, NOT `zen/v1` pay-as-you-go. The 18 Go models populate automatically; a few (e.g. `qwen3.7-max`) 401 as "not supported for format oa-compat" and are opencode-native-only — just pick another. (Same account key, the base URL picks product/billing.)
- Secrets (`OPENAI_API_KEY` = the Go key, `WEBUI_SECRET_KEY`) in gitignored `~/open-webui/.env` (600). Compose + `up.sh` are tracked; `.env` is not.
- **Routing: direct-tunnel pattern** (like dependabot, NOT Traefik) — tunnel ingress `chat.carter2099.com → http://localhost:48100`; proxied CNAME `chat` → `<tunnel-id>.cfargotunnel.com`. Loopback bind = off the LAN, only reachable via the tunnel.
- **Auth: two layers.** CF Access (edge SSO, manually configured in Zero Trust) + Open WebUI's own login (`WEBUI_AUTH=True`, `ENABLE_SIGNUP=False`).
- Manage: `cd ~/open-webui && bash up.sh` (pull + restart); `docker compose -f ~/open-webui/docker-compose.yml logs -f`.
- **Web search:** Configured in-app via Admin Settings → Web Search: engine `searxng`, query URL `http://searxng:8080/search` (lives in the webui.db config table, not env). Reaches the SearXNG container over a **shared external Docker network `homelab-chat-search`** declared in both `~/open-webui/docker-compose.yml` and `~/searxng/docker-compose.yml` so the `searxng` hostname resolves. No external API key needed.


### OMP Web (Pi Web fork)
- **What:** Web UI for browsing omp sessions, chatting with the agent in-browser, and managing models/skills.
- **Fork:** `carter2099/omp-web` (English) — upstream is `best-linux-code/pi-web` (Chinese, an omp-native fork by agegr of the original pi-web).
- **Deploy dir:** `~/dev/pi-web/` (clone of `carter2099/omp-web` on GitHub).
- **Service:** `pi-web.service` systemd user service on `127.0.0.1:30141` (loopback-only).
- **Tunnel ingress:** `omp.carter2099.com → http://localhost:30141`, Cloudflare Access SSO fronted (same pattern as `chat.carter2099.com`).
- **Build:** `cd ~/dev/pi-web && bun run build` (production build via `bun --bun ./node_modules/next/dist/bin/next build --webpack` — Next.js 16 defaults to Turbopack, so `--webpack` is required).
- **Dev:** `cd ~/dev/pi-web && bun run dev` (serves on port 30141).
- **Logs/restart:** `journalctl --user -u pi-web -f` · `systemctl --user restart pi-web`
- **Reads:** `~/.omp/agent/sessions/` and `~/.omp/agent/models.yml` natively via the `@oh-my-pi/*` SDK.
- **Upstream sync:** `git remote add upstream git@github.com:best-linux-code/pi-web.git` then `git pull upstream main`.

### SearXNG (Self-hosted search backend)
- **Port:** 8080 (internal) / loopback-only (`127.0.0.1:8080`), **not exposed** to LAN or tunnel.
- **Purpose:** Metasearch backend for `rpiv-web-tools` `web_search` (omp agent + daily email digests). Replaces Brave Search API to eliminate per-query billing. Aggregates Google/Bing/DDG/etc.; JSON API at `GET /search?q=…&format=json`.
- **Docker Compose:** `~/searxng/` (`searxng/searxng:latest`). Single container, no Valkey (limiter disabled). `restart: unless-stopped` survives reboots. Attached to `homelab-chat-search` external network so Open WebUI can resolve `searxng`.
- **Config source-of-truth:** `~/searxng/settings.yml` (tracked). Runtime copy with the real `secret_key` lives in gitignored `~/searxng/core-config/` (generated by `up.sh`).
- **Manage:** `cd ~/searxng && bash up.sh` (pull + restart). Logs: `docker compose -f ~/searxng/docker-compose.yml logs -f`.
- **omp provider config:** `~/.config/rpiv-web-tools/config.json` → `"provider": "searxng"`, `"baseUrls": {"searxng": "http://localhost:8080"}`. Brave key retained as one-line rollback (`"provider": "brave"`).

### Cloudflare API Access
- Account-owned API token at `~/.config/cloudflare/api-token` (gitignored, 600 perms)
- Scopes: Cloudflare Tunnel:Edit (account), DNS:Edit (carter2099.com zone). **No Zero Trust / Access scope** — so Access apps/policies (the SSO gate in front of tunneled hostnames) must be configured **manually in the Zero Trust dashboard**; the API token returns 403 on `/access/apps` (verified). To automate Access too, add "Access: Apps and Policies: Edit" (Account) to the token.
- Supporting IDs in `~/.config/cloudflare/`: `account-id`, `zone-id`, `homelab-tunnel-id`
- **Tunnel ingress inventory** (live, updated 2026-07-22): `hooks`, `chat`, `deltaneutral`, `freshrss`, `blog`, `omp`, `ssh` + catch-all 404 (claimed; the catch-all cannot be verified from the host without CF Zero Trust dashboard access). Stale entries (grafana, prometheus, uptime, apex, railsapi, pi, paseo) were removed from both the tunnel config and zone DNS. `ssh.carter2099.com → ssh://localhost:22` provides SSH-over-tunnel (via `cloudflared access ssh` / CF Access) — kept and documented intentionally. `omp.carter2099.com → http://localhost:30141` (omp-web, loopback-only, Access SSO fronted).
- Env vars (`CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_ZONE_ID`, `CLOUDFLARE_HOMELAB_TUNNEL_ID`) exported from `.zshrc`

## Email Digests

Five daily HTML email digests produced by a **deterministic 9-phase Python workflow** (`~/scripts/digest_runner.py`). **Architecture, stories-in-flight mechanics, and debugging details live in [`~/notes/homelab/email-digests.md`](notes/homelab/email-digests.md)** — read that note when working on the digest system.

### Schedule

All five digests run sequentially via a single systemd timer to avoid conflicts with gaming (the llm-proxy kills the LLM when gaming is detected).

| Timer | Fires (UTC) | Fires (ET) |
|---|---|---|
| `homelab-backup` | 03:00 | 11:00 PM (prev. day) |
| `homelab-backup-restore-drill` | 12:00 (1st of month) | 8:00 AM (1st of month) |
| `homelab-steward` | 21:00 (summer) / 22:00 (winter) | 5:00 PM |
| `homelab-steward-resume` | on-boot (2 min delay) | — |
| `digests-daily` | 08:00 | 4:00 AM |
`digests-daily.service` runs `~/scripts/run_all_digests.sh`, which calls `digest_runner.py` per topic in order: **ai-tech → agentic-platform → ai-hardware → gaming → world**. Total runtime ~3–3.5 hours, done by ~7:30 AM ET. The old per-topic timers and `run_<topic>_digest.sh` scripts are **disabled/unused**.

### Topics

| Topic | Category dir | Recipients |
|---|---|---|
| AI & tech | `ai-tech/` | carter2099@pm.me |
| Agentic platforms | `agentic-platform/` | carter2099@pm.me + CC from `~/scripts/.smtp_config` |
| AI hardware | `ai-hardware/` | carter2099@pm.me |
| Gaming | `gaming-digest/` | carter2099@pm.me |
| World / U.S. events | `world-digest/` | carter2099@pm.me |

### Key files

- `~/scripts/digest_runner.py` — 9-phase orchestrator (topic configs in `TOPICS` dict)
- `~/scripts/run_all_digests.sh` — sequential wrapper for all 5 topics
- `~/scripts/send_digest.py` — SMTP sender (reads `~/scripts/.smtp_config`)
- `~/digests/template.html` — shared HTML template
- `~/.config/systemd/user/digests-daily.{service,timer}` — systemd units

```bash
systemctl --user status digests-daily.timer
python3 ~/scripts/digest_runner.py ai-tech --dry-run    # single topic, skip email
bash ~/scripts/run_all_digests.sh                     # run all topics
```

## Homelab Steward

Daily maintenance at **5:00 PM ET** (21:00 UTC summer / 22:00 UTC winter) via `homelab-steward.timer`. The steward is a 9-phase deterministic Python orchestrator (`~/scripts/steward_runner.py`) replacing both `update-check` and `agents-md-audit`. LLM agents run as native `omp -p` subprocesses. **Model split:** most phases use `STEWARD_MODEL=opencode-go/deepseek-v4-flash`; the P6 plan executor uses `EXECUTOR_MODEL=opencode-go/deepseek-v4-pro`; dotfiles commit/judge use `SMALL_MODEL=opencode-go/deepseek-v4-flash`. Every agent step is reviewed by an independent judge pass, evidence-grounded. **Full architecture, phase details, and debugging live in [`~/notes/homelab/homelab-steward.md`](notes/homelab/homelab-steward.md)** — read that note when working on the steward.

**Phases (P0–P9):** P0 setup (usage report via proxy health, stop dependabot-webhook to prevent racing, prev-summary delta) · P1 update apply (apt, Docker, cloudflared, k3s, open-webui bump) · P2 validate (docker ps, k3s pods, endpoint curls) · P3 troubleshoot (if an endpoint is unhealthy after P1: spawn omp diagnostic agent to fix forward) · P4 heartbeat (failed units, LLM stack, backup recency, disk, TLS, ddns) · P5 work queue (ideas/plans scan, consistency checks, pick next plan) · P6 executor (≤1 approved plan/night via native omp agent, post-impl review, monthly cap) · P7 audit (7 nightly sections, collector→delta-gate→worker+judge omp pairs) · P7b auto-fix (per-section omp fix agent for confirmed findings, judge-reviewed) · P8 render+send (structured data → HTML → `send_digest.py`) · P9 archive (summary.md, `.runs.jsonl`, restart dependabot).

**Restart handling:** If a kernel update (`/var/run/reboot-required`) is detected after P3, the steward writes `~/agent-state/pending.md` with run context and reboots. On boot, `homelab-steward-resume.service` checks `pending.md` and calls `steward_runner.py --resume` to continue from where it left off.
### Safety rules

- Never `sudo apt dist-upgrade` — only `upgrade` / `--only-upgrade`
- Never `sudo aa-remove-unknown` — can delete load-bearing AppArmor profiles
- Docker engine lives in the apt repo; `apt install --only-upgrade docker-*` is the auto-apply path (its postinst restarts `docker.service`, the sole daemon — no `snap refresh docker` anymore, snap docker is gone).
- After a `docker-*` upgrade, assert the daemon is the expected one before declaring success: `docker info --format '{{.DockerRootDir}}'` must equal `/var/lib/docker` (guards against a second daemon creeping back in).
- Stop on first auto-apply failure — don't continue to next step
- After Docker daemon restart, verify containers came back before proceeding
- P3 troubleshooting agent runs after P1 auto-apply if an endpoint is unhealthy; it tries to fix forward rather than rolling back. If it can't fix, the failure is reported — Carter must intervene. SMTP is Docker-independent (`send_digest.py` talks to the mail server directly), so the failure email still goes out even if Docker is down.

```bash
python3 ~/scripts/steward_runner.py --dry-run      # skip mutations + email, still audit + archive
python3 ~/scripts/steward_runner.py --resume      # resume (skip phases w/ existing artifacts)
systemctl --user status homelab-steward.timer
ls ~/digests/steward/$(date +%Y-%m-%d)/            # latest run artifacts
```

## Agent CLI: omp

The sole agent CLI on this host is **omp** (`@oh-my-pi/pi-coding-agent`, installed via bun). The legacy `pi` agent and pi-web have been removed — all automated and interactive agent sessions use omp.

| | omp |
|---|---|
| **Package** | `@oh-my-pi/pi-coding-agent` |
| **Binary** | `omp` (at `~/.bun/bin/omp`) |
| **Config dir** | `~/.omp/agent/` |
| **Skills** | `~/.omp/agent/skills/` (user-level, tracked in dotfiles) |
| **Extension API** | `@oh-my-pi/pi-coding-agent` ExtensionAPI (same import path as pi) |

### What uses omp

| System | Invocation | Notes |
|---|---|---|
| **Steward** (`steward_runner.py`) | `omp -p` | Headless subprocess |
| **Digests** (`digest_runner.py`) | `omp -p` | Headless with `@file` prompt loading |
| **Hyperliquid SDK** (`run_hyperliquid_sdk.sh`) | `omp -p` | Headless on systemd timer |
| **Dependabot webhook** | `omp -p -e omp-sandbox.ts` | Sandboxed via extension |
| **Interactive sessions** | `omp` | SSH into the homelab; run `omp` directly |

### Auth & models

omp shares the same model providers (opencode-go-proxy, llm-proxy). Provider config lives in `~/.omp/agent/models.yml` (providers + local model definitions) and `~/.omp/agent/config.yml` (model roles, extensions). Auth for opencode-go is `--api-key proxy` (the opencode-go-proxy on localhost:8082 owns the real keys; clients send the placeholder `proxy`).

### Headless config overlay (`headless-override.yml`)

Every headless `omp -p` invocation (steward, digests, hyperliquid SDK, dependabot) passes `--config ~/.omp/agent/headless-override.yml`, which deep-merges over the global config. This decouples automated-agent settings from interactive sessions. The overlay currently pins:
- `advisor.enabled: true` — interactive `/advisor off` does not disable it for scheduled runs
- `advisor.syncBacklog: off` — advisor never blocks the primary in batch processing

## Remote Agent Operations

**Remote access is via SSH + omp.** Carter uses Termius (iOS) + SSH to connect to the homelab and runs `omp` interactively. The previous web-based agents (pi-web, Paseo) have been removed.

- **SSH access:** `ssh carter@<host>` — key auth (`~/.ssh/id_ed25519`). Also available via CF tunnel at `ssh.carter2099.com` (SSH-over-tunnel through `cloudflared access ssh` / CF Access).
- **Interactive omp:** once SSH'd in, run `omp` to start an interactive agent session in the current directory. Headless mode: `omp -p "prompt"`.
- **XDG_RUNTIME_DIR:** before any `systemctl --user ...` commands, set `export XDG_RUNTIME_DIR=/run/user/$(id -u)` (required for systemctl --user to reach the user bus).

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

Carter's gaming PC — a Windows 11 Home machine (`DESKTOP-KQHLUCL`, user `carte`) on the LAN. Also hosts the **local LLM stack** (below).

- **IP:** `192.168.4.103` (reserved DHCP lease)
- **Host alias:** `gamingrig` — resolves via `/etc/hosts` and `~/.ssh/config`
- **SSH:** `ssh gamingrig` (key auth `~/.ssh/id_ed25519`, user `carte`)
- **Windows OpenSSH:** Server installed, auto-start. Uses `administrators_authorized_keys` (not the user profile path) because `carte` is an Administrator — the standard Windows OpenSSH quirk.
- **ICMP blocked** by Windows Firewall — ping won't work, but SSH does.

SSH from this homelab can run arbitrary PowerShell commands on the gaming rig.

### Local LLM Server (llama-swap + llm-proxy)

The gaming rig runs **llama-swap** over llama.cpp's `llama-server.exe`, serving GGUF models from `C:\llm\`. The homelab runs **llm-proxy** (`~/dev/llm-proxy/`), a Go reverse proxy handling WoL wake-on-demand, gaming-aware auto-pause, SSH lifecycle management, and transparent cloud fallback. **Full operational runbook (topology, models, env vars, service management, troubleshooting) lives in [`~/notes/homelab/local-llm-gaming-rig.md`](notes/homelab/local-llm-gaming-rig.md)** — read that note when working on the LLM stack.

Quick reference (verified 2026-07-21):
- **Client endpoint:** `http://localhost:8081/v1` (homelab proxy) · **Backend (don't hit directly):** `http://192.168.4.103:8080/v1`
- **Models:** `curl http://localhost:8081/v1/models` → 6 GGUF models: `qwen-3.6-35b-q5` (general reasoning), `ornith-1.0-35b-q5` (agentic coding), `ornith-1.0-9b-q6` (Ornith 1.0 9B reasoning), `qwythos-9b-v2-q6` (Qwythos v2 reasoning), `qwen-3.5-4b-q8` (fast+reasoning), `agents-a1-4b-q8` (agentic tasks). All served via llama-swap. Model files on `C:\llm\`.
- **Service:** `llm-proxy.service` · binary `~/.local/bin/llm-proxy` · source `~/dev/llm-proxy/` · config `~/.config/llm-proxy/env`
- **Logs/restart/deploy:** `journalctl --user -u llm-proxy -f` · `systemctl --user restart llm-proxy` · `cd ~/dev/llm-proxy && bash release.sh`
- **Cloud fallback:** requests that can't reach the rig proxy to OpenCode Go (`deepseek-v4-flash`); `X-Fallback: true` response header signals it. Proxy waits up to `STARTUP_GRACE` (45s) for WoL wake before falling back.

### OpenCode Go Proxy (opencode-go-proxy)

**omp's `opencode-go/*` models route through this, not directly to opencode.ai.** If opencode-go models fail, check this service first.

- **What:** An always-on local reverse proxy on `0.0.0.0:8082` that owns **two** OpenCode Go subscriptions and routes each request to the account with more headroom (proactive 60s cookie scrape of `/go`+`/billing` + reactive `cost`-field demote on each 200). Sticky+hysteresis(8pt) among `go_free`, round-robin on PAYG ($25 cap each), 401 self-healing cooldown, cookie-stale email alert. Path-transparent to `https://opencode.ai/zen/go`; injects the real per-account key (clients send a placeholder). Binds `0.0.0.0` (not loopback) so Docker containers (Open WebUI) can reach it via `host.docker.internal:8082`, but **ufw gates 8082 to the docker bridges only** — the LAN still can't reach it (default deny), same posture as the sibling `llm-proxy` on `:8081`. omp reaches it as `localhost:8082` (loopback, unaffected).
- **Agent config:** `~/.omp/agent/models.yml` → `providers.opencode-go.baseUrl = http://localhost:8082/v1`; omp uses `--api-key proxy` flag (the proxy owns both real API keys; clients send the placeholder `"proxy"`). **rollback = revert those fields.**
- **Open WebUI config:** Admin Settings → Connections → OpenAI API: Base URL `http://host.docker.internal:8082/v1`, key `proxy`. Stored in the `webui.db` config table (keys `openai.api_base_urls` / `openai.api_keys`, index 0), which **overrides** the compose `OPENAI_API_BASE_URL` env. The container reaches the host via `extra_hosts: host.docker.internal:host-gateway` (already in compose for SearXNG).
- **ufw (load-bearing for containers):** the container's traffic arrives on its docker-**bridge interface** (`br-<network id>`, e.g. `br-52cf29032cfb` = `homelab-chat-search`), NOT `docker0`, and NOT from `172.23`. Allow rules must match the real bridge: `sudo ufw allow in on br-<id> to any port 8082 proto tcp`. Same rule on `:8081` restores `llm-proxy` for Open WebUI (its local-qwen connection). If Open WebUI hangs on a host service, check `sudo ufw status` for the container's actual bridge before anything else — a `docker compose down/up` or network recreate silently lands containers on a new bridge the rules don't cover (same class as the k3s `cni0`/`flannel.1` rules).
- **Service:** `opencode-go-proxy.service` (systemd user, enabled) · binary `~/.local/bin/opencode-go-proxy` · source `~/dev/opencode-go-proxy/` (repo `carter2099/opencode-go-proxy`) · config `~/.config/opencode-go-proxy/config.json` (600, gitignored — contains real API keys + auth cookies).
- **Health:** `curl http://localhost:8082/health` → JSON with per-account tier, rolling/weekly/monthly usage, PAYG balance, last_cost, cookie_fresh.
- **Logs/restart/deploy:** `journalctl --user -u opencode-go-proxy -f` · `systemctl --user restart opencode-go-proxy` · `cd ~/dev/opencode-go-proxy && bash release.sh` (build → install binary+unit → start). Tests: `cd ~/dev/opencode-go-proxy && go test ./...` (no network).
- **Cookie expiry:** scrape sets `cookie_fresh:false` + `last_error`; one alert email per fresh→stale transition (re-alert ≤24h). Fix: re-grab that account's `auth` cookie on opencode.ai, update `auth_cookie` in `config.json`, restart. Cookies are **separate from** API keys — they authenticate the *dashboard scrape*, not the API calls.

## Environment

- **Shell:** zsh with vim keybindings
- **Editor:** neovim (built from source in `build/neovim/`)
- **Ruby:** managed via rbenv (`rbenv versions`)
- **Node:** managed via fnm (`node -v`)
- **Tmux prefix:** Ctrl+Space
- **Git user:** carter2099 <carter2099@pm.me>
- **GitHub CLI:** `gh` authenticated as carter2099 (HTTPS, broad scopes)
- **Client topology:** Carter develops from a Mac and SSHs into the homelab. When he mentions file paths like `/Users/carterbrown/...`, those are on his Mac and **not reachable** from this session. Don't try to read Mac paths directly — they'll 404. For screenshots or files on his Mac, suggest `scp`-ing to the homelab first, or ask him to describe the content in words. Everything under `/home/carter/` is local and readable.

## Where the deep dives live

Verbose architecture for subsystems an agent only needs when actively working on them. These are in `~/notes/homelab/` (standalone vault repo, grepped on-demand):

- [`local-llm-gaming-rig.md`](notes/homelab/local-llm-gaming-rig.md) — llm-proxy / llama-swap topology, models, env vars, troubleshooting
- [`email-digests.md`](notes/homelab/email-digests.md) — 9-phase digest workflow, stories-in-flight, audit/debug
- [`homelab-steward.md`](notes/homelab/homelab-steward.md) — steward phases, work queue, executor, budget guard, debugging
- [`homelab-backup.md`](notes/homelab/homelab-backup.md) — 23-target taxonomy, pre-collection, verify/latest/list subcommands, restore drill, retention, notify/debug

Grep the vault (`rg -l "term" ~/notes/`) before starting work on a known topic; the `~/notes/INDEX.md` lists all formal notes.
