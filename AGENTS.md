# AGENTS.md

This file provides guidance to AI agents (opencode, Claude, etc.) when working with code in this repository.

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

## Overview

Single-node homelab running on Ubuntu Server (ThinkPad L14 Gen 3, AMD Ryzen 5 PRO 5675U, 16GB RAM, 500GB NVMe SSD). A k3s Kubernetes cluster routes traffic via Traefik ingress to apps running in Docker Compose on the host machine. The server uses wired ethernet (`enp3s0f0`) as its primary uplink, with static secondary IPs `192.168.4.92` (blog, delta_neutral, hub) and `192.168.4.102` (tbitt, stickies — both not live) — all on the same physical interface. WiFi (`wlp6s0`) is disabled.

## Hardware

| Component | Details |
|---|---|
| **Model** | ThinkPad L14 Gen 3 (AMD) |
| **Machine Type** | 21C6 |
| **Serial** | PW099A70 |
| **CPU** | AMD Ryzen 5 PRO 5675U (6C/12T, 2.3–4.3GHz) |
| **RAM** | 16GB DDR4-3200 (2x SO-DIMM slots, dual-channel) |
| **Storage** | 500GB NVMe M.2 2242 SSD (PCIe 3.0 x4) |
| **Display** | 14" FHD (1920×1080) IPS |
| **Network** | Gigabit Ethernet (Realtek RTL8111HN/EPV), Wi-Fi 6E, Bluetooth 5.1 |
| **Ports** | 2× USB-C (Gen 1 + Gen 2, both PD + DP), 2× USB-A 3.2 Gen 1, HDMI 2.0, RJ-45, microSD, 3.5mm combo |
| **BIOS** | R1YET47W (1.24), 08/04/2023 |

**Notes:**
- The wired NIC `enp3s0f0` (Realtek) is the **primary uplink**.
- **WiFi is disabled** in netplan. The interface `wlp6s0` is down by default.
- **Network config:** `/etc/netplan/50-cloud-init.yaml` (managed by systemd-networkd)
  - `enp3s0f0`: DHCP primary (`192.168.4.100`), static secondary (`192.168.4.92/22`, `192.168.4.102/22`)
  - `wlp6s0`: Removed from netplan — disabled
- **Default route:** Via `enp3s0f0` (metric 100)
- **k3s ingress IPs:** `192.168.4.92` (blog, delta_neutral, hub) and `192.168.4.102` (tbitt, stickies — both not live) are secondary IPs on the wired interface.
- **Rollback:** To re-enable WiFi, restore `/etc/netplan/50-cloud-init.yaml.bak` and run `sudo netplan apply`.

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

## Dotfiles Management

```bash
# The 'dotfiles' alias manages the bare repo
dotfiles status
dotfiles add <file>
dotfiles commit -m "message"
dotfiles push
```

Alias defined in `.zshrc`: `dotfiles='/usr/bin/git --git-dir="$HOME/.dotfiles-homelab/" --work-tree="$HOME"'`

## App Deployment Pattern

All apps follow the same deploy flow:
1. `release.sh` - pulls latest code, tears down containers, removes old images, calls `up.sh`
2. `up.sh` - starts Docker Compose in detached mode with production config

Rails apps (blog, hub) pass `RAILS_MASTER_KEY` from `config/master.key` at startup.

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

When a user reports "my blog redeployed and lost feature X" (or "shows old UI"), **do not assume a deploy regression** and do not start rolling back. The near-universal cause is **Cloudflare or browser cache serving a stale page while origin is down**. CF keeps serving its last cached HTML when the origin 5xx's — which happens whenever `docker-proxy` is orphaned (previous section) or the container is Exited.

