# AGENTS.md

This file provides guidance to omp agents when working on this homelab.

**Maintenance:** Keep this file up to date. When deploying a new app, adding a service, changing ports/IPs, or making any structural changes to the homelab, update the relevant sections here as part of that work. Deep-dive architecture for subsystems lives in `~/notes/docs/homelab/` and `~/notes/journal/` (see "Where the deep dives live" at the bottom) — keep AGENTS.md as the always-loaded operational reference and update the relevant note when those subsystems change.

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

**ThinkPad L14 Gen 3 (AMD)** — Ryzen 5 PRO 5675U, 16GB RAM, 500GB NVMe. Wired NIC `enp3s0f0` (primary), secondary IP `192.168.4.92` for k3s ingress. Full specs at [`~/notes/docs/homelab/hardware.md`](notes/docs/homelab/hardware.md).
## Repository Structure

Home directory managed as a bare git repo for dotfiles. Key dirs:
- `blog/` / `delta_neutral/` — Rails 8 apps (deploy wrappers, apps nested within)
- `homelab-backup/` — Go backup service
- `k3s/` — Kubernetes manifests
- `dev/` — Scratch space for cloned repos, tests, development
- `scripts/` — Digest + steward orchestrators
- `notes/` — Agent-maintained knowledge vault (`docs/` for maintained ref, `logs/` for session history, `journal/` for research/records)
- `digests/` / `backups/` — Automated output archives
- `ideas/` — Unstructured ideas (not maintained)
- `.dotfiles-homelab/` — Bare git repo tracking dotfiles
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

Detailed deploy runbook at [`~/notes/docs/homelab/deployment.md`](notes/docs/homelab/deployment.md).

**Critical rules (every deploy):**
- **Commit before deploy.** Deployed state must match `origin/main`. Check `git status` first.
- **Orphaned docker-proxy.** Container exit 255 can leave `docker-proxy` holding the port. Fix: `sudo kill <proxy-pid>`, `docker rm <container>`, `bash up.sh`.
- **"Missing feature" = check cache first.** Cloudflare serves stale HTML if origin is down. `curl` the origin before debugging code.
- **Exit 255 is intermittent.** Restart with existing image; don't rebuild.
- **Never run `sudo aa-remove-unknown`.** Can delete AppArmor profiles Docker/containerd depend on.
## Kubernetes (k3s)

`k` is aliased to `kubectl`. Full reference at [`~/notes/docs/homelab/k3s.md`](notes/docs/homelab/k3s.md).

**Key:** flannel-iface must match `enp3s0f0` (wired, not WiFi). Pod↔host traffic needs `ufw allow in on cni0` + `flannel.1` — if ClusterIPs fail after a reboot/ufw reload, check these first.
## App Details

Each app has a reference doc in `~/notes/docs/homelab/`:

- **Blog** (Rails 8, port 33099) → [`blog.md`](notes/docs/homelab/blog.md)
- **Delta Neutral** (Rails 8, port 43080) → [`delta-neutral.md`](notes/docs/homelab/delta-neutral.md)
- **Homelab Backup** (Go, daily 03:00 UTC → R2) → [`homelab-backup.md`](notes/docs/homelab/homelab-backup.md)
- **Dependabot Webhook** (Go, localhost:9099) → [`dependabot-webhook.md`](notes/docs/homelab/dependabot-webhook.md)
- **Open WebUI** (chat frontend, localhost:48100) → [`open-webui.md`](notes/docs/homelab/open-webui.md)
- **OMP Web** (agent web UI, localhost:30141) → [`omp-web.md`](notes/docs/homelab/omp-web.md)
- **SearXNG** (search backend, localhost:8080) → [`searxng.md`](notes/docs/homelab/searxng.md)
- **Cloudflare** (API token, tunnel, DNS) → [`cloudflare.md`](notes/docs/homelab/cloudflare.md)
- **OpenCode Go Proxy** (localhost:8082) → [`opencode-go-proxy.md`](notes/docs/homelab/opencode-go-proxy.md)
## Email Digests

Five daily HTML digests (ai-tech, agentic-platform, ai-hardware, gaming, world) via 9-phase Python workflow. Full architecture at [`~/notes/docs/homelab/email-digests.md`](notes/docs/homelab/email-digests.md).

