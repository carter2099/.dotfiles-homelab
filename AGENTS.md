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
- **Tune shared local models for general utility.** Use diverse evaluation tasks, neutral prompts, and sealed holdouts. Never encode benchmark answers, rubric fields, named test cases, or product-specific workflows in shared model prompts or runtime messages. Specialized behavior belongs in a separate deployment or profile.

## Scope

Carter wants this agent framed as a **homelab assistant and general personal assistant**, not narrowly as a coding tool. Software engineering is a large part of the work, but non-code help (planning, notes, research, life admin, digests, correspondence drafting, scheduling) is equally in scope and should be treated as first-class.

## Overview

Single-node homelab: Ubuntu Server on a ThinkPad L14 Gen 3 (16GB RAM, 512GB NVMe), k3s routing via Traefik to apps in Docker Compose.

## Key Practice

Use `notes/` as a knowledge base. You will see this referenced throughout this AGENTS.md.

## Repository Structure

Home directory managed as a bare git repo for dotfiles. Key dirs:
- `blog/` — Rails 8 deploy wrapper (app nested within)
- `beatz/` — public beat archive deployment checkout
- `beatz-selected/` — read-only audio, starter-selection, and artwork library mounted by the beatz service (not Git-tracked)
- `homelab-backup/` — Go backup service
- `k3s/` — Kubernetes manifests
- `dev/` — Scratch space for cloned repos, tests, development
- `scripts/` — Digest + steward orchestrators
- `notes/` — Agent-maintained knowledge vault (`docs/` for maintained ref, `logs/sessions/` for session history, `journal/` for research/records)
- `digests/` / `backups/` — Automated output archives
- `ideas/` — Unstructured ideas (not maintained)
- `.dotfiles-homelab/` — Bare git repo tracking dotfiles
## Dev Workflow (`dev/`)

**Hard rule:** Always develop in `~/dev/<repo>/`. Never edit files in prod deploy folders such as `/home/carter/blog/` — those are deployment artifacts only. If a dev/ clone doesn't exist for a repo, pull a fresh one with `git clone git@github.com:carter2099/<repo>.git ~/dev/<repo>` before making changes.

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

## Slash commands

Always use the `/create-command` command when creating a new user-global slash command. User-global commands are file prompts in `~/.omp/agent/prompts/*.md`; the filename determines the slash-command name. The command handles the required VCS step so the prompt is tracked and survives a homelab storage wipe.

## Dotfiles Management

```bash
# The 'dotfiles' command manages the bare repo
dotfiles status
dotfiles add <file>
dotfiles commit -m "message"
dotfiles push
```

`dotfiles` is a real command at `~/.local/bin/dotfiles` (tracked in the repo itself), so it
works in any shell — no shell alias needed. In headless/bash sessions where `~/.local/bin` is
not on PATH (e.g. agent tool shells), either `export PATH="$HOME/.local/bin:$PATH"` first or
use the raw form: `/usr/bin/git --git-dir="$HOME/.dotfiles-homelab/" --work-tree="$HOME" ...`.

**⚠️ Always use targeted `dotfiles add <path>` — never bare `dotfiles add -A` or `dotfiles add .`.** Since the work-tree is `$HOME`, an unqualified `add -A` would stage everything in `/home/carter/` that isn't gitignored. Scope adds to the specific file(s) being tracked.

