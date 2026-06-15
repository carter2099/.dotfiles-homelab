# Pi Migration Runbook

**Started:** 2026-06-15 | **Plan:** `~/plans/opencode-to-pi-migration.md`

Chronological log of every action taken, test result, and decision.

---

## 2026-06-15 20:30 UTC — Phase 0.5 Start

### 0.5.1 Skills migration

**Action:** Copied all 16 skills from `~/.config/opencode/skills/` to `~/.pi/agent/skills/`
```bash
cp -r ~/.config/opencode/skills/* ~/.pi/agent/skills/
```

**Result:** 16 skill directories present in `~/.pi/agent/skills/`:
backup-health, blog-delete, blog-edit, blog-post, blog-review, create-skill,
dependabot-release, deploy-app, dev-test-and-release, email-digest,
homelab-reboot, hyperliquid-release, hyperliquid-run, image-fetch, k-logs, note-save

**Verification:**
- [x] Each has a `SKILL.md` — all 16 confirmed present
- [ ] `/skill:homelab-reboot` expands correctly — pending (skills loaded on next pi restart)
- [ ] `/skill:hyperliquid-run` expands correctly — pending

### 0.5.2 Rules migration

**Action:** Archived `opencode-agent-migration.md` to `~/docs/pi-migration/prior-migration.md`

**AGENTS.md updates applied** (commit d217219):
- [x] dev_topology.md → Added Mac/SSH client topology to Environment section
- [x] user_assistant_framing.md → Added "Scope" section after Endler tenets
- [x] feedback_commit_before_deploy.md → Added "Commit before deploy" to App Deployment
- [x] feedback_create_skill.md → Added "Skills" section with /create-skill rule
- [x] feedback_no_aa_remove_unknown.md → Added "Never run aa-remove-unknown" to App Deployment
- [x] digest_archives.md → Covered (AGENTS.md "Email Digests"), no changes needed
- [x] notes_vault.md → Covered (AGENTS.md "Repository Structure"), no changes needed
- [x] user_endler_tenets.md → Covered (AGENTS.md "Working principles"), no changes needed
- [x] MEMORY.md → Discard (index only)

### 0.5.3 Archive

- [x] Tarball created: `~/docs/pi-migration/archives/opencode-artifacts-20260615.tar.gz` (10.8 MB)
- [x] Contents verified: `.config/opencode/`, `.config/opencode-homelab/`, dependabot `opencode.json`, auth.json, snapshot/
- [x] Original files still on disk (not deleting until Phase 5)

### 0.5.4 Dotfiles tracking

- [x] `dotfiles add` — all migration artifacts staged
- [x] `dotfiles commit` — commit d217219 pushed to origin/main
- [x] `dotfiles status` — clean

---

## Skill opencode-reference audit

Skills updated:
- [x] `create-skill` — paths updated from `~/.config/opencode/skills/` to `~/.pi/agent/skills/`
- [ ] `email-digest` — opencode-specific tool names and references → will update in Phase 1
- [ ] `hyperliquid-run` — references `opencode run -m`, opencode-specific notes → will update in Phase 2
- [ ] `dependabot-release` — references `opencode.json` → will update in Phase 3

**Decision (2026-06-15):** Only update `create-skill` now (load-bearing — Pi agent uses it to create new skills). `email-digest`, `hyperliquid-run`, and `dependabot-release` will be updated in their respective phases when the actual scripts are rewritten.

---

## 2026-06-15 20:26 UTC — Phase 1.1: AI & Tech Digest Migration

### Script changes (`~/scripts/run_ai_tech_digest.sh`)

- `opencode run -m opencode-go/deepseek-v4-flash` → `pi -p --model opencode-go/deepseek-v4-pro`
- Removed `< /dev/null` (not needed in pi -p)
- Removed `$HOME/.opencode/bin` from PATH (pi is on PATH via fnm)
- Removed `DBUS_SESSION_BUS_ADDRESS`
- Tool names: Read→read, WebFetch→web_fetch, Bash→bash
- "you do NOT have a web search tool" → "use web_search to find articles, web_fetch to read them"
- Removed headless-mode write restriction warning

### Manual test (20:26–20:29 UTC)

```bash
cd ~ && bash ~/scripts/run_ai_tech_digest.sh
```