Diagnose in this order before touching code:
1. `docker ps --filter name=<app>` — is the container actually running? If not, the user is seeing a cache.
2. `grep -r <feature> <app-dir>/app/views <app-dir>/app/controllers` — does the code on disk have the feature? (Usually yes.)
3. `docker run --rm --entrypoint ls <image> /rails/app/views/<feature>` — does the **image** have the feature? (Usually yes.)
4. `curl -s http://localhost:<port>/` — does the origin serve the feature now? If yes, it's 100% a cache issue.
5. Only after 1–4 clear: investigate whether rebuild skipped or an old commit got deployed.

If origin is healthy and the user still sees stale content, the fix is **not a redeploy** — it's either a hard-refresh on their end (`Cmd+Shift+R`) or a Cloudflare cache purge. Redeploying a healthy app wastes a build cycle and risks another exit-255 crash during the restart window, which *extends* the cache-hit problem.

### Exit 255 is a known intermittent on this host

Documented for visibility: `blog-web` and other containers on this 16GB host occasionally exit 255 without warning, no stack trace in `docker logs`. Likely causes are OOM (check `docker inspect <container> --format '{{.State.OOMKilled}}'` next time it happens) or SIGKILL from a competing deploy. Don't rebuild in response — just restart with the existing image unless there's evidence the image itself is bad.

## Kubernetes (k3s)

```bash
k get pods          # 'k' is aliased to 'kubectl'
k get svc
k logs -n <namespace> -l app=<appname>
k delete pod <name>  # k3s auto-recreates
```

**Architecture pattern:** Two deployment models coexist:
- **Self-developed webapps** (blog, hub, tbitt, stickies, delta_neutral) run on the host in Docker Compose. K3s uses ExternalService + Endpoints to route Traefik ingress to host IPs (blog/delta_neutral at 192.168.4.92, hub/tbitt/stickies at 192.168.4.102). Note: stickies is not currently live; tbitt is deprecated.
- **Third-party services** (grafana, prometheus, node-exporter, freshrss, uptime-kuma, traefik) run natively as k3s Deployments/DaemonSets.

Each service in `k3s/` has its own directory with granular YAML manifests (deployment, service, ingress, etc.).

## App Details

### Blog (Rails 8 + SQLite)
- Port: 3099 (internal) / 33099 (exposed)
- Markdown-based content in `app/posts/` and `app/reviews/` (git-ignored)
- Obsidian vault image syntax (`![[file.jpg]]`) converted automatically
- Has its own `AGENTS.md` with detailed architecture docs
- Ruby 3.4.3, Propshaft, Importmap, Turbo/Stimulus

### Hub (React + Rails API)
- Client: React 19 + TypeScript + Vite (port 3000/13000)
- Server: Rails 8 API-only (port 3001/13001)
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
- Verifies HMAC-SHA256 signature, then spawns a sandboxed **opencode agent (Qwen 3.7 Max)** to handle bundler bumps
- Agent runs with a narrow permission sandbox (`~/.config/dependabot-webhook/opencode.json`, pointed to via the `OPENCODE_CONFIG` env var) — a default-deny bash floor + a git/bundle/gh/rake allowlist; sudo/docker/systemctl/curl/wget/rm/release.sh/up.sh denied. Verified that headless `opencode run` enforces these deny rules (it drops the bash tool entirely on a full deny, and blocks non-allowlisted commands incl. non-`main` git push).
- 90-second coalesce window so a burst of PRs is handled in one agent run
- Source: `~/dev/dependabot-webhook/`; config (with webhook secret): `~/.config/dependabot-webhook/env`
- Logs: `journalctl --user -u dependabot-webhook -f`
- Release: `cd ~/dev/dependabot-webhook && bash release.sh`

