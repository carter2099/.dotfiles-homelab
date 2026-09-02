# AGENTS.md

This file provides guidance to omp agents when working on this homelab.

**Maintenance:** Keep this file up to date. When deploying a new app, adding a service, changing ports/IPs, or making any structural changes to the homelab, update the relevant sections here as part of that work. Deep-dive architecture for subsystems lives in `~/notes/docs/homelab/` and `~/notes/journal/` (see "Where the deep dives live" at the bottom) — keep AGENTS.md as the always-loaded operational reference. The ThinkPad remains the sole notes, documentation, infrastructure, and production authority; the gaming rig's development boundary is documented in the linked homelab notes.

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

Two-host homelab: the ThinkPad L14 Gen 3 runs the primary Ubuntu services, k3s/Traefik ingress, Docker Compose apps, and systemd automation; the dual-boot gaming rig provides Linux AI inference, a focused development center, and Windows gaming.

## Key Practice

Use `notes/` as a knowledge base. You will see this referenced throughout this AGENTS.md.

## Repository Structure

Home directory managed as a bare git repo for dotfiles. Key dirs:
- `blog/` — Rails 8 production checkout/wrappers (canonical source is `~/dev/blog/`)
- `beatz/` — public beat archive deployment checkout
- `beatz-selected/` — read-only audio, starter-selection, and artwork library mounted by the beatz service (not Git-tracked)
- `homelab-backup/` — Go backup service
- `k3s/` — Kubernetes manifests
- `dev/` — ThinkPad scratch space for cloned repos, tests, and development; gamingrig-linux uses `/home/carte/dev/<repo>` instead
- `news/` — Daily News nginx deployment configuration and static assets
- `scripts/` — Daily News and steward entrypoints, bounded packages, and verification pipelines
- `notes/` — Agent-maintained knowledge vault (`docs/` for maintained ref, `logs/sessions/` for session history, `journal/` for research/records)
- `digests/` / `backups/` — Automated output archives; `digests/news/{publications,attention,mail}/` is durable Daily News state
- `ideas/` — Unstructured ideas (not maintained)
- `.dotfiles-homelab/` — Bare git repo tracking dotfiles
- `.config/nvim/` — live Neovim config and canonical standalone Git repository; ignored by the parent bare repo
## Dev Workflow (`dev/`)

**Hard rule:** On the ThinkPad, always develop application code in `~/dev/<repo>/`. Never edit files in production deploy folders such as `/home/carter/blog/` or `/home/carter/homelab-backup/`; those are deployment artifacts only. If a ThinkPad dev clone does not exist, clone `git@github.com:carter2099/<repo>.git` into `~/dev/<repo>` before changing it. **Explicit exception:** `~/.config/nvim/` is intentionally both the live config and its own canonical standalone Git repository; edit, commit, and push it in place. It has no deploy step and the parent dotfiles repo ignores it.

The gaming rig is a separate development center: work only in `/home/carte/dev/<repo>`, use GitHub as the normal code-transfer boundary, and never deploy production from the rig. Do not place notes, production deploy trees, k3s manifests/kubeconfig, or homelab application state there. The only exception is the steward-managed `/home/carte/src/llama.cpp*` serving-build workspace; it is not a general development root and must remain outside normal project work. See [`local-llm-gaming-rig.md`](notes/docs/homelab/local-llm-gaming-rig.md) and [`environment.md`](notes/docs/homelab/environment.md) for the boundary and installed conventions.

The ThinkPad `dev/` directory is for cloning GitHub repos (via SSH: `git@github.com:carter2099/<repo>.git`), running their test suites, making changes, and pushing back. It is **not** tracked by the dotfiles bare repo.

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

User-global commands are file prompts in `~/.omp/agent/prompts/*.md`; the filename determines the slash-command name.