```bash
dotfiles add .zshrc                                 # single file
dotfiles add .config/systemd/user/homelab-backup.*  # glob pattern for related files
dotfiles add .omp/agent/prompts/create-command.md       # command-creation prompt
dotfiles add .omp/agent/prompts/hyperliquid-run.md      # scheduled command prompt
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

**Key:** Explicit `flannel-iface: "enp3s0f0"` in `/etc/rancher/k3s/config.yaml` (matches the default route interface). Pod↔host traffic needs `ufw allow in on cni0` + `flannel.1` — if ClusterIPs fail after a reboot/ufw reload, check these first.
## App Details

Each app has a reference doc in `~/notes/docs/homelab/`:

- **Blog** (Rails 8, port 33099) → [`blog.md`](notes/docs/homelab/blog.md)
- **Beatz** (public Go music player branded “Beats” in-app, localhost:30142; no Cloudflare Access; media: `~/beatz-selected/`; play history: `~/beatz-data/plays.jsonl`) → [`beatz.md`](notes/docs/homelab/beatz.md)
- **Hyperliquid SDK maintenance** (automated dependency maintenance; no trading runtime) → [`hyperliquid-sdk.md`](notes/docs/homelab/hyperliquid-sdk.md)
- **Homelab Backup** (Go, daily 03:00 UTC → R2) → [`homelab-backup.md`](notes/docs/homelab/homelab-backup.md)
- **Dependabot Webhook** (Go, localhost:9099) → [`dependabot-webhook.md`](notes/docs/homelab/dependabot-webhook.md)
- **Open WebUI** (chat frontend + native SearXNG + Weather v2, localhost:48100) → [`open-webui.md`](notes/docs/homelab/open-webui.md)
- **OMP Web** (agent web UI, localhost:30141) → [`omp-web.md`](notes/docs/homelab/omp-web.md)
- **SearXNG** (search backend, localhost:8080) → [`searxng.md`](notes/docs/homelab/searxng.md)
- **FreshRSS** (RSS reader, k3s Deployment, freshrss.carter2099.com) → [`k3s.md`](notes/docs/homelab/k3s.md) (covered as third-party k3s service)
- **Cloudflare** (API token, tunnel, DNS) → [`cloudflare.md`](notes/docs/homelab/cloudflare.md)
- **OpenCode Go Proxy** (0.0.0.0:8082, UFW-gated to Docker bridges) — if opencode-go models fail, check this first → [`opencode-go-proxy.md`](notes/docs/homelab/opencode-go-proxy.md)
- **LLM Proxy** (wildcard:8081, UFW-gated to Docker bridges; eight reasoning-enabled local entries) → [`local-llm-gaming-rig.md`](notes/docs/homelab/local-llm-gaming-rig.md)
- **Prompt-Guard Classifier** (localhost:8090) → [`dependabot-webhook.md`](notes/docs/homelab/dependabot-webhook.md)

## Email Digests

Five daily HTML digests (ai-tech, agentic-platform, ai-hardware, gaming, world) at 08:00 UTC via `digests-daily.timer`. Per-topic inference is bounded to two concurrent calls; curation uses a validated proposal + independent critic before deterministic state application and HTML rendering. Full architecture: [`email-digests.md`](notes/docs/homelab/email-digests.md).

## Homelab Steward

Daily maintenance at 5:00 PM ET via `homelab-steward.timer` (`~/scripts/steward_runner.py`). **Safety rules:** never `dist-upgrade`, never `aa-remove-unknown`, Docker engine via apt `--only-upgrade`, assert `DockerRootDir=/var/lib/docker` after upgrade, failures become email badges, never sys.exit mid-run. Full architecture: [`homelab-steward.md`](notes/docs/homelab/homelab-steward.md).

## Agent CLI: omp

The sole agent CLI on this host is **omp** (`@oh-my-pi/pi-coding-agent`, via bun; binary at `~/.bun/bin/omp`, config in `~/.omp/agent/`). Headless runs (`omp -p`) pass `--config ~/.omp/agent/headless-override.yml`. What uses omp, auth/models, remote ops, reboot protocol: [`omp-agent-cli.md`](notes/docs/homelab/omp-agent-cli.md).

## Remote Agent Operations

Carter's omp agent web UI is the **pi-web fork** ("OMP Web", English — his fork of `best-linux-code/pi-web`, the in-progress omp port) at `omp.carter2099.com` → [`omp-web.md`](notes/docs/homelab/omp-web.md). SSH details, `XDG_RUNTIME_DIR`, reboot protocol, `~/agent-state/pending.md` startup check: [`omp-agent-cli.md`](notes/docs/homelab/omp-agent-cli.md).

## Persistent Memory (`~/notes/`)

The `~/notes/` vault is the homelab's long-term knowledge base — a standalone git repo of reference notes, session memoirs, and cross-referenced context.

### For agents

**Before starting work on a known topic**, grep the vault for relevant context:
```bash
rg -l "search term" ~/notes/
```
This is opt-in — only do it when past context would materially help the current task. Don't load entire files into context preemptively.

**After significant sessions**, write a brief session memoir. "Significant" means: architectural decisions, system state changes, or context a future agent would need. Routine checks and quick Q&A don't need one.

### Session memory bank (`~/notes/logs/sessions/`)

The steward (P0b, nightly) maintains this bank from interactive omp sessions — it writes
missing memoirs, judges/updates agent-written ones against the source transcript, and
filter-skips test/dead-end sessions (LLM judge, fail-open). P7 audit workers + judges and
the email TL;DR writer receive recent memoirs as context.

Location + format — one compact `.md` per interactive omp session, in a per-day folder:

```
~/notes/logs/sessions/YYYY-MM-DD/<HHMM>-<slug>.md
```

```markdown
---
title: <short topic>
source: <absolute path to the omp session jsonl — REQUIRED>
session_id: <omp session uuid — REQUIRED>
project: <sessions subdir, e.g. - or -dev>
date: YYYY-MM-DD
---
# Session: YYYY-MM-DD HH:MM — <title>
**Topics:** comma-separated list
**Decisions:**
- decision 1
- decision 2
**State changes:**
- what was modified on the system
**Context for next time:** 1-2 sentences a future agent should know
```

Agents writing a memoir during a session MUST include `source:` and `session_id:` — read
them from the session jsonl header (the `{"type":"session"}` line in
`~/.omp/agent/sessions/<project>/<ts>_<uuid>.jsonl`). That's how the steward matches
sessions and judges existing memoirs. Keep the body compact: the source transcript is the
source of truth, the memoir is the pointer + durable context.

**Session dirs:** interactive omp sessions live in `~/.omp/agent/sessions/<project>/`
(project-scoped subdirs). Headless invocations MUST pass
`--session-dir ~/.omp/agent/sessions-automated/` — the steward's session-memory filter
relies on this separation and misses sessions that leak into the wrong dir.

Session memoirs are NOT formal notes — don't use `/note-save` or full frontmatter for them.
They're quick context dumps for cross-session continuity. Formal reference notes use
`/note-save` when the user explicitly asks.

### Vault structure

- `~/notes/INDEX.md` — index of all formal reference notes (maintained by `/note-save`)
- `~/notes/docs/` — maintained reference docs (subsystem architecture and runbooks)
- `~/notes/logs/sessions/` — session memory bank (YYYY-MM-DD/ folders, one compact .md per interactive omp session, frontmatter `source:` pointer; maintained by the steward P0b)
- `~/notes/journal/` — research notes and project records (not maintained)
- The vault is a standalone git repo (not the dotfiles bare repo) — `/note-save` handles commits

## Gaming Rig (Linux AI / Windows gaming)

Dual-boot rig at `192.168.4.103`: Ubuntu Server 24.04.4 LTS on the 250 GB SATA SSD is the
intended always-on AI OS; Windows 11 remains on the 2 TB NVMe for one-shot gaming boots.
Use `ssh gamingrig-linux` (or `ssh gamingrig`) for Ubuntu and `ssh gamingrig-windows` for
Windows. Both aliases pin distinct host keys for the shared IP.

Linux-primary migration completed 2026-08-17: signed NVIDIA 595.71.05 + CUDA 13.2,
llama.cpp b10453, and llama-swap v250 serve five retained IDs continuously under systemd:
Qwen 3.8 27B IQ2, Ornith 35B Q8, Ornith 9B Q6, Gemma 4 12B Q6, and Gemma 4 26B Q8.
All five passed Linux behavioral serving checks. Post-migration autoresearch completed
2026-08-18. Current reasoning budgets in the same order are 768/256/544/112/384; Qwen now
uses an 81,920-token full-GPU profile and Gemma 26B uses physical batch 256. Direct
post-deploy smokes all emitted reasoning and exceeded 30 decode tokens/s.
The proxy reports explicit Linux/Windows/offline state and keeps cloud fallback; the
loopback dashboard at `127.0.0.1:30143` behaviorally passed Linux→Windows and
sleeping-Windows→Linux transitions. Both OSes re-arm Ubuntu one-shot boot selection.
Windows retains only seven verified GGUF backups; 21 retired models and the obsolete
Windows inference stack were removed. Linux now mirrors the ThinkPad's agent privilege
model: `carte` has unrestricted passwordless sudo through a root-owned policy. The
dashboard is published at `rig.carter2099.com` through Cloudflare Access; unauthenticated
GET and POST requests were verified to redirect to the Access login while the origin
remains loopback-only. Full runbook:
[`local-llm-gaming-rig.md`](notes/docs/homelab/local-llm-gaming-rig.md).

## Environment

Shell zsh (vim bindings), nvim, rbenv, fnm, tmux (Ctrl+Space), git carter2099, `gh` authed: [`environment.md`](notes/docs/homelab/environment.md). **Client topology:** Carter develops from a Mac — `/Users/carterbrown/...` paths are NOT reachable from this session (see doc for details).

## Where the deep dives live

Verbose architecture for subsystems an agent only needs when actively working on them. These are in `~/notes/docs/homelab/` and `~/notes/journal/` (standalone vault repo, grepped on-demand):

- [`hardware.md`](notes/docs/homelab/hardware.md) — hardware specs, network config
- [`local-llm-gaming-rig.md`](notes/docs/homelab/local-llm-gaming-rig.md) — llm-proxy / llama-swap topology, models, env vars, troubleshooting
- [`omp-agent-cli.md`](notes/docs/homelab/omp-agent-cli.md) — omp CLI facts, what uses omp, auth/models, remote ops, reboot protocol
- [`environment.md`](notes/docs/homelab/environment.md) — shell/editor tooling, git/gh, client topology
- [`deployment.md`](notes/docs/homelab/deployment.md) — deploy flow, port-in-use, exit 255, aa-remove-unknown
- [`k3s.md`](notes/docs/homelab/k3s.md) — k3s architecture, flannel, CNI ufw rules; also covers FreshRSS as a third-party k3s deployment
- [`email-digests.md`](notes/docs/homelab/email-digests.md) — 9-phase digest workflow, stories-in-flight, audit/debug
- [`homelab-steward.md`](notes/docs/homelab/homelab-steward.md) — steward phases, work queue, executor, budget guard, debugging
- [`homelab-backup.md`](notes/docs/homelab/homelab-backup.md) — 22-target taxonomy, pre-collection, verify/latest/list subcommands, restore drill, retention, notify/debug
- [`blog.md`](notes/docs/homelab/blog.md) — Rails 8 blog app
- [`beatz.md`](notes/docs/homelab/beatz.md) — public beat archive player, starter/artwork pools, media library, deploy/runbook
- [`hyperliquid-sdk.md`](notes/docs/homelab/hyperliquid-sdk.md) — automated Hyperliquid SDK maintenance
- [`dependabot-webhook.md`](notes/docs/homelab/dependabot-webhook.md) — Go webhook + Prompt-Guard classifier
- [`open-webui.md`](notes/docs/homelab/open-webui.md) — chat frontend, native SearXNG, Weather v2
- [`omp-web.md`](notes/docs/homelab/omp-web.md) — agent web UI, next.js build quirks
- [`searxng.md`](notes/docs/homelab/searxng.md) — metasearch backend, config
- [`cloudflare.md`](notes/docs/homelab/cloudflare.md) — API token, tunnel ingress, DNS
- [`opencode-go-proxy.md`](notes/docs/homelab/opencode-go-proxy.md) — dual-account proxy, ufw bridge rules, cookie expiry

`journal/` contains research notes and project records (not maintained). `logs/sessions/` contains chronological session memoirs.

Grep the vault (`rg -l "term" ~/notes/`) before starting work on a known topic; the `~/notes/INDEX.md` lists all formal notes.
