---
name: Email digest archives at ~/digests/
description: Each daily email digest saves a markdown summary at ~/digests/<topic>/YYYY-MM-DD.md; read these before asking Carter to re-describe digest content
type: reference
originSessionId: a329b227-d261-4645-9ad4-83798b131d25
---
Each daily email digest (see `email-digest` skill and the four systemd user timers documented in `~/CLAUDE.md`) writes a concise markdown summary of what it sent to `~/digests/<topic>/YYYY-MM-DD.md` at the end of each run. The scripts auto-delete summaries older than 7 days.

**Known topic directories:**
- `~/digests/agentic-platform/` — agentic-digest (16:00 UTC)
- Other three timers (`ai-tech-digest`, `gaming-digest`, `world-digest`) use the same pattern; verify the exact path by reading `~/scripts/run_<name>_digest.sh` when needed.

**When to use:** Any time Carter references something he saw in a digest ("I saw X in the agentic digest") and asks for notes or follow-up. Read the recent archives first — they contain the one-line story summaries Carter already saw. That's faster and more accurate than asking him to re-describe or doing a fresh web search.

**Format:** `# <Topic> Digest — YYYY-MM-DD` with `## Fresh` and `## Recent & Relevant` sections, each a bulleted list of `- **Story title** — one-line summary`.
