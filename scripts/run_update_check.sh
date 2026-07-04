#!/usr/bin/env bash
# Audits the homelab for outdated software and emails a categorized report.
# Scheduled via systemd timer (update-check.timer). Report-only — no changes made.
# Run manually with: bash ~/scripts/run_update_check.sh
set -euo pipefail
export HOME="/home/carter"

TODAY="$(date +%Y-%m-%d)"
START_TS="$(date +%s)"
mkdir -p "$HOME/digests/updates"

PROMPT='You are a homelab maintenance auditor. Your job is to check for outdated software across the homelab and email a report. DO NOT install, upgrade, or modify anything. Report only.

## Step 0: Prepare

Create /home/carter/digests/updates/ if it does not exist (it should already exist). You will write the HTML email body to /home/carter/digests/updates/.daily_report.html and send it. Then archive and write a summary.

## Step 1: System packages (apt)

Run: apt list --upgradable 2>/dev/null
Count and categorize:
- Security-critical / infrastructure: docker-*, cloudflared, openssl, kernel, systemd, containerd
- Everything else (routine libs, tools)
Report current → new versions for the infra-critical ones. For routine ones, just note the count.

## Step 2: Snap packages

Run: snap refresh --list 2>/dev/null || sudo snap refresh --list 2>/dev/null
List any snaps with available updates. Flag the docker snap separately — it must never be refreshed unattended (snap-managed Docker + AppArmor = risk of profile breakage). Other snaps (core22, core24, snapd) are safe to note.

## Step 3: Docker images — k3s workloads

Run:
  export XDG_RUNTIME_DIR=/run/user/$(id -u)
  /usr/local/bin/k3s kubectl get deploy,ds,sts -A -o wide 2>/dev/null

For each non-kube-system deployment, note the image and tag:
- traefik (currently rancher/mirrored-library-traefik:3.3.6) — use web_search to find the latest 3.x patch release
- freshrss (freshrss/freshrss:latest) — tagged :latest, cannot determine freshness without pulling. Flag as "pull to verify."
- uptime-kuma (louislam/uptime-kuma:1) — use web_search to find the latest release
- Any other non-kube-system workloads

For kube-system images (coredns, metrics-server, local-path-provisioner), note them but they are tied to the k3s version — flag under the k3s section.

## Step 4: Docker images — Compose apps

Run:
  docker ps --format '"'"'{{.Names}} {{.Image}} {{.Status}}'"'"'
  docker images --format '"'"'{{.Repository}}:{{.Tag}} {{.CreatedAt}}'"'"' | grep -E "blog|delta|webui"

- open-webui: pinned to v0.10.2. Use web_search ("open-webui releases github") to check for newer tags. Compare semver.
- blog-web: locally built image. Note the build date. Flag if >2 weeks old (requires deploy via release.sh).
- delta_neutral-web: locally built image. Note the build date. Flag if >2 weeks old (requires deploy via release.sh).

## Step 5: npm global packages

Run: npm list -g --depth=0 2>/dev/null
Then: npm outdated -g 2>/dev/null || echo "(npm outdated not available or all up to date)"

Report any outdated global packages. pi-web (@jmfederico/pi-web) is the main one to watch.

## Step 6: Language runtimes

- Go: run "go version". Use web_search ("go latest stable version 2026") to find current stable. Flag if more than one minor version behind.
- Ruby: run "rbenv versions". Note installed versions. No action needed unless user asks.
- Node: run "fnm list 2>/dev/null || node --version". Note installed/default version.

## Step 7: Infrastructure versions

- k3s: run "/usr/local/bin/k3s --version 2>/dev/null | head -1". Use web_search ("k3s latest release github") to find current stable. Flag if behind.
- neovim (built from source): run "nvim --version 2>/dev/null | head -1". Use web_search ("neovim latest release") to find current stable. Flag if behind.
- cloudflared: already covered by apt check above.
- dependabot-webhook: run "cd ~/dev/dependabot-webhook && git fetch origin 2>/dev/null && git status 2>/dev/null | head -5" to check if the dev clone is behind origin. Also check the binary: "file ~/dev/dependabot-webhook/dependabot-webhook 2>/dev/null" for build date.

## Step 8: System health (bonus — always include)

Run each of these:
- df -h / /home
- free -h
- uptime
- test -f /var/run/reboot-required && echo "REBOOT REQUIRED" || echo "No reboot required"
- docker ps -a --format '"'"'{{.Names}} {{.Status}}'"'"'
- Check if any docker containers are in "Exited" or "unhealthy" state — flag them

## Step 9: Build the HTML email

Write a clean, mobile-friendly HTML email to /home/carter/digests/updates/.daily_report.html. Use inline styles (not a stylesheet). Keep it simple — this is read on a phone.

Structure:
1. Header: "Homelab Update Report — '"$TODAY"'"
2. 🔴 Needs Attention (breaking changes, major versions, security, exited containers)
3. 🟡 Behind but Safe (minor/patch updates, no breaking changes expected)
4. 🟢 Up to Date
5. ℹ️ System Health (disk, memory, uptime, reboot needed, container status)

Each item in sections 2-3 should show: current version → target version, and a one-line note.

## Step 10: Send the email

Run:
  python3 /home/carter/scripts/send_digest.py --subject "Homelab Update Report — '"$TODAY"'" --body-file /home/carter/digests/updates/.daily_report.html --to carter2099@pm.me

Then archive: rename /home/carter/digests/updates/.daily_report.html to /home/carter/digests/updates/'"$TODAY"'.html

## Step 11: Write machine-readable summary

Write a summary to /home/carter/digests/updates/'"$TODAY"'.md in this exact format:

```
# Homelab Update Report — '"$TODAY"'
**Model:** deepseek-v4-flash | **Sent to:** carter2099@pm.me

## Needs Attention
- [item] — current → target — reason
- [item] — current → target — reason

## Behind but Safe
- [item] — current → target
- [item] — current → target

## Up to Date
- [item]
- [item]

## System Health
- Disk: X% used on /
- Memory: X available
- Uptime: X days
- Reboot needed: yes/no
- Containers: X running, Y exited/unhealthy
```

Every item in "Needs Attention" and "Behind but Safe" MUST include the current version and the target version separated by →. This format is machine-readable so the update-homelab skill can parse and act on it.

Then delete any .md files in /home/carter/digests/updates/ older than 30 days.'

pi -p --model opencode-go/deepseek-v4-flash "$PROMPT"
END_TS="$(date +%s)"
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) update-check duration=$((END_TS - START_TS))s model=deepseek-v4-flash" >> "$HOME/digests/updates/.runs.log"
