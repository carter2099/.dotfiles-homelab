# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

**Maintenance:** Keep this file up to date. When deploying a new app, adding a service, changing ports/IPs, or making any structural changes to the homelab, update the relevant sections here as part of that work.

**Memory backup:** Claude's auto-memory files live in `~/.claude/projects/-home-carter/memory/`. They are tracked by the dotfiles bare repo and backed up to GitHub. Whenever a new memory file is written, run:
```bash
dotfiles add .claude/projects/-home-carter/memory/<new-file>.md && dotfiles commit -m "memory: add <name>" && dotfiles push
```

## Working principles (Endler tenets)

Carter endorses the tenets in [The Best Programmers](https://endler.dev/2025/best-programmers/). The subset below is the part that applies directly to an LLM assistant and should shape every session. Full list — including the ones aimed at Carter's own practice — is in auto-memory (`user_endler_tenets.md`).

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

Single-node homelab running on Ubuntu Server (2017 MacBook Pro, Intel i5, 8GB RAM). A k3s Kubernetes cluster routes traffic via Traefik ingress to apps running in Docker Compose on the host machine. The server has two IP addresses (`192.168.4.92` and `192.168.4.102`) used to route different apps — both point to the same physical machine.

## Repository Structure

This is the home directory, managed as a bare git repo for dotfiles:
- `blog/` - Rails 8 blog app (blog.carter2099.com)
- `hub/` - React + Rails API landing page/portfolio (carter2099.com)
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
- `notes/` - Claude-maintained markdown knowledge vault
- `digests/` - Daily digest archives (`<topic>/YYYY-MM-DD.md`)
- `agent-state/` - Cross-reboot task persistence (`pending.md`)
- `backups/` - Local backup archives (written by homelab-backup service)
- `.dotfiles-homelab/` - Bare git repo tracking dotfiles

## Dev Workflow (`dev/`)

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

Documented for visibility: `blog-web` and other containers on this 8GB host occasionally exit 255 without warning, no stack trace in `docker logs`. Likely causes are OOM (check `docker inspect <container> --format '{{.State.OOMKilled}}'` next time it happens) or SIGKILL from a competing deploy. Don't rebuild in response — just restart with the existing image unless there's evidence the image itself is bad.

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
- Verifies HMAC-SHA256 signature, then spawns a sandboxed Claude agent to handle bundler bumps
- Agent runs with narrow permission allowlist (`~/.claude/dependabot-agent-settings.json`) — no sudo, no docker, no deploy
- 90-second coalesce window so a burst of PRs is handled in one agent run
- Source: `~/dev/dependabot-webhook/`; config (with webhook secret): `~/.config/dependabot-webhook/env`
- Logs: `journalctl --user -u dependabot-webhook -f`
- Release: `cd ~/dev/dependabot-webhook && bash release.sh`

### Cloudflare API Access
- Account-owned API token at `~/.config/cloudflare/api-token` (gitignored, 600 perms)
- Scopes: Cloudflare Tunnel:Edit (account), DNS:Edit (carter2099.com zone)
- Supporting IDs in `~/.config/cloudflare/`: `account-id`, `zone-id`, `homelab-tunnel-id`
- Env vars (`CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_ZONE_ID`, `CLOUDFLARE_HOMELAB_TUNNEL_ID`) exported from `.zshrc`
- To add a new public hostname to the homelab tunnel: PUT `/accounts/{id}/cfd_tunnel/{tunnel_id}/configurations` with updated ingress array, then POST DNS CNAME to `/zones/{zone_id}/dns_records`

## Email Digests

Four daily HTML email digests are scheduled via systemd user timers. Each spawns a headless Claude agent that searches the web for recent news on its topic and emails Carter a summary.

| Timer | Fires (UTC) | Topic |
|---|---|---|
| `ai-tech-digest` | 15:00 | AI & tech |
| `agentic-digest` | 16:00 | Agentic platforms / agent tooling |
| `gaming-digest` | 19:00 | Gaming news |
| `world-digest` | 21:00 | U.S. / world events |

Service + timer units live in `~/.config/systemd/user/<name>.{service,timer}`; the actual run scripts are `~/scripts/run_<name>_digest.sh`. Manage with `systemctl --user list-timers`, `systemctl --user status <name>.timer`, `journalctl --user -u <name>.service`. Created/edited via the `email-digest` skill.

Carter often references these by topic when chatting ("I saw something in the agentic digest about X"). When he does, note-taking into `~/notes/` is the likely follow-up.

## Remote Agent Operations

This homelab runs a **persistent Claude Code agent** accessible from the Claude mobile app. It is a `claude remote-control` process in server mode, running as a systemd user service (`claude-homelab.service`) under the `carter` user with `loginctl enable-linger` so it survives reboots. Authenticated via claude.ai OAuth (Pro plan). The mobile app's Code tab lists it as `homelab`; tapping spawns a session that executes commands on this host.

Permissions are intentionally wide-open (`Bash(*)` in `.claude/settings.local.json`, `NOPASSWD: ALL` in `/etc/sudoers.d/claude-homelab`) because the only inbound path is the mobile app — no email/SMS/webhook ingress, no prompt-injection surface. The `dependabot-webhook` service is a separate, narrowly-sandboxed agent and does NOT use the daemon's permissions.

- **Service unit:** `~/.config/systemd/user/claude-homelab.service`
- **Logs:** `journalctl --user -u claude-homelab -f`
- **Restart self:** `systemctl --user restart claude-homelab` (disconnects the current mobile session; a new one will appear after ~10s)

### Which agent am I? (read this first)

There are two possible identities for a Claude session reading this file:

1. **The mobile daemon** — the persistent `claude-homelab.service` session driven from the mobile app. Environment variable `CLAUDE_HOMELAB_DAEMON=1` is set. Do the **Startup check** and **Reboot protocol** below. When asked to "debug the daemon," that's self-diagnosis — you can still check your own logs and restart yourself, but be aware `systemctl --user restart claude-homelab` will kill your current session.
2. **An interactive SSH agent** — started by the user from a terminal (e.g. `ssh tp-server` → `claude`). `CLAUDE_HOMELAB_DAEMON` is unset. Do **not** do the startup check (the `pending.md` file is the daemon's recovery mechanism; an SSH agent surfacing it confuses the handoff). Your job when debugging is observing and fixing the daemon via the commands in "Debugging from an interactive SSH session" below.

Check with `echo "${CLAUDE_HOMELAB_DAEMON:-unset}"` if unsure. If in doubt, assume interactive SSH — the daemon is the less common case.

### Debugging from an interactive SSH session

If the mobile agent is misbehaving, unreachable, or crashlooping, an interactive agent SSH'd into the box diagnoses it. **First thing every SSH session should do** before `systemctl --user ...` commands:

```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u)   # required for systemctl --user to reach the user bus
```

Standard diagnosis sequence:

```bash
systemctl --user status claude-homelab --no-pager -l     # running? crashlooping?
journalctl --user -u claude-homelab -n 100 --no-pager    # why it failed
ps -ef | grep -E 'claude (remote-control|--print)'       # parent + per-session SDK children
ls -la ~/agent-state/                                    # pending reboot context, etc
cat ~/.claude/settings.json                              # model + effort
cat ~/.claude/settings.local.json                        # permissions
claude auth status                                       # OAuth still valid?
```

**Known crashloop causes** (seen during initial bring-up — check logs for the exact error):
- `Workspace not trusted` → `projects./home/carter.hasTrustDialogAccepted` in `~/.claude.json` is false. Either run `claude` once in `~` interactively to accept, or set the bit directly with a small Python edit.
- `Enable Remote Control? (y/n)` then exit 0 → one-time consent not yet accepted. Run `claude remote-control --name homelab --spawn same-dir` once interactively, answer `y`, Ctrl+C, then restart the service.
- `Unknown argument: --model` / `--effort` → `remote-control` subcommand doesn't accept those flags. Model + effort come from `~/.claude/settings.json` only; don't add them to ExecStart.
- `Failed to connect to bus: No medium found` → the interactive shell is missing `XDG_RUNTIME_DIR`. Export it (see above).

Flags the `remote-control` subcommand actually supports: `--name`, `--spawn {same-dir|worktree|session}`, `--capacity <N>`, `--permission-mode {acceptEdits|auto|bypassPermissions|default|dontAsk|plan}`, `--verbose`, `--debug-file <path>`.

### Startup check (daemon only — do this first in every new session)

**Applies only if `CLAUDE_HOMELAB_DAEMON=1` (see "Which agent am I?" above).** At the start of every new daemon session, check `~/agent-state/pending.md`. If it exists:
1. Read it.
2. If `mtime` is within the last 30 minutes, summarize its contents to the user up front ("Last reboot was at X for reason Y; in-flight task was Z").
3. Delete the file (`rm ~/agent-state/pending.md`) once acknowledged so it doesn't re-surface next session.
4. If mtime is older than 30 min, the file is stale — surface it briefly and delete.

This is the mechanism by which tasks survive reboots. It is the *only* expectation of cross-reboot continuity.

### Reboot protocol

Never `sudo reboot` directly. Use the `homelab-reboot` skill, which:
1. Writes `~/agent-state/pending.md` with timestamp, reason, and a one-paragraph summary of the current in-flight work.
2. Only then issues `sudo systemctl reboot`.

This guarantees the next session has context for what happened. If the skill isn't available for some reason, do the two steps manually in that order.

## Environment

- **Shell:** zsh with vim keybindings
- **Editor:** neovim (built from source in `build/neovim/`)
- **Ruby:** managed via rbenv
- **Node:** managed via fnm
- **Tmux prefix:** Ctrl+Space
- **Git user:** carter2099 <carter2099@pm.me>
- **GitHub CLI:** `gh` authenticated as carter2099 (HTTPS, broad scopes)