**Verification:**
- [x] Script exits with code 0
- [x] `~/digests/ai-tech/2026-06-15.md` exists (2892 bytes, 10 fresh + 4 recent stories)
- [x] Email sent to carter2099@pm.me (agent confirmed)
- [x] Quality check: All 14 stories from reputable sources, dates verified via fetch
- [x] Dedup check: Agent reports no overlap with yesterday's digest
- [x] Summary written in correct format
- [x] Timer active and enabled — next fire: 2026-06-16 15:00 UTC

**Result: PASS** — AI & Tech digest migrated to Pi. Timer unchanged (same script path, same service unit).

**Decision:** Proceed to Phase 1.2 (agentic digest) next.

---

## 2026-06-15 20:35 UTC — Phase 1.3: Gaming Digest Migration

### Script changes (`~/scripts/run_gaming_digest.sh`)

Same pattern as AI & Tech, with `deepseek-v4-pro`.

### Manual test

```bash
cd ~ && bash ~/scripts/run_gaming_digest.sh
```

**Verification:**
- [x] Script exits with code 0
- [x] `~/digests/gaming-digest/2026-06-15.md` exists — 10 fresh + 2 recent stories
- [x] Email sent to carter2099@pm.me
- [x] Steam Next Fest, Nintendo Direct, Civilization 7, UMVC3 patch, etc. — all verified via fetch

**Result: PASS**

---

## 2026-06-15 20:36 UTC — Phase 1.4: World Digest Migration

### Script changes (`~/scripts/run_world_digest.sh`)

Same pattern with `deepseek-v4-pro`.

### Manual test

```bash
cd ~ && bash ~/scripts/run_world_digest.sh
```

**Verification:**
- [x] Script exits with code 0
- [x] `~/digests/world-digest/2026-06-15.md` exists — 9 fresh + 3 recent stories
- [x] Email sent to carter2099@pm.me
- [x] U.S.-Iran framework deal, UK social media ban, Russia arson investigation, etc. — all verified via fetch
- [x] Tone check: neutral, fact-based, no editorializing

**Result: PASS**

---

## 2026-06-15 20:36 UTC — Phase 1.2: Agentic Digest (Script Updated, Test Deferred)

### Script changes (`~/scripts/run_agentic_digest.sh`)

Same pattern applied: `pi -p --model opencode-go/deepseek-v4-pro`, tool name updates, removed headless restrictions.

**Not manually tested** — has AGENTIC_CC second recipient. Will test after all digests verified (per user request).

---

## Phase 1 Summary

| Digest | Model | Manual Test | Next Scheduled |
|---|---|---|---|
| AI & Tech | deepseek-v4-pro | ✅ PASS | 2026-06-16 15:00 UTC |
| Agentic | deepseek-v4-pro | ⏸️ Deferred (AGENTIC_CC) | 2026-06-16 16:00 UTC |
| Gaming | deepseek-v4-pro | ✅ PASS | 2026-06-16 19:00 UTC |
| World | deepseek-v4-pro | ✅ PASS | 2026-06-16 21:00 UTC |

All four timers remain enabled. Next automatic fires will use `pi -p`.

---

## 2026-06-15 ~23:13 UTC — Phase 2: Hyperliquid SDK Migration

### Script changes (`~/scripts/run_hyperliquid_sdk.sh`)

- `opencode run -m opencode-go/qwen3.7-max --dir "$HOME"` → `pi -p --model opencode-go/qwen3.7-max`
- Removed `< /dev/null`, `$HOME/.opencode/bin` from PATH, `DBUS_SESSION_BUS_ADDRESS`
- Skill path: `~/.claude/skills/` → `~/.pi/agent/skills/` (migrated in Phase 0.5)
- Tool name: "Read tool" → "read tool"

### Manual test (run #41)

Agent completed the full hyperliquid-run skill cycle:

- [x] Read `~/.pi/agent/skills/hyperliquid-run/SKILL.md` correctly
- [x] Checked out dev branch, pulled latest
- [x] Scanned upstream SHAs (Python unchanged, TS bumped)
- [x] Implemented Explorer WebSocket transport (subscribe_explorer_block, subscribe_explorer_txs)
- [x] 569/569 unit specs pass, rubocop clean
- [x] 13/13 integration tests pass (eighth consecutive 100% green run)
- [x] Committed and pushed to dev: `de832aa`
- [x] State file updated with run #41 details
- [x] Email sent to carter2099@pm.me (temp file cleaned up)
- [x] Timer active and enabled — next fire: Mon/Thu at 08:00 UTC

**Result: PASS** — Qwen 3.7 Max performed identically on Pi vs opencode.
