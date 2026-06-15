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
- [ ] Each has a `SKILL.md` — pending
- [ ] `/skill:homelab-reboot` expands correctly — pending (need to restart pi)
- [ ] `/skill:hyperliquid-run` expands correctly — pending

### 0.5.2 Rules migration

**Action:** Archived `opencode-agent-migration.md` to `~/docs/pi-migration/prior-migration.md`

**AGENTS.md updates needed** (from gap analysis):
- [ ] dev_topology.md → Add Mac/SSH topology section
- [ ] user_assistant_framing.md → Add non-code assistance framing
- [ ] feedback_commit_before_deploy.md → Add commit-before-deploy discipline
- [ ] feedback_create_skill.md → Update skill creation path references to Pi
- [ ] feedback_no_aa_remove_unknown.md → Add aa-remove-unknown warning
- [ ] digest_archives.md → Covered (AGENTS.md "Email Digests"), no changes needed
- [ ] notes_vault.md → Covered (AGENTS.md "Repository Structure"), no changes needed
- [ ] user_endler_tenets.md → Covered (AGENTS.md "Working principles"), no changes needed
- [ ] MEMORY.md → Discard (index only)

### 0.5.3 Archive

- [ ] Tarball created — pending
- [ ] Original files still on disk — confirmed (not deleting until Phase 5)

### 0.5.4 Dotfiles tracking

- [ ] `dotfiles add` — pending after AGENTS.md update
- [ ] `dotfiles commit` — pending
- [ ] `dotfiles push` — pending

---

## Skill opencode-reference audit

Skills that need updates for Pi compatibility:
- `create-skill` — writes to `~/.config/opencode/skills/` → update to `~/.pi/agent/skills/`
- `email-digest` — may reference opencode tool names (Read/Bash/WebFetch) → update to Pi tool names
- `hyperliquid-run` — references `opencode run -m` → update to `pi -p --model`
- `dependabot-release` — references `opencode.json` → update to `pi-sandbox.ts`
