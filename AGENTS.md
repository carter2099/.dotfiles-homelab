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

Never run `sudo aa-remove-unknown` on this host. Snap-installed Docker depends on AppArmor profiles (e.g. `snap.docker.dockerd`) that `aa-remove-unknown` classifies as "unknown" and deletes. This causes Docker to crashloop with "missing profile snap.docker.dockerd" and all containers go down. Recovery requires `sudo systemctl restart snapd.apparmor` to reload the profiles, then restarting Docker. If Docker containers can't be stopped/killed due to AppArmor "permission denied" errors, fix by restarting `snapd.apparmor` and then Docker (`sudo systemctl restart snapd.apparmor && sudo snap start docker.dockerd`) — not by clearing AppArmor profiles.

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
- Targets: blog posts, reviews, images, blog SQLite DB, FreshRSS SQLite DB + config
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
- Docker Compose in `~/open-webui/` (`ghcr.io/open-webui/open-webui:main`), bound **`127.0.0.1:48100`** (loopback-only).
- **Backend = the OpenCode Go endpoint** (`OPENAI_API_BASE_URL=https://opencode.ai/zen/go/v1`) so chat usage rides the **flat-sub session-cap billing**, NOT `zen/v1` pay-as-you-go. The 18 Go models populate automatically; a few (e.g. `qwen3.7-max`) 401 as "not supported for format oa-compat" and are opencode-native-only — just pick another. (See the Zen-vs-Go endpoint note: same account key, the base URL picks product/billing.)
- Secrets (`OPENAI_API_KEY` = the Go key, `WEBUI_SECRET_KEY`) in gitignored `~/open-webui/.env` (600). Compose + `up.sh` are tracked; `.env` is not.
- **Routing: direct-tunnel pattern** (like pi-web/dependabot, NOT Traefik) — tunnel ingress `chat.carter2099.com → http://localhost:48100`; proxied CNAME `chat` → `<tunnel-id>.cfargotunnel.com`. Loopback bind = off the LAN, only reachable via the tunnel.
- **Auth: two layers.** CF Access (edge SSO, manually configured in Zero Trust) + Open WebUI's own login (`WEBUI_AUTH=True`, `ENABLE_SIGNUP=False`).
- Manage: `cd ~/open-webui && bash up.sh` (pull + restart); `docker compose -f ~/open-webui/docker-compose.yml logs -f`.

### Cloudflare API Access
- Account-owned API token at `~/.config/cloudflare/api-token` (gitignored, 600 perms)
- Scopes: Cloudflare Tunnel:Edit (account), DNS:Edit (carter2099.com zone). **No Zero Trust / Access scope** — so Access apps/policies (the SSO gate in front of tunneled hostnames) must be configured **manually in the Zero Trust dashboard**; the API token returns 403 on `/access/apps`. To automate Access too, add "Access: Apps and Policies: Edit" (Account) to the token.
- Supporting IDs in `~/.config/cloudflare/`: `account-id`, `zone-id`, `homelab-tunnel-id`
- Env vars (`CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_ZONE_ID`, `CLOUDFLARE_HOMELAB_TUNNEL_ID`) exported from `.zshrc`

## Email Digests

Four daily HTML email digests are scheduled via systemd user timers. Each digest runs a **headless Pi agent** (`pi -p`) on the OpenCode Go subscription, using **deepseek-v4-flash**. The agent researches via `web_search` (for discovering articles) and `web_fetch` (for reading pages), fills the shared `~/digests/template.html`, emails via `send_digest.py`, and writes a URL-enriched dedup/continuity summary to `~/digests/<topic>/YYYY-MM-DD.md`.

| Timer | Fires (UTC) | Topic |
|---|---|---|
| `ai-tech-digest` | 15:00 | AI & tech |
| `agentic-digest` | 16:00 | Agentic platforms / agent tooling |
| `gaming-digest` | 19:00 | Gaming news |
| `world-digest` | 21:00 | U.S. / world events |

Service + timer units live in `~/.config/systemd/user/<name>.{service,timer}`; the actual run scripts are `~/scripts/run_<name>_digest.sh`. Manage with `systemctl --user list-timers`, `systemctl --user status <name>.timer`, `journalctl --user -u <name>.service`.

Each script runs `pi -p --model opencode-go/deepseek-v4-flash "$PROMPT"`. Pi's `-p` mode is the equivalent of `opencode run` for headless/automated use — no stdin hacks needed, no write-path restrictions. The agentic digest's second recipient is kept out of the public dotfiles repo — it's read from `AGENTIC_CC=` in the un-tracked `~/scripts/.smtp_config`.

