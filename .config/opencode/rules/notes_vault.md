---
name: Notes vault at ~/notes
description: Carter's Claude-maintained knowledge vault — markdown repo where Claude organizes, indexes, and retrieves notes across sessions
type: project
originSessionId: a329b227-d261-4645-9ad4-83798b131d25
---
Carter keeps a private GitHub repo `carter2099/notes` cloned at `~/notes/`. It is a Claude-maintained knowledge vault: Carter surfaces things worth remembering (often from email digests or ad-hoc chats), and Claude writes/organizes the notes so they are retrievable in future sessions.

**Why:** Carter wants a durable external memory that survives past the auto-memory system — structured markdown he can also read/edit himself, with Claude as the librarian.

**How to apply:**
- Layout: topic directories at root (`crypto/`, `ai/`, `homelab/`, `reading/`, etc. — add new ones as needed). Atomic notes, one idea per file, `kebab-case-slug.md`.
- Every note has frontmatter: `title`, `tags`, `source`, `created`, `updated`. Template is in `~/notes/README.md`.
- `~/notes/INDEX.md` is the map — one line per note (`- [Title](path) — hook · tags: ...`). Keep it in sync on every commit; scan it first during retrieval before grepping the tree.
- Retrieval order: INDEX.md → Grep on tags/body → Glob on topic dir.
- Workflow: draft note, propose path+tags, write it, update INDEX.md, commit with descriptive message, `git push`. Auto-commit-and-push is the default (Carter confirmed) — don't stage-and-wait.
- Remote is SSH (`git@github.com:carter2099/notes.git`), tracks `origin/main`.