All five run sequentially at 08:00 UTC via `digests-daily.timer`. Key files: `~/scripts/digest_runner.py`, `~/scripts/run_all_digests.sh`, `~/scripts/send_digest.py`.
## Homelab Steward

Daily maintenance at 5:00 PM ET via `homelab-steward.timer`. 9-phase Python orchestrator (`~/scripts/steward_runner.py`). Full architecture at [`~/notes/docs/homelab/homelab-steward.md`](notes/docs/homelab/homelab-steward.md).

**Safety rules:** never `dist-upgrade`, never `aa-remove-unknown`, Docker engine via apt `--only-upgrade`, assert `DockerRootDir=/var/lib/docker` after upgrade, stop on first failure.
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
- `~/notes/docs/` — maintained reference docs (subsystem architecture and runbooks)
- `~/notes/logs/` — session memoirs (YYYY-MM-DD.md)
- `~/notes/journal/` — research notes and project records (not maintained)
- The vault is a standalone git repo (not the dotfiles bare repo) — `/note-save` handles commits
## Gaming Rig (Windows 11)

Windows 11 gaming PC at `192.168.4.103` (`ssh gamingrig`). Hosts the local LLM stack (llama-swap + llm-proxy). Full operational runbook at [`~/notes/docs/homelab/local-llm-gaming-rig.md`](notes/docs/homelab/local-llm-gaming-rig.md).

**OpenCode Go Proxy** (`localhost:8082`) — routes `opencode-go/*` models across 2 subscriptions. If opencode-go models fail, check this first. Full doc at [`~/notes/docs/homelab/opencode-go-proxy.md`](notes/docs/homelab/opencode-go-proxy.md).
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

Verbose architecture for subsystems an agent only needs when actively working on them. These are in `~/notes/docs/homelab/` and `~/notes/journal/` (standalone vault repo, grepped on-demand):

- [`hardware.md`](notes/docs/homelab/hardware.md) — hardware specs, network config
- [`local-llm-gaming-rig.md`](notes/docs/homelab/local-llm-gaming-rig.md) — llm-proxy / llama-swap topology, models, env vars, troubleshooting
- [`deployment.md`](notes/docs/homelab/deployment.md) — deploy flow, port-in-use, exit 255, aa-remove-unknown
- [`k3s.md`](notes/docs/homelab/k3s.md) — k3s architecture, flannel, CNI ufw rules
- [`email-digests.md`](notes/docs/homelab/email-digests.md) — 9-phase digest workflow, stories-in-flight, audit/debug
- [`homelab-steward.md`](notes/docs/homelab/homelab-steward.md) — steward phases, work queue, executor, budget guard, debugging
- [`homelab-backup.md`](notes/docs/homelab/homelab-backup.md) — 23-target taxonomy, pre-collection, verify/latest/list subcommands, restore drill, retention, notify/debug
- [`blog.md`](notes/docs/homelab/blog.md) — Rails 8 blog app
- [`delta-neutral.md`](notes/docs/homelab/delta-neutral.md) — Rails 8 rebalancer + Hyperliquid SDK timer
- [`dependabot-webhook.md`](notes/docs/homelab/dependabot-webhook.md) — Go webhook + Prompt-Guard classifier
- [`open-webui.md`](notes/docs/homelab/open-webui.md) — chat frontend, searxng integration
- [`omp-web.md`](notes/docs/homelab/omp-web.md) — agent web UI, next.js build quirks
- [`searxng.md`](notes/docs/homelab/searxng.md) — metasearch backend, config
- [`cloudflare.md`](notes/docs/homelab/cloudflare.md) — API token, tunnel ingress, DNS
- [`opencode-go-proxy.md`](notes/docs/homelab/opencode-go-proxy.md) — dual-account proxy, ufw bridge rules, cookie expiry

`journal/` contains research notes and project records (not maintained). `logs/sessions/` contains chronological session memoirs.

Grep the vault (`rg -l "term" ~/notes/`) before starting work on a known topic; the `~/notes/INDEX.md` lists all formal notes.
