#!/usr/bin/env bash
# Nightly homelab maintenance: auto-applies safe updates, validates, and emails a report.
# Scheduled via systemd timer (update-check.timer). 
# Safe auto-applies: apt (non-Docker), snap (non-Docker), npm global, k3s image pulls.
# Reports only: Docker engine, cloudflared, open-webui pin, major versions, runtimes.
# Run manually with: bash ~/scripts/run_update_check.sh
set -euo pipefail
export HOME="/home/carter"

TODAY="$(date +%Y-%m-%d)"
START_TS="$(date +%s)"
mkdir -p "$HOME/digests/updates"

PROMPT='You are a homelab maintenance agent. Your job is to auto-apply safe updates, validate everything still works, and email a categorized report of what was done and what still needs human attention.

## Safety boundaries

AUTO-APPLY (do these without asking):
- apt upgrade for routine packages — BUT hold back: docker-ce, docker-ce-cli, containerd.io, docker-buildx-plugin, docker-compose-plugin, cloudflared
- snap refresh for core22, core24, snapd — BUT NEVER refresh the docker snap (AppArmor risk)
- npm update -g (updates pi-web and other global packages)
- k3s workload image pulls: freshrss (freshrss/freshrss:latest) and uptime-kuma (louislam/uptime-kuma:1) — do a rollout restart to pull latest within their current tag track

DO NOT TOUCH (report only):
- Docker engine/plugins (apt) — restarts daemon, manual only
- cloudflared (apt) — restarts tunnel, manual only
- open-webui (version-pinned in docker-compose.yml) — needs human to bump the tag
- blog-web / delta_neutral-web — custom images, need release.sh
- k3s itself, Go, Ruby, Node, neovim — runtimes and infrastructure
- Any major version bumps (traefik 3→4, uptime-kuma 1→2, etc.)

## Step 1: Hold risky apt packages

Run:
  sudo apt-mark hold docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin cloudflared 2>/dev/null || true

This prevents these from being upgraded in Step 2. We will unhold them after.

## Step 2: Apply safe apt upgrades

Run:
  sudo apt update
  sudo apt upgrade -y

Capture the output. Note how many packages were upgraded and which ones.

## Step 3: Unhold the risky packages

Run:
  sudo apt-mark unhold docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin cloudflared 2>/dev/null || true

This restores normal apt behavior for future manual updates.

## Step 4: Apply safe snap refreshes

Run:
  sudo snap refresh core22 core24 snapd 2>/dev/null || true

Do NOT refresh the docker snap. Note what was refreshed.

## Step 5: Apply npm global updates

Run:
  npm update -g 2>/dev/null || true

Note what was updated (especially pi-web).

## Step 6: Pull latest k3s workload images

For freshrss (freshrss/freshrss:latest in namespace freshrss):
  export XDG_RUNTIME_DIR=/run/user/$(id -u)
  /usr/local/bin/k3s kubectl rollout restart deploy/freshrss -n freshrss
  /usr/local/bin/k3s kubectl rollout status deploy/freshrss -n freshrss --timeout=120s

For uptime-kuma (louislam/uptime-kuma:1 in namespace default):
  /usr/local/bin/k3s kubectl rollout restart deploy/uptime-kuma -n default
  /usr/local/bin/k3s kubectl rollout status deploy/uptime-kuma -n default --timeout=120s

If either rollout fails or times out, flag it prominently in the report.

## Step 7: Validate — did everything survive?

Run ALL of these checks:

```bash
# Docker containers — all should be Up and healthy
docker ps -a --format '"'"'{{.Names}} {{.Status}}'"'"'

# k3s pods — none should be Error, CrashLoopBackOff, or Pending
/usr/local/bin/k3s kubectl get pods -A --no-headers | grep -v -E "Running|Completed"

# Key endpoints — all should return 200 or 3xx
curl -so /dev/null -w '"'"'%{http_code}'"'"' http://127.0.0.1:48100  # open-webui
echo ""
curl -so /dev/null -w '"'"'%{http_code}'"'"' http://127.0.0.1:3099   # blog
echo ""
curl -so /dev/null -w '"'"'%{http_code}'"'"' http://127.0.0.1:43080  # delta_neutral
echo ""
curl -so /dev/null -w '"'"'%{http_code}'"'"' http://127.0.0.1:8504   # pi-web
echo ""
```

