---
name: idea
description: Save an idea as a markdown note in ~/ideas/ for later reference. Use when the user describes an idea they want to capture and revisit later, especially product/concept ideas.
---

# Idea

Save the user's idea verbatim as a markdown file in `~/ideas/`. Do not research, elaborate, analyze, or fill in gaps. The goal is to record the idea exactly as stated so the user can revisit it later.

## Steps

1. **Title.** If the user didn't state one, ask for a short title. If they said one, use it. The title becomes the filename slug.

2. **Write.** Write the file at `~/ideas/<slug>.md`:

   ```markdown
   # <Title>
   **Date:** YYYY-MM-DD
   **Status:** idea

   <The idea exactly as the user stated it — no elaboration.>
   ```

3. **Confirm.** Tell the user the path. That's it.

## Hard rules

- **No elaboration.** Do not expand, research, restructure, or "help" the idea along. Record only what the user said.
- **No sections.** Just date + status + the idea text. No "What it does", "Notes", or any other sections.
- **No connections.** Don't link it to other projects, even if a connection seems obvious.
- **Status is always "idea"** — the user can change it later if they start building.