### Quality infrastructure

Each run writes a summary (`~/digests/<topic>/YYYY-MM-DD.md`), an HTML archive (`.html`), and a run log (`.runs.log`). Summaries are machine-readable for retrospective quality audits.

Carter often references these by topic when chatting ("I saw something in the agentic digest about X"). When he does, note-taking into `~/notes/` is the likely follow-up.

## Remote Agent Operations

This homelab runs an **always-on pi-web agent** accessible from any browser at `https://opencode.carter2099.com`. It runs `pi-web` (installed via `npm install -g @jmfederico/pi-web`) as two systemd user services with `loginctl enable-linger` so they survive reboots. It is **intentionally full-privilege** (no command denylist, no `NoNewPrivileges`); the trust anchor is **Cloudflare Access**.

- **Services:** `pi-web-sessiond.service` (session daemon) + `pi-web.service` (web/API at `127.0.0.1:8504`). **Loopback-only bind on purpose** — the sole ingress is the CF tunnel; it is NOT reachable on the LAN (so there's no path that bypasses Cloudflare Access).
- **Access URL:** `https://opencode.carter2099.com` (browser → CF Access SSO → pi-web UI). The old `pi.carter2099.com` hostname also routes to the same service.
- **Auth:** Cloudflare Access (identity gate at the CF edge) — unauthenticated requests get a 302 to `carter2099.cloudflareaccess.com` and never reach the host. Policy is managed in the CF Zero Trust dashboard. No secondary password layer (unlike opencode-web which had `OPENCODE_SERVER_PASSWORD`).
- **Routing:** direct-tunnel pattern — tunnel ingress `opencode.carter2099.com → http://localhost:8504` (cloudflared runs on the host and reaches loopback). No k3s manifest, no ExternalService/Endpoints, no Traefik hop. DNS: proxied CNAME `opencode` → `<tunnel-id>.cfargotunnel.com`.
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

The gaming rig runs **llama-swap** on top of llama.cpp's `llama-server.exe`, serving GGUF models from `C:\llm\`. The homelab runs **llm-proxy** (`~/dev/llm-proxy/`), a Go reverse proxy that handles WoL wake-on-demand, gaming-aware auto-pause, and SSH lifecycle management.

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
                │                     SSH reachable? → start llama-swap
                │                            ↓ no
                │                     send WoL → wait → SSH → start llama-swap
                │
                └─ Background: check encoder sessions every 10s
                   → gaming detected? kill LLM to free VRAM
                   → gaming ended? restart LLM
```

The proxy replaces three old components: `llama-server.service`, `llama-gaming-proxy.timer`, and `~/.local/bin/llama-gaming-proxy.sh`.

#### Available models

| Model ID | Alias | Architecture | File Size | Notes |
|---|---|---|---|---|
| `Qwen3.6-35B-A3B-Q6_K` | `qwen3.6-35b`, `qwen3.6-35b-q6` | Qwen 3.6 35B MoE (Q6_K) | 28.8 GB | **Default.** Highest quality Qwen. `--cpu-moe` |
| `Qwen3.6-35B-A3B-Q5_K_M` | `qwen3.6-35b-q5` | Qwen 3.6 35B MoE (Q5_K_M) | 25.9 GB | Slightly faster, good for coding. `--cpu-moe` |
| `Qwen3.6-35B-A3B-Q4_K_M` | `qwen3.6-35b-q4` | Qwen 3.6 35B MoE (Q4_K_M) | 22.3 GB | Lighter Qwen. `--cpu-moe` |
| `Gemma-4-26B-A4B-Q6_K` | `gemma4-26b`, `gemma4-26b-q6` | Gemma 4 26B MoE (Q6_K) | 23.2 GB | Multimodal (text+image). `--cpu-moe` + `--mmproj` |
| `GPT-OSS-20B-mxfp4` | `gpt-oss`, `gpt-oss-20b` | GPT-OSS 20B dense (mxfp4) | 12.1 GB | Fits entirely in 12GB VRAM. No offloading. |

#### Service management

- **Service:** `llm-proxy.service` (`~/.config/systemd/user/llm-proxy.service`)
- **Binary:** `~/.local/bin/llm-proxy`
- **Source:** `~/dev/llm-proxy/`
- **Logs:** `journalctl --user -u llm-proxy -f`
- **Restart:** `systemctl --user restart llm-proxy`
- **Deploy:** `cd ~/dev/llm-proxy && bash release.sh`

#### Troubleshooting

**Gaming rig went to sleep** — The proxy sends WoL automatically on next request. No manual intervention needed. Wake + llama-swap startup takes ~30-60s.

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
