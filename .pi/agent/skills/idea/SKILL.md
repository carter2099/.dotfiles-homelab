---
name: idea
description: Save an idea as a markdown note in ~/ideas/ for later reference. Use when the user describes an idea they want to capture and revisit later, especially product/concept ideas.
---

# Idea

Capture a user's idea as a markdown file in `~/ideas/`. These are functional scratchpads — not specs, not task lists. The goal is to preserve the thought so the user can revisit it later.

## Steps

1. **Title.** If the user didn't state one explicitly, infer a short descriptive title and confirm it with them. The title becomes the filename slug.

2. **Summarize.** Distill the idea into a markdown file at `~/ideas/<slug>.md`:

   ```markdown
   # <Title>
   **Date:** YYYY-MM-DD
   **Status:** idea

   <One-paragraph summary of the concept.>

   ## What it does
   <Functional description — what the user or end-user experiences, capabilities, and behavior. Not implementation.>

   ## Notes
   <Any additional context, constraints, inspiration sources, related homelab projects, or loose thoughts. Optional — omit if there's nothing to add.>
   ```

3. **Write the file** and tell the user the path.

## Guidelines

- **Functional, not technical.** Describe what it does and what it feels like to use — not how to build it.
- **Favor brevity.** These are idea scratchpads. A tight paragraph beats a wall of text.
- **Don't over-structure.** If the user just has a one-paragraph thought, that's enough — skip the sections. Meet the idea where it is.
- **Note connections.** If the idea relates to an existing homelab project (blog, delta_neutral, hub, etc.), mention it in the Notes section.
- **Status is always "idea"** — the user can change it later if they start building.