### Open WebUI (Homelab Chat)
- ChatGPT/Claude-style self-hosted chat UI at `https://chat.carter2099.com`. Not an agent — a general chat front-end.
- Docker Compose in `~/open-webui/` (`ghcr.io/open-webui/open-webui:main`), bound **`127.0.0.1:48100`** (loopback-only).
- **Backend = the OpenCode Go endpoint** (`OPENAI_API_BASE_URL=https://opencode.ai/zen/go/v1`) so chat usage rides the **flat-sub session-cap billing**, NOT `zen/v1` pay-as-you-go. The 18 Go models populate automatically; a few (e.g. `qwen3.7-max`) 401 as "not supported for format oa-compat" and are opencode-native-only — just pick another. (See the Zen-vs-Go endpoint note: same account key, the base URL picks product/billing.)
- Secrets (`OPENAI_API_KEY` = the Go key, `WEBUI_SECRET_KEY`) in gitignored `~/open-webui/.env` (600). Compose + `up.sh` are tracked; `.env` is not.
- **Routing: direct-tunnel pattern** (like `opencode-homelab`/dependabot, NOT Traefik) — tunnel ingress `chat.carter2099.com → http://localhost:48100`; proxied CNAME `chat` → `<tunnel-id>.cfargotunnel.com`. Loopback bind = off the LAN, only reachable via the tunnel.
- **Auth: two layers.** CF Access (edge SSO, manually configured in Zero Trust) + Open WebUI's own login (`WEBUI_AUTH=True`, `ENABLE_SIGNUP=False`). Admin `carter2099@pm.me`, created over loopback so there was never an open-signup window.
- Manage: `cd ~/open-webui && bash up.sh` (pull + restart); `docker compose -f ~/open-webui/docker-compose.yml logs -f`.

### Cloudflare API Access
- Account-owned API token at `~/.config/cloudflare/api-token` (gitignored, 600 perms)
- Scopes: Cloudflare Tunnel:Edit (account), DNS:Edit (carter2099.com zone). **No Zero Trust / Access scope** — so Access apps/policies (the SSO gate in front of tunneled hostnames) must be configured **manually in the Zero Trust dashboard**; the API token returns 403 on `/access/apps`. To automate Access too, add "Access: Apps and Policies: Edit" (Account) to the token.
- Supporting IDs in `~/.config/cloudflare/`: `account-id`, `zone-id`, `homelab-tunnel-id`
- Env vars (`CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_ZONE_ID`, `CLOUDFLARE_HOMELAB_TUNNEL_ID`) exported from `.zshrc`
- To add a new public hostname to the homelab tunnel: PUT `/accounts/{id}/cfd_tunnel/{tunnel_id}/configurations` with updated ingress array, then POST DNS CNAME to `/zones/{zone_id}/dns_records`

## Email Digests

Four daily HTML email digests are scheduled via systemd user timers. Each digest runs a **headless opencode agent on the OpenCode Go subscription, pinned to MiniMax M3** (`opencode-go/minimax-m3`). The agent researches via opencode's built-in **WebFetch** (there is *no* WebSearch tool in opencode), fills the shared `~/digests/template.html`, emails via `send_digest.py`, and writes a dedup/continuity summary to `~/digests/<topic>/`.

| Timer | Fires (UTC) | Topic |
|---|---|---|
| `ai-tech-digest` | 15:00 | AI & tech |
| `agentic-digest` | 16:00 | Agentic platforms / agent tooling |
| `gaming-digest` | 19:00 | Gaming news |
| `world-digest` | 21:00 | U.S. / world events |

Service + timer units live in `~/.config/systemd/user/<name>.{service,timer}`; the actual run scripts are `~/scripts/run_<name>_digest.sh`. Manage with `systemctl --user list-timers`, `systemctl --user status <name>.timer`, `journalctl --user -u <name>.service`.

