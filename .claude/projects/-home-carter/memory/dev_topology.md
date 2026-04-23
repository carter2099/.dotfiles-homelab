---
name: Dev topology (Mac → homelab SSH)
description: User works from a Mac and SSHs into the homelab; Claude Code runs server-side. Mac-local file paths are not accessible from this session.
type: user
originSessionId: 59cee07f-6a07-48f6-88c9-b642bbd2b665
---
Carter develops from a Mac and SSHs into the homelab (`tp-server`, `/home/carter`) to run Claude Code sessions. When he mentions file paths like `/Users/carterbrown/...`, those are on his Mac and **not** reachable from this session.

How to apply:
- Don't try to `Read` Mac paths directly — they'll 404.
- For screenshots or files on his Mac, suggest `scp`-ing to the homelab first, or ask him to describe the content in words.
- For anything under `/home/carter/` — that's local to this session; read freely.
