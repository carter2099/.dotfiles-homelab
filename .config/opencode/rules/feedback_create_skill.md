---
name: Always use /create-skill to make new skills
description: When creating a new Claude Code skill, invoke the create-skill skill instead of writing files directly — it handles dotfiles tracking automatically.
type: feedback
originSessionId: 39399a38-486b-4d35-8579-01557ebf45d5
---
Always use the `/create-skill` skill when creating a new user-level skill. Writing `~/.claude/skills/*/SKILL.md` directly skips the `dotfiles add` + commit + push step, leaving the skill untracked and at risk of being lost if homelab storage is wiped.

**Why:** Skills were historically gitignored by Claude Code's default `.claude/.gitignore`. That was fixed (2026-04-23), but the only reliable way to ensure the VCS step never gets skipped is to use the skill that bakes it in.

**How to apply:** Any time the user asks to create, add, or write a new skill → invoke `create-skill` skill first, don't write the file ad-hoc.