Each script ends with `opencode run -m opencode-go/minimax-m3 "$PROMPT" < /dev/null`. **Headless opencode gotchas (learned the hard way):** (1) stdin MUST be closed (`< /dev/null`) or the process hangs after finishing the task; (2) opencode auto-rejects file writes *outside the working directory* in headless mode, so all I/O — including the temp HTML — must stay under `/home/carter` (not `/tmp`); (3) `opencode run` exits 0 even when the model skipped a step, so verify artifacts (the `Sent to …` journal line + the summary `.md`), never the exit code alone. Auth is an OpenCode Go API key in `~/.local/share/opencode/auth.json`. The agentic digest's second recipient is kept out of the public dotfiles repo — it's read from `AGENTIC_CC=` in the un-tracked `~/scripts/.smtp_config`.

Carter often references these by topic when chatting ("I saw something in the agentic digest about X"). When he does, note-taking into `~/notes/` is the likely follow-up.

## Remote Agent Operations

This homelab runs an **always-on opencode web agent** accessible from any browser at `https://opencode.carter2099.com`. It runs `opencode web` as a systemd user service (`opencode-homelab.service`) under the `carter` user with `loginctl enable-linger` so it survives reboots. It is **intentionally full-privilege** (no command denylist, no `NoNewPrivileges`); the trust anchor is **Cloudflare Access**.

- **Service unit:** `~/.config/systemd/user/opencode-homelab.service` → `opencode web --hostname 127.0.0.1 --port 48099`. **Loopback-only bind on purpose** — the sole ingress is the CF tunnel; it is NOT reachable on the LAN (so there's no path that bypasses Cloudflare Access).
- **Access URL:** `https://opencode.carter2099.com` (browser → CF Access SSO → opencode UI).
- **Two independent credentials (defense in depth):**
  1. **Cloudflare Access** (identity gate at the CF edge) — unauthenticated requests get a 302 to `carter2099.cloudflareaccess.com` and never reach the host. Policy is managed in the CF Zero Trust dashboard.
  2. **`OPENCODE_SERVER_PASSWORD`** — opencode's own HTTP basic-auth, enforced server-side (401 without). Stored in `~/.config/opencode-homelab/env` (gitignored, 600). **Never commit this file.** The basic-auth **username MUST be `opencode`** (literal) — opencode's API rejects every other username even with the right password; the password is the `OPENCODE_SERVER_PASSWORD` value.
- **Routing:** uses the **direct-tunnel (webhook) pattern, NOT Traefik** — deliberately, so the app stays off the LAN. Tunnel ingress `opencode.carter2099.com → http://localhost:48099` (cloudflared runs on the host and reaches loopback) → the `opencode web` process. No k3s manifest, no ExternalService/Endpoints, no Traefik hop. DNS: proxied CNAME `opencode` → `<tunnel-id>.cfargotunnel.com`. (This mirrors how `hooks.carter2099.com → localhost:9099` works for the dependabot webhook.)
- **Wart:** `opencode web` tries to `xdg-open` a browser at startup (ENOENT on this headless host) — **non-fatal**, the server runs fine; it's just noise in the journal.
- **Logs:** `journalctl --user -u opencode-homelab -f`
- **Restart:** `systemctl --user restart opencode-homelab`

### Debugging from an interactive SSH session

If the opencode web agent is misbehaving or unreachable, an interactive agent SSH'd into the box diagnoses it. **First thing every SSH session should do** before `systemctl --user ...` commands:

```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u)   # required for systemctl --user to reach the user bus
```

Standard diagnosis sequence:

```bash
systemctl --user status opencode-homelab --no-pager -l     # running? crashlooping?
journalctl --user -u opencode-homelab -n 100 --no-pager    # why it failed
ls -la ~/agent-state/                                      # pending reboot context, etc
cat ~/.config/opencode/opencode.jsonc                      # config
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

## Environment

- **Shell:** zsh with vim keybindings
- **Editor:** neovim (built from source in `build/neovim/`)
- **Ruby:** managed via rbenv
- **Node:** managed via fnm
- **Tmux prefix:** Ctrl+Space
- **Git user:** carter2099 <carter2099@pm.me>
- **GitHub CLI:** `gh` authenticated as carter2099 (HTTPS, broad scopes)
