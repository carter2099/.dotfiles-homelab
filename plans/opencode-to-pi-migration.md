# OpenCode → Pi Agent Migration Plan

**Created:** 2026-06-15 | **Status:** Planning

## Migration Record

All migration artifacts — decision log, runbook, test results, and lessons learned — live in `~/docs/pi-migration/`. This exists so future migrations (Pi → whatever comes next) have a reference for what worked, what didn't, and how we verified each step.

| File | Purpose |
|---|---|
| `~/docs/pi-migration/runbook.md` | Chronological log of every action taken, test result, and decision |
| `~/docs/pi-migration/lessons.md` | What went well, what surprised us, what we'd do differently |
| `~/plans/opencode-to-pi-migration.md` | This file — the plan itself |

Every time a phase step is executed, append to the runbook with: timestamp, what was done, verification results (pass/fail with evidence), and any decisions made.

## Goal

Replace all OpenCode agent usage in the homelab with Pi agents. Components:
1. Email digests (4× daily systemd timers)
2. Hyperliquid SDK maintenance (daily systemd timer)
3. Dependabot webhook (Go service + spawned agent with permission sandbox)
4. `opencode web` (remote web UI via CF tunnel + CF Access)
5. Interactive SSH CLI (already done — this session is Pi)

## Principles

- **Verify at every step.** No bulk migration — each component is migrated, tested, and confirmed live before the next starts.
- **Parallel run where possible.** Old opencode infrastructure stays up until its Pi replacement is verified.
- **Manual before automated.** Every script is tested manually before the timer/service is enabled.
- **Rollback is cheap.** Each step includes explicit rollback instructions.
- **Verification is concrete.** "It works" isn't a verification — specific artifacts, exit codes, and observable outputs are.

---

## Phase 0: Prerequisites (COMPLETE)

- [x] Pi installed (v0.79.4)
- [x] OpenCode Go auth configured (`~/.pi/agent/auth.json`)
- [x] `@juicesharp/rpiv-web-tools` installed (provides `web_search` + `web_fetch`)
- [x] Models confirmed available: `opencode-go/deepseek-v4-flash`, `opencode-go/deepseek-v4-pro`, `opencode-go/minimax-m3`, `opencode-go/qwen3.7-max`, `opencode-go/qwen3.7-plus`
- [x] Interactive SSH CLI working (`pi` in terminal)

---

## Phase 0.5: Artifact Audit & Migration

Before migrating any automation, inventory and move all opencode artifacts so Pi has equivalent context. This mirrors what was done during the Claude → OpenCode migration (documented at `~/.config/opencode/rules/opencode-agent-migration.md` — 16 skills ported from `~/.claude/skills/` to `~/.config/opencode/skills/`, CLAUDE.md replaced with AGENTS.md).

### Artifact inventory

