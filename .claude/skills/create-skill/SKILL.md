---
name: create-skill
description: Create a new user-level Claude Code skill, commit it to dotfiles, and push. Always use this instead of writing skill files directly.
---

# Create Skill

Create a new skill under `~/.claude/skills/` and immediately commit it to the dotfiles bare repo so it's backed up to GitHub.

## Step 1: Agree on the skill

Before writing anything, confirm with the user:
- **Name**: the slash-command name (e.g. `my-skill` → `/my-skill`)
- **Description**: one line, used to decide when to invoke the skill
- **What it does**: enough to write a complete SKILL.md

## Step 2: Write the skill file

```
~/.claude/skills/<name>/SKILL.md
```

Frontmatter fields:
```
---
name: <name>
description: <one-line description — specific enough to trigger correctly>
---
```

Body: step-by-step instructions Claude will follow when the skill is invoked. Write it as instructions to yourself, not documentation for a human.

## Step 3: Commit to dotfiles immediately

```bash
dotfiles add .claude/skills/<name>/SKILL.md
dotfiles commit -m "skill: add <name>"
dotfiles push
```

Do not skip this step. Skills not in dotfiles are lost if the homelab storage is wiped.

## Step 4: Confirm

Tell the user the skill is live and tracked. Remind them to invoke it as `/<name>`.
