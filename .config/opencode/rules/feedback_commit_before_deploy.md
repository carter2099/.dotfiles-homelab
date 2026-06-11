---
name: Always commit and push before deploying
description: Before running release.sh on any homelab app, commit and push uncommitted changes — never deploy off a dirty working tree.
type: feedback
originSessionId: 06cd4c56-8bd5-43d5-9211-6eaa3f046d08
---
Always commit and push before deploying any homelab app. Do not run `release.sh` (or trigger the deploy-app skill) while there are uncommitted changes in the app's repo, even though the docker build uses local files and would technically pick them up.

**Why:** Carter wants the deployed state to match `origin/main` exactly. If the source on disk diverges from git, the next deploy from a fresh clone (or anyone else looking at GitHub) will see the wrong code, and rollbacks via git become unreliable. Deploys must be reproducible from the remote.

**How to apply:** Before invoking the `deploy-app` skill or running `release.sh`, check `git status` in the app repo. If there are uncommitted changes related to the deploy, commit and push them first, then deploy. If there are unrelated dirty files, surface them and ask before proceeding.