| Artifact | Path | Disposition |
|---|---|---|
| **Skills (16)** | `~/.config/opencode/skills/` | **Migrate** to `~/.pi/agent/skills/` |
| **Rules / Memory** | `~/.config/opencode/rules/MEMORY.md` + 9 `.md` files | **Fold** missing content into `~/AGENTS.md`; archive rest |
| **Config** | `~/.config/opencode/opencode.jsonc` | **Discard** (just pointed to rules dir) |
| **Binary + npm deps** | `~/.opencode/` | **Archive** to tarball, then delete (Phase 5) |
| **Sessions DB** | `~/.local/share/opencode/opencode.db` (87MB) | **Archive** (not migratable to Pi's JSONL format) |
| **Auth** | `~/.local/share/opencode/auth.json` | **Already migrated** (same key in `~/.pi/agent/auth.json` under `opencode-go`) |
| **Tool output cache** | `~/.local/share/opencode/tool-output/` | **Discard** (ephemeral) |
| **Logs** | `~/.local/share/opencode/log/` | **Discard** (rotating) |
| **Snapshot** | `~/.local/share/opencode/snapshot/` | **Archive** |
| **Dependabot sandbox** | `~/.config/dependabot-webhook/opencode.json` | **Archive** (replaced by `pi-sandbox.ts` in Phase 3) |
| **opencode-homelab env** | `~/.config/opencode-homelab/env` | **Delete** after pi-web verified (Phase 4) |
| **opencode-homelab service** | `~/.config/systemd/user/opencode-homelab.service` | **Disable + delete** after pi-web verified (Phase 4) |

### 0.5.1 Migrate skills

Skills follow the [Agent Skills standard](https://agentskills.io) so they're compatible with Pi. Copy all 16:

```bash
cp -r ~/.config/opencode/skills/* ~/.pi/agent/skills/
```

**Verification:**
1. [ ] `ls ~/.pi/agent/skills/` shows all 16 skill directories
2. [ ] Each has a `SKILL.md`
3. [ ] Restart pi: `/reload` (or restart session)
4. [ ] `/skill:homelab-reboot` expands correctly
5. [ ] `/skill:hyperliquid-run` expands correctly

**Skill audit for opencode-specific references — check each SKILL.md for:**
- References to `opencode` binary or flags → update to Pi equivalents
- References to `~/.config/opencode/skills/` paths → update to `~/.pi/agent/skills/`
- References to `CLAUDE.md` → already updated to `AGENTS.md` (done during Claude→OpenCode migration)

Skills that likely need updates:
- `create-skill` — writes to `~/.config/opencode/skills/`, needs to target `~/.pi/agent/skills/`
- `email-digest` — may reference opencode tool names (Read/Bash/WebFetch)
- `hyperliquid-run` — references `opencode run -m` in its prompt
- `dependabot-release` — references `opencode.json` config

### 0.5.2 Migrate rules / memory to AGENTS.md

The opencode rules directory (`~/.config/opencode/rules/`) contained 10 Markdown files loaded as system instructions via `opencode.jsonc`'s `instructions` field. Pi loads `AGENTS.md` from the working directory + parent directories instead.

**Gap analysis — what AGENTS.md already covers vs. what it doesn't:**

| Rule file | Already in AGENTS.md? | Action |
|---|---|---|
| `user_endler_tenets.md` | ✅ "Working principles (Endler tenets)" section | Covered |
| `dev_topology.md` | ❌ No mention of Mac/SSH topology | **Fold in** — add note that user SSHs from a Mac and `/Users/...` paths are unreachable |
| `digest_archives.md` | ✅ "Email Digests" section | Covered (but rules version has more detail on retrieval workflow — fold in if useful) |
| `notes_vault.md` | ✅ Mentioned under "Repository Structure" | Covered (but rules version has INDEX.md sync, commit-and-push, retrieval order — fold in if those are still current) |
| `user_assistant_framing.md` | ❌ AGENTS.md frames as "coding agent harness" | **Fold in** — the user wants non-code work treated as first-class |
| `feedback_commit_before_deploy.md` | ❌ Not mentioned | **Fold in** — critical deploy discipline |
| `feedback_create_skill.md` | ❌ Not mentioned | **Fold in** — skill creation via `/skill:create-skill`, not ad-hoc file writes |
| `feedback_no_aa_remove_unknown.md` | ❌ Not mentioned | **Fold in** — critical system admin warning (breaks Docker) |
| `MEMORY.md` | N/A (index of other rules) | **Discard** — just an index; content captured in the rules themselves |
| `opencode-agent-migration.md` | N/A (historical record) | **Archive** — move to `~/docs/pi-migration/prior-migration.md` as reference |

**Verification:**
1. [ ] `~/AGENTS.md` contains all 4 missing rules (dev topology, assistant framing, commit-before-deploy, create-skill, aa-remove-unknown)
2. [ ] `~/docs/pi-migration/prior-migration.md` contains the full opencode-agent-migration.md content
3. [ ] Old rules directory still intact on disk (not deleted — archived later in Phase 5)

### 0.5.3 Archive opencode artifacts

Create a timestamped tarball for posterity (keeps the Claude→OpenCode migration record chain intact):

```bash
mkdir -p ~/docs/pi-migration/archives
tar czf ~/docs/pi-migration/archives/opencode-artifacts-$(date +%Y%m%d).tar.gz \
  ~/.config/opencode/ \
  ~/.config/opencode-homelab/ \
  ~/.config/dependabot-webhook/opencode.json \
  ~/.local/share/opencode/auth.json \
  ~/.local/share/opencode/snapshot/
```

**Verification:**
1. [ ] Tarball exists and is non-zero
2. [ ] `tar tzf ~/docs/pi-migration/archives/opencode-artifacts-*.tar.gz | head -20` lists expected files

**Do NOT delete the originals yet** — that happens in Phase 5 after all components are verified live.

### 0.5.4 Track the migrated files

Skills and AGENTS.md changes are tracked via dotfiles:

```bash
dotfiles add -A ~/.pi/agent/skills/
dotfiles add ~/AGENTS.md
dotfiles add ~/docs/pi-migration/
dotfiles add ~/plans/opencode-to-pi-migration.md
dotfiles commit -m "pi migration: migrate skills from opencode, update AGENTS.md with missing rules, add migration docs"
dotfiles push
```

**Verification:**
1. [ ] `dotfiles status` is clean
2. [ ] `dotfiles log --oneline -1` shows the commit

---

## Phase 1: Email Digest Migration

**Risk:** Low. Each digest runs as a systemd oneshot timer. We migrate one digest at a time, verify, then proceed.

### 1.1 AI & Tech Digest (pilot)

This is the simplest digest — single recipient, straightforward topic. Use it to prove the pattern.

#### Script changes

Current (`~/scripts/run_ai_tech_digest.sh`):
```bash
exec "$HOME/.opencode/bin/opencode" run -m opencode-go/deepseek-v4-flash "$PROMPT" < /dev/null
```

Replace with:
```bash
exec pi -p --model opencode-go/deepseek-v4-pro "$PROMPT"
```

Also update the prompt text:
- Remove "you do NOT have a web search tool" (Pi has `web_search` via rpiv-web-tools)
- Change "WebFetch tool" references to "web_fetch tool" (the actual Pi tool name)
- Change "Read tool" → "read tool", "Bash tool" → "bash tool"
- Remove the "writes outside /home/carter are blocked in headless mode" warning (Pi print mode has no such restriction)
- Add a note that `web_search` (for finding links) and `web_fetch` (for reading pages) are both available

**Verification — Manual run:**

```bash
cd ~
export XDG_RUNTIME_DIR=/run/user/$(id -u)
bash ~/scripts/run_ai_tech_digest.sh
```

**Verify:**
1. [ ] Script exits with code 0
2. [ ] `~/digests/ai-tech/$(date +%Y-%m-%d).md` exists and contains a summary
3. [ ] Email arrives at carter2099@pm.me (check inbox)
4. [ ] **Quality check:** Spot-check 3 story URLs — do they exist? Are they from today?
5. [ ] **Dedup check:** Read the summary `.md`. Are there stories that were also in yesterday's digest? If yes, does the "Recent & Relevant" section justify them with new developments?

**Rollback:** Revert the script to the opencode exec line. The old `run_ai_tech_digest.sh` is in git history.

#### Enable timer

Once manual run passes all checks:

```bash
systemctl --user start ai-tech-digest.service   # run once on-demand
systemctl --user status ai-tech-digest.service --no-pager -l
```

After confirming the on-demand run works:
```bash
systemctl --user enable --now ai-tech-digest.timer  # if not already enabled
```

Wait for the next scheduled fire (15:00 UTC) and verify the email arrives.

### 1.2 Agentic Digest

Same pattern as AI & Tech. The agentic digest has a second recipient (`AGENTIC_CC` from `~/scripts/.smtp_config`) — verify BOTH recipients receive it.

**Special attention:** The agentic digest prompt references a specific audience (Claude Code agents, Hono API, Slack/Jira webhooks, etc.). The prompt itself doesn't change, just the tool references. Verify the content quality is on-par.

### 1.3 Gaming Digest

Models: `opencode-go/minimax-m3` → `opencode-go/minimax-m3` (same ID in Pi)

### 1.4 World Digest

Models: `opencode-go/minimax-m3` → `opencode-go/minimax-m3` (same ID in Pi)

**Note:** Both gaming and world digests currently use `minimax-m3`. After migration, consider trying `opencode-go/deepseek-v4-pro` for world news (longer context, potentially better for geopolitical nuance).

---

## Phase 2: Hyperliquid SDK Maintenance

**Risk:** Low. Runs daily, same `pi -p` pattern as digests.

### Script changes

Current (`~/scripts/run_hyperliquid_sdk.sh`):
```bash
exec "$HOME/.opencode/bin/opencode" run -m opencode-go/qwen3.7-max --dir "$HOME" "$PROMPT" < /dev/null
```

Replace with:
```bash
exec pi -p --model opencode-go/qwen3.7-max "$PROMPT"
```

The `--dir "$HOME"` is replaced by `cd ~` before the exec (or keeping CWD as $HOME). Pi's `-p` mode uses the current working directory.

### Verification — Manual run

```bash
cd ~
export XDG_RUNTIME_DIR=/run/user/$(id -u)
bash ~/scripts/run_hyperliquid_sdk.sh
```

**Verify:**
1. [ ] Script exits with code 0
2. [ ] Agent successfully reads `~/.claude/skills/hyperliquid-run/SKILL.md` (check journal output)
3. [ ] Agent performs the git clone/pull, upstream SHA check, and any implementation work
4. [ ] If the agent made changes: check that the commit on `~/dev/hyperliquid` is sensible
5. [ ] If the agent pushed: check that CI is green on GitHub

**Rollback:** Revert the exec line to opencode.

### Enable timer

```bash
systemctl --user start hyperliquid-sdk.service
systemctl --user status hyperliquid-sdk.service --no-pager -l
```

After confirming:
```bash
systemctl --user enable --now hyperliquid-sdk.timer
```

---

## Phase 3: Dependabot Webhook Migration

**Risk: Medium-High.** This is the most security-sensitive component. The agent runs with a permission sandbox that must be accurately reproduced in Pi.

### Architecture change

| Aspect | OpenCode | Pi |
|---|---|---|
| Binary | `~/.opencode/bin/opencode` | `pi` (on PATH) |
| Mode | `run` (headless oneshot) | `-p` (print mode) |
| Model flag | `-m opencode-go/qwen3.7-plus` | `--model opencode-go/qwen3.7-plus` |
| Working dir | `--dir $HOME` | `cd ~` (CWD) |
| Sandbox | `opencode.json` config file | Extension: `pi-sandbox.ts` + `--tools` flag |
| Config env | `OPENCODE_CONFIG` | `-e /path/to/pi-sandbox.ts` |

### 3.1 Build the Pi sandbox extension

Create `~/.config/dependabot-webhook/pi-sandbox.ts`:

This extension must replicate the exact security posture of the current `opencode.json`:
- **Default-deny bash:** Block any bash command not in the allowlist
- **Allowlist:** echo, cat, rbenv, ruby, bundle, git, bin/rake, bin/brakeman, bin/bundler-audit, bin/importmap, bin/rubocop, bin/rails, gh pr/run/api
- **Deny explicitly:** sudo, docker, systemctl, curl, wget, rm, release.sh, up.sh (these are already blocked by the default-deny, but kept as comments for audit clarity)
- **Protected paths:** Block writes to `config/master.key`, `config/credentials`, `.env`

The Pi command:
```bash
pi -p \
  --model opencode-go/qwen3.7-plus \
  --tools bash,read,write,edit,grep,find,ls \
  -e /home/carter/.config/dependabot-webhook/pi-sandbox.ts \
  "$PROMPT"
```

The `--tools` flag limits available tools to just these seven. Since `web_search`, `web_fetch`, and any other extension tools are NOT in the list, the model can't even see them — they're an implicit deny. The extension provides a second layer: bash command-level allowlist.

### 3.2 Test the sandbox (critical — do NOT skip)

Before touching the Go code, verify the sandbox works:

**Test A: Allowed commands pass**
```bash
cd ~
pi -p \
  --model opencode-go/qwen3.7-plus \
  --tools bash,read,write,edit,grep,find,ls \
  -e /home/carter/.config/dependabot-webhook/pi-sandbox.ts \
  "Run: echo hello && git status && bundle -v"
```
- [ ] Output shows `hello`, git status, and bundle version
- [ ] No "blocked" messages

**Test B: Blocked commands are denied**
```bash
cd ~
pi -p \
  --model opencode-go/qwen3.7-plus \
  --tools bash,read,write,edit,grep,find,ls \
  -e /home/carter/.config/dependabot-webhook/pi-sandbox.ts \
  "Run: curl https://example.com"
```
- [ ] Agent receives a "blocked" response for the curl command
- [ ] Agent does NOT fetch the URL

**Test C: Blocked tools are unavailable**
```bash
cd ~
pi -p \
  --model opencode-go/qwen3.7-plus \
  --tools bash,read,write,edit,grep,find,ls \
  -e /home/carter/.config/dependabot-webhook/pi-sandbox.ts \
  "Search the web for something"
```
- [ ] Agent does NOT call `web_search` or `web_fetch` (tools not in allowlist)
- [ ] Agent reports it cannot search the web

**Test D: Full dependabot dry run**
Copy the prompt template from the Go code (the one `buildPrompt()` generates) and test it against a real repo:
```bash
cd ~
# Generate a prompt manually and test:
pi -p \
  --model opencode-go/qwen3.7-plus \
  --tools bash,read,write,edit,grep,find,ls \
  -e /home/carter/.config/dependabot-webhook/pi-sandbox.ts \
  "You are an automated dependency maintenance agent...
  [paste a test prompt pointing at ~/dev/delta_neutral or ~/dev/blog]
  Find all open dependabot PRs. If none, report and stop."
```
- [ ] Agent successfully runs `gh pr list`
- [ ] Agent respects sandbox boundaries
- [ ] If there are open PRs: agent attempts the bump workflow
- [ ] Check the git log on the dev repo to confirm changes are sensible

### 3.3 Update Go code

Changes to `~/dev/dependabot-webhook/main.go`:

1. Change default `agentPath` from `opencode` binary path to `"pi"`
2. Change args construction from `run -m ... --dir ...` to `-p --model ... --tools ... -e ...`
3. Remove `OPENCODE_CONFIG` env var (or repurpose it to hold the extension path)
4. Ensure CWD is `$HOME` when spawning

### 3.4 Deploy and test end-to-end

```bash
cd ~/dev/dependabot-webhook
# Build and deploy
bash release.sh
```

**Verification — End-to-end:**

1. [ ] Service starts cleanly: `systemctl --user status dependabot-webhook --no-pager -l`
2. [ ] Health check passes: `curl -s http://localhost:9099/health`
3. [ ] **Trigger a real dependabot PR:** Either wait for the next dependabot batch, or close + reopen a recent dependabot PR from GitHub to trigger a new webhook
4. [ ] Watch logs: `journalctl --user -u dependabot-webhook -f`
5. [ ] Verify the agent picks up the PR, processes it, and pushes
6. [ ] Verify CI is green on the repo
7. [ ] Verify the dependabot PR is closed with the batch comment

**Rollback:** `cd ~/dev/dependabot-webhook && git checkout main.go` to restore the opencode version, then `bash release.sh`. The old `opencode.json` is still on disk.

---

## Phase 4: `opencode web` → `pi-web`

**Risk: Medium.** New service install, but fully independent of other components. Can be done in parallel with Phases 1-3.

### 4.1 Install pi-web

```bash
npm install -g @jmfederico/pi-web
pi-web install
```

This creates two systemd user services:
- `pi-web-sessiond.service` — session daemon
- `pi-web-server.service` — web/API at `127.0.0.1:8504`

### 4.2 Verify local access

```bash
systemctl --user status pi-web-sessiond pi-web-server --no-pager -l
curl -s http://127.0.0.1:8504/ | head -20
```

**Verify:**
1. [ ] Both services active (sessiond + server)
2. [ ] HTTP 200 from `localhost:8504`
3. [ ] SSH tunnel from laptop: `ssh -L 8504:localhost:8504 carter@homelab` then open http://localhost:8504
4. [ ] UI loads in browser
5. [ ] Can add a project (start with `~/dev/delta_neutral`)
6. [ ] Can start a session
7. [ ] Model switching works
8. [ ] Session persists across browser refresh

### 4.3 Configure CF tunnel

Update the Cloudflare tunnel configuration to route `opencode.carter2099.com` to `localhost:8504` instead of `localhost:48099`.

**Option A:** Repurpose the existing hostname (simpler, but loses old access during cutover)
**Option B:** Create a new hostname (e.g., `pi.carter2099.com`) first, verify, then switch

Recommend Option B:

```bash
# Add new tunnel ingress for pi.carter2099.com → localhost:8504
# This is done via Cloudflare API or dashboard
# Then verify: https://pi.carter2099.com loads (behind CF Access)
```

**Verify:**
1. [ ] `https://pi.carter2099.com` prompts for CF Access SSO
2. [ ] After authentication, pi-web UI loads
3. [ ] Full workflow on phone: add project, start session, send message, agent responds
4. [ ] Session state survives phone browser tab close + reopen

### 4.4 Cutover

Once pi-web is verified on the new hostname:
1. Update the tunnel to also route `opencode.carter2099.com` → `localhost:8504` (both hostnames point to pi-web)
2. Verify `https://opencode.carter2099.com` also works
3. After a week of pi-web proving stable, stop the opencode-homelab service:
   ```bash
   systemctl --user stop opencode-homelab
   systemctl --user disable opencode-homelab
   ```

**Rollback (at any point before step 3):**
- Point the CF tunnel back at `localhost:48099`
- `systemctl --user restart opencode-homelab`

### 4.5 Cleanup (deferred)

After pi-web has been running stably for 2+ weeks:
- Remove `~/.config/systemd/user/opencode-homelab.service` and `.timer` files
- Remove `~/.config/opencode-homelab/env`
- Uninstall opencode: `rm -rf ~/.opencode`
- Remove opencode config: `~/.config/opencode/opencode.jsonc`

---

## Phase 5: Final Cleanup

After all components are migrated and verified:

- [ ] All 4 digest timers running on Pi for 2+ weeks with consistent quality
- [ ] Dependabot webhook has processed 3+ real PRs without issues
- [ ] pi-web stable for 2+ weeks
- [ ] No references to `opencode` remain in systemd units
- [ ] Remove opencode binary: `rm -rf ~/.opencode`
- [ ] Remove opencode auth: check `~/.local/share/opencode/`
- [ ] Remove `~/.config/dependabot-webhook/opencode.json` (replaced by `pi-sandbox.ts`)
- [ ] Update AGENTS.md to reflect Pi as the sole agent platform
- [ ] Update any shell aliases referencing opencode

---

## Risk Matrix

| Component | Risk | Impact if broken | Mitigation |
|---|---|---|---|
| Digests | Low | Missed daily digest emails | Migrate one at a time; old script in git history |
| Hyperliquid SDK | Low | Missed one daily maintenance run | Manual run if needed; not time-critical |
| Dependabot webhook | Medium-High | Dependabot PRs pile up unmerged | Sandbox tested thoroughly before Go deploy; manual PR merge fallback |
| pi-web | Medium | No remote browser access to agent | Old opencode-web stays up during migration; SSH as fallback |
| Interactive CLI | None | — | Already migrated |

## Decision Log

| Date | Decision |
|---|---|
| 2026-06-15 | Plan created. Phase 1 (digest migration) ready for review. |
| 2026-06-15 | Migration record will be kept in `~/docs/pi-migration/` for future reference. |