Make sure to track in VCS when adding slash commands.

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
dotfiles add .config/systemd/user/homelab-steward.* # canonical infrastructure units
dotfiles add .omp/agent/prompts/create-command.md       # command-creation prompt
dotfiles add .omp/agent/prompts/hyperliquid-run.md      # scheduled command prompt
```

The steward's P9b dotfiles phase treats recent unclosed interactive OMP sessions as in-flight ownership evidence. It must not stage overlapping paths. A failed post-commit push may be retried only for the exact recorded branch and commit OID; divergence requires inspection.

## App Deployment Pattern

Detailed deploy runbook at [`~/notes/docs/homelab/deployment.md`](notes/docs/homelab/deployment.md).

**Critical rules (every deploy):**
- **Commit before deploy.** Deployed state normally matches `origin/main`; check `git status` first.
  **Explicit exception:** Steward P1 may mutate and deploy tracked managed-version pins before P9b attempts to commit/push them; even failed or dirty pins can persist if later gates fail, so reconcile against live health and Git immediately. No other dirty-tree deploy is allowed.
- **Orphaned docker-proxy.** Container exit 255 can leave `docker-proxy` holding the port. Fix: `sudo kill <proxy-pid>`, `docker rm <container>`, `bash up.sh`.
- **"Missing feature" = check cache first.** Cloudflare serves stale HTML if origin is down. `curl` the origin before debugging code.
- **Exit 255 is intermittent.** Restart with existing image; don't rebuild.
- **Never run `sudo aa-remove-unknown`.** Can delete AppArmor profiles Docker/containerd depend on.
## Kubernetes (k3s)

`k` is aliased to `kubectl`. Full reference at [`~/notes/docs/homelab/k3s.md`](notes/docs/homelab/k3s.md).

**Key:** Explicit `flannel-iface: "enp3s0f0"` in `/etc/rancher/k3s/config.yaml` (matches the default route interface). Pod↔host traffic needs `ufw allow in on cni0` + `flannel.1` — if ClusterIPs fail after a reboot/ufw reload, check these first.
## App Details

Each app has a reference doc in `~/notes/docs/homelab/`:

- **Blog** (canonical `~/dev/blog/`; transactional deploy via `deploy/deploy.sh`; production checkout `~/blog/blog/`; Docker on port 33099; public blog.carter2099.com served via k3s Traefik ingress — tunnel origin is 127.0.0.1:80, not 33099) → [`blog.md`](notes/docs/homelab/blog.md)
- **Beatz** (public Go music player branded “Beats” in-app, localhost:30142; no Cloudflare Access; media: `~/beatz-selected/`; play history: `~/beatz-data/plays.jsonl`) → [`beatz.md`](notes/docs/homelab/beatz.md)
- **Daily News** (public static newspaper UI, localhost:30144, news.carter2099.com; bounded `~/scripts/daily_news/` package; per-run validated SQLite workflow state; priority-ranked front page + five category pages, historical editions, one summary email, durable data in R2 backup) → [`email-digests.md`](notes/docs/homelab/email-digests.md)
- **Hyperliquid SDK maintenance** (scheduled upstream API + dependency maintenance; Dependabot PR metadata is deterministically collected, Prompt-Guard-classified, and reconciled into the regular maintenance queue before later bounded processing; verification: `verify-dependabot-intake.sh full` and `verify-hyperliquid-guard.sh full`; no trading runtime) → [`hyperliquid-sdk.md`](notes/docs/homelab/hyperliquid-sdk.md)
- **Homelab Backup** (canonical `~/dev/homelab-backup/`; transactional `release.sh`; 23 exact manifest targets; daily 03:00 UTC → R2; full verifier `verify.sh full`) → [`homelab-backup.md`](notes/docs/homelab/homelab-backup.md)
- **Dependabot Webhook** (Go, localhost:9099) → [`dependabot-webhook.md`](notes/docs/homelab/dependabot-webhook.md)
- **Open WebUI** (chat frontend + native SearXNG + Weather v2, localhost:48100) → [`open-webui.md`](notes/docs/homelab/open-webui.md)
- **Herdr Web Client** (browser product title **Herdr Web**; mobile-first attachment to the live Herdr server; `herdr-web-client.service`, loopback-only on 30145 at remote.carter2099.com; Cloudflare Access is the production authentication boundary and the origin has no authentication; one hardened transient browser attachment; background completion toast/chime; Kitty Shift+Enter newline) → [`omp-agent-cli.md`](notes/docs/homelab/omp-agent-cli.md)
- **SearXNG** (search backend, localhost:8080) → [`searxng.md`](notes/docs/homelab/searxng.md)
- **FreshRSS** (RSS reader, k3s Deployment, freshrss.carter2099.com) → [`k3s.md`](notes/docs/homelab/k3s.md) (covered as third-party k3s service)
- **Cloudflare** (API token, tunnel, DNS) → [`cloudflare.md`](notes/docs/homelab/cloudflare.md)
- **OpenCode Go Proxy** (0.0.0.0:8082, UFW-gated to Docker bridges; quota routing from the authenticated OpenCode usage API, API keys only). The optional direct Zen free-model path is controlled by `free_endpoint_enabled`; it is currently `false`, so all requests go directly through `/zen/go`. `/health` reports the active setting. If opencode-go models fail, check this first → [`opencode-go-proxy.md`](notes/docs/homelab/opencode-go-proxy.md)
- **LLM Proxy** (canonical `~/dev/llm-proxy/`; transactional `release.sh`; wildcard:8081 with no client auth, so release requires active default-deny UFW and bridge-only 8081 rules; five reasoning-enabled local entries) → [`local-llm-gaming-rig.md`](notes/docs/homelab/local-llm-gaming-rig.md)
- **Prompt-Guard Classifier** (canonical `~/dev/prompt-guard/`; immutable model revision and release runtime; transactional `deploy.sh`; localhost:8090) → [`dependabot-webhook.md`](notes/docs/homelab/dependabot-webhook.md)

## Daily News Digests

Five category pipelines run at 08:00 UTC via `digests-daily.timer`, publish a priority-ranked front page plus separate sections at `news.carter2099.com`, and send one summary email. Editorial significance measures consequence only: `high` requires structured, source-grounded impact evidence, and routine maintenance/deprecations without demonstrated broad impact are downgraded. GDELT supplies observed attention; valid no-match queries score zero attention, while provider/query failure uses confidence 0 and editorial-only priority. GDELT API availability and query hit-rate are monitored per attention run in `~/digests/.gdelt-health.log`; sustained degradation (windowed `availability_rate` < 0.6 or `hit_rate` < 0.3) is logged and reported via `rec: warn` rather than gating publication. Deterministic ties use prominence, attention, confidence, scope, significance, date, title, and URL—never discovery order. The front page guarantees one curated lead per section, then fills remaining slots globally at priority ≥65 (max 10). Standfirsts use complete newspaper prose. Published story URLs are normalized to canonical reader-facing publisher domains (e.g. NYT sample hosts such as `monorepo-sample1.nyt.net` map to `www.nytimes.com`). Durable state under `~/digests/news/` is backed up as `daily-news-data`; the public origin is loopback-only `carter-news` on 30144. **Developing and Ongoing** requires high validated significance plus developments on at least two dates. Full architecture: [`email-digests.md`](notes/docs/homelab/email-digests.md).
GDELT event terms are queried as explicit `OR` alternatives. Requests are separated by 10 seconds; 429, 5xx, timeout, and transport failures get one bounded `Retry-After`/exponential-backoff retry, and the cooldown carries into the next candidate.
Reader-facing headlines are always English: Phase 4 preserves English headlines and faithfully translates non-English source headlines. Category pages start with Latest; standfirsts remain metadata/email copy and are not repeated as an In Brief block.
DeepSeek Flash handles primary synthesis and a separate critic pass; Mimo v2.5 is the API fallback.
Daily News alone uses an explicit Codex-primary, SearXNG-fallback OMP `web_search` chain via `~/.omp/agent/daily-news-headless.yml`; the shared `~/.omp/agent/headless-override.yml` remains provider-neutral for steward, Hyperliquid, Dependabot, and ad hoc headless runs. SearXNG fallback health is monitored with `categories=news&time_range=day` but does not block successful primary-provider research. An unfiltered general search is not sufficient because the working general engines do not currently honor the day filter.
Phase 1 records search evidence without opening articles; Phase 4 verifies queued sources with the public-HTTPS `read` tool. `digest_runner.py --test` routes mutable caches, attention observations, and search-health logs under `~/digests/test/`, never their production paths.
Each topic run records attempt-owned, hash-validated phase state in `workflow-state.sqlite3`; source/policy mutation aborts at phase boundaries, and stateful publications are accepted only when Phase 8's recorded path/hash/schema match. Run `bash ~/scripts/verify-daily-news.sh full` after changes.

`run_all_digests.sh` runs `digest_runner.py --preflight` before any research; missing load-bearing constants/contracts must fail immediately. If an edition is absent, inspect `systemctl --user status digests-daily.service`, then `journalctl --user -u digests-daily.service` and `~/digests/.digests.log`. The 2026-08-27 missed edition was a code regression (`CROSS_DAY_DEDUP_DAYS` removed by an automated audit fix), not an OpenCode subscription failure; 429s on the optional Zen free-model endpoint caused extra fallthrough attempts but did not indicate exhausted Go quota.

## Homelab Steward

Daily maintenance at 1:00 AM ET via `homelab-steward.timer` (`~/scripts/steward_runner.py`) on the ThinkPad. Its deterministic remote branch connects only through pinned `gamingrig-linux`: Linux runs the approved apt, Herdr, and OMP maintenance with smoke/rollback and health gates; a Windows skip requires trusted local `llm-proxy` `/health` corroboration (`rig_os=windows`), while offline skip is limited to recognized timeout/refusal/no-route/unreachable transport failures; auth/config/unknown/host-key failures are failures. A required reboot re-arms Linux BootNext, requires a changed boot ID, and polls bounded full readiness/health before reporting reboot, return, or recheck. The steward is never installed on the rig. SearXNG and Linux llama.cpp releases auto-deploy only after a 7-day upstream soak and attempt rollback when post-update checks fail. The llama helper reports `ROLLBACK_FAILED` when its own recovery validation fails; any failed recovery requires manual inspection. **Safety rules:** never `dist-upgrade` or `aa-remove-unknown`; upgrade Docker only through apt, never manual binary replacement; assert `DockerRootDir=/var/lib/docker` after Docker upgrades; record failures in durable artifacts and email badges rather than aborting later reporting.
The executable is a thin entrypoint over `~/scripts/steward/`; each run uses attempt-owned, hash-validated SQLite workflow state and a fixed startup source/policy fingerprint. P1 nested failure packets remain retryable failed state while independent phases continue; P8 SMTP errors fail the phase. Run `bash ~/scripts/verify-steward.sh full` after changes.

## Agent CLI: omp

The ThinkPad's sole agent CLI is **omp** (`@oh-my-pi/pi-coding-agent`, via bun; binary at `~/.bun/bin/omp`, config in `~/.omp/agent/`). The gaming rig also runs the package via Bun for rig-local development, with rig-local OMP state and the safe `omp --allow-home` wrapper; never copy ThinkPad OMP state or credentials. Headless ThinkPad runs (`omp -p`) normally pass `--config ~/.omp/agent/headless-override.yml`; Daily News tool calls use `daily-news-headless.yml`, which preserves the same advisor-off settings while isolating its search-provider chain. What uses omp, auth/models, remote ops, reboot protocol: [`omp-agent-cli.md`](notes/docs/homelab/omp-agent-cli.md).

## Remote Agent Operations

Carter's browser attachment is **Herdr Web Client** (browser title **Herdr Web**) at `remote.carter2099.com`; source is `github.com/carter2099/herdr-web-client` in `~/dev/herdr-web-client`, and production uses `herdr-web-client.service`. It connects to the live Herdr server instead of maintaining a separate web-owned OMP session store. OMP Web and `omp.carter2099.com` were retired on 2026-08-31. SSH details, `XDG_RUNTIME_DIR`, reboot protocol, `~/agent-state/pending.md` startup check: [`omp-agent-cli.md`](notes/docs/homelab/omp-agent-cli.md)

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

## Gaming Rig (Linux inference + focused development / Windows gaming)

The dual-boot rig is the Linux inference/development host and Windows gaming machine. The
ThinkPad remains the sole notes, documentation, infrastructure, and production authority:
develop under `/home/carte/dev/<repo>`, transfer code through GitHub, and never deploy
production, copy authoritative state, or extend k3s to the rig.

- [`local-llm-gaming-rig.md`](notes/docs/homelab/local-llm-gaming-rig.md) — host topology,
  inference/models, proxy/dashboard, Windows/Apollo driver constraints, boot switching,
  steward maintenance, and recovery.
- [`environment.md`](notes/docs/homelab/environment.md) — development/tooling conventions,
  canonical configuration map, SSH/trust boundary, serving-build exception, and state
  exclusions.

## Environment

ThinkPad shell zsh (vim bindings), nvim, rbenv, fnm, tmux (Ctrl+Space), git carter2099, `gh`
authed: [`environment.md`](notes/docs/homelab/environment.md). The rig's shell/tooling,
OMP/Herdr, SSH, and state exclusions are documented there too. **Client topology:** Carter
develops from a Mac — `/Users/carterbrown/...` paths are NOT reachable from this session.

## Where the deep dives live

Verbose architecture for subsystems an agent only needs when actively working on them. These are in `~/notes/docs/homelab/` and `~/notes/journal/` (standalone vault repo, grepped on-demand):

- [`hardware.md`](notes/docs/homelab/hardware.md) — hardware specs, network config
- [`local-llm-gaming-rig.md`](notes/docs/homelab/local-llm-gaming-rig.md) — llm-proxy / llama-swap topology, models, env vars, troubleshooting
- [`omp-agent-cli.md`](notes/docs/homelab/omp-agent-cli.md) — omp CLI facts, what uses omp, auth/models, remote ops, reboot protocol
- [`environment.md`](notes/docs/homelab/environment.md) — shell/editor tooling, git/gh, client topology
- [`deployment.md`](notes/docs/homelab/deployment.md) — deploy flow, port-in-use, exit 255, aa-remove-unknown
- [`k3s.md`](notes/docs/homelab/k3s.md) — k3s architecture, flannel, CNI ufw rules; also covers FreshRSS as a third-party k3s deployment
- [`email-digests.md`](notes/docs/homelab/email-digests.md) — Daily News significance/attention/priority scoring, front page, standfirsts, R2 backup, delivery, audit/debug
- [`homelab-steward.md`](notes/docs/homelab/homelab-steward.md) — steward phases, session memory, audit/fix loop, work queue, and debugging
- [`homelab-backup.md`](notes/docs/homelab/homelab-backup.md) — 23-target manifest taxonomy, pre-collection, strict verify/latest/list, restore drill, retention, release, notify/debug
- [`blog.md`](notes/docs/homelab/blog.md) — Rails 8 blog app
- [`beatz.md`](notes/docs/homelab/beatz.md) — public beat archive player, starter/artwork pools, media library, deploy/runbook
- [`hyperliquid-sdk.md`](notes/docs/homelab/hyperliquid-sdk.md) — automated Hyperliquid SDK maintenance
- [`dependabot-webhook.md`](notes/docs/homelab/dependabot-webhook.md) — Go webhook + Prompt-Guard classifier
- [`open-webui.md`](notes/docs/homelab/open-webui.md) — chat frontend, native SearXNG, Weather v2
- [`searxng.md`](notes/docs/homelab/searxng.md) — metasearch backend, config
- [`cloudflare.md`](notes/docs/homelab/cloudflare.md) — API token, tunnel ingress, DNS
- [`opencode-go-proxy.md`](notes/docs/homelab/opencode-go-proxy.md) — multi-account usage API routing, ufw bridge rules

`journal/` contains research notes and project records (not maintained). `logs/sessions/` contains chronological session memoirs.

Grep the vault (`rg -l "term" ~/notes/`) before starting work on a known topic; the `~/notes/INDEX.md` lists all formal notes.

Blast radius: after making changes to any code or functionality, anywhere, ask yourself: What else could these changes have broken? Did the blast radius hit anything we did not verify or test?