If any endpoint returns 5xx or connection refused, flag it as 🔴 CRITICAL in the report.

## Step 8: Full audit — what still needs attention?

Now check everything that was NOT auto-applied:

```
# apt packages still upgradable (should be docker + cloudflared)
apt list --upgradable 2>/dev/null

# snap (docker only)
snap refresh --list 2>/dev/null

# Docker Compose images
docker images --format '"'"'{{.Repository}}:{{.Tag}} {{.CreatedAt}}'"'"' | grep -E "blog|delta|webui"

# open-webui: use web_search to check for newer tags beyond v0.10.2
# blog-web: note build date
# delta_neutral-web: note build date (flag if >2 weeks old)

# k3s infrastructure images (traefik, coredns, etc.)
/usr/local/bin/k3s kubectl get deploy,ds -A -o wide 2>/dev/null | grep -E "traefik|coredns|metrics"

# Language runtimes
go version
rbenv versions 2>/dev/null | tail -1
node --version

# Infrastructure
/usr/local/bin/k3s --version 2>/dev/null | head -1
nvim --version 2>/dev/null | head -1
cd ~/dev/dependabot-webhook && git fetch origin 2>/dev/null && git status -sb 2>/dev/null | head -3
```

## Step 9: System health

```
df -h / /home
free -h
uptime
test -f /var/run/reboot-required && echo "REBOOT REQUIRED" || echo "No reboot required"
```

## Step 10: Build the HTML email

Write to /home/carter/digests/updates/.daily_report.html. Clean, mobile-friendly HTML with inline styles.

Sections:
1. Header: "Homelab Update Report — '"$TODAY"'"
2. ✅ Auto-Applied (what the agent did tonight — apt packages, snaps, npm, k3s restarts)
3. 🔴 Needs Attention (held-back packages: docker-*, cloudflared; open-webui if newer tag exists; blog/delta if >2 weeks old; any CRITICAL validation failures)
4. ℹ️ Behind but Safe (traefik patch, uptime-kuma if major bump exists, runtimes, neovim, dependabot-webhook)
5. 📊 System Health (disk, memory, uptime, reboot needed, container/pod status)
6. ✅ Validation (endpoint check results — passed or failed)

## Step 11: Send and archive

Send:
  python3 /home/carter/scripts/send_digest.py --subject "Homelab Update Report — '"$TODAY"'" --body-file /home/carter/digests/updates/.daily_report.html --to carter2099@pm.me

Archive: rename .daily_report.html to '"$TODAY"'.html

## Step 12: Write machine-readable summary

Write to /home/carter/digests/updates/'"$TODAY"'.md:

```
# Homelab Update Report — '"$TODAY"'
**Model:** deepseek-v4-flash | **Sent to:** carter2099@pm.me

## Auto-Applied
- [item] — updated from X → Y
- [item] — already current (rollout restart only)

## Needs Attention
- [item] — current → target — reason
- [item] — current → target — reason

## Behind but Safe
- [item] — current → target
- [item] — current → target

## System Health
- Disk: X% used on /
- Memory: X available
- Uptime: X days
- Reboot needed: yes/no
- Containers: X running, Y issues
- Endpoints: all passed / [list failures]
```

Delete any .md files in /home/carter/digests/updates/ older than 30 days.'

pi -p --model opencode-go/deepseek-v4-flash "$PROMPT"
END_TS="$(date +%s)"
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) update-check duration=$((END_TS - START_TS))s model=deepseek-v4-flash" >> "$HOME/digests/updates/.runs.log"
