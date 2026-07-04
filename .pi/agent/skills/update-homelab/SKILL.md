---
name: update-homelab
description: Apply safe homelab updates (apt, snap, npm, Docker images) with validation. Use when user says "update the homelab", "run updates", "apply patches", or after receiving an update report email.
---

# Update Homelab

Apply pending updates to the homelab, conscious of what needs a restart vs what doesn't, and validate everything after.

## Step 1: Assess current state

Read the most recent summary in ~/digests/updates/ (latest .md file) to see what was flagged in the last nightly audit.

Also run a fresh quick check:
```
apt list --upgradable 2>/dev/null | tail -n +2 | wc -l
snap refresh --list 2>/dev/null
npm outdated -g 2>/dev/null
docker images --format '{{.Repository}}:{{.Tag}} {{.CreatedAt}}' | grep -E "blog|delta|webui"
```

Present findings to the user. Group by risk level:
1. Safe (no restart needed): apt routine packages, snap non-docker, npm global
2. Needs restart (low risk): Docker container image pulls (open-webui, k3s workloads)
3. Needs restart (higher risk): docker-ce, cloudflared, containerd (infrastructure)
4. Custom deploys: blog-web, delta_neutral-web (requires release.sh)
5. Manual only: k3s version, Go runtime, neovim source build

## Step 2: Apply safe updates (no restart needed)

Ask for confirmation, then run:

```bash
# apt — but hold back docker packages
sudo apt update
sudo apt upgrade -y

# snap — skip docker snap
sudo snap refresh core22 core24 snapd 2>/dev/null || true

# npm global
npm update -g 2>/dev/null || true
```

Report what was updated and what was held back.

## Step 3: Docker image updates (needs container restart)

For each, confirm with user before proceeding. Process one at a time so failures are isolated.

### open-webui (pinned tag)
If a newer tag exists (e.g., v0.10.2 → v0.10.3):
1. Edit ~/open-webui/docker-compose.yml: bump the image tag
2. Run: cd ~/open-webui && docker compose pull && docker compose up -d
3. Wait for healthy: docker ps --filter name=open-webui --format '{{.Status}}'
4. Curl test: curl -so /dev/null -w '%{http_code}' http://127.0.0.1:48100

### k3s workloads (freshrss, uptime-kuma)
If a newer image tag is available:
1. Edit the k3s manifest (e.g., ~/k3s/freshrss/deployment.yaml): bump image tag
2. Apply: /usr/local/bin/k3s kubectl apply -f <manifest>
3. Wait for rollout: /usr/local/bin/k3s kubectl rollout status deploy/<name> -n <ns>
4. Verify pod is running

### freshrss (currently :latest)
This requires pulling the latest image and restarting the pod:
1. /usr/local/bin/k3s kubectl rollout restart deploy/freshrss -n freshrss
2. Wait for rollout
3. Verify

## Step 4: Custom app deploys (blog, delta_neutral)

These require the full deploy flow (release.sh). Only do this if:
- The user explicitly asks
- There are uncommitted changes? If yes, surface them. If not, proceed.

For each:
```bash
cd ~/<app> && bash release.sh
```
Then verify container is healthy and endpoint responds.

## Step 5: Infrastructure updates (Docker engine, cloudflared)

⚠️ These restart critical services. Only proceed with explicit user confirmation.

### Docker engine (apt)
```bash
sudo apt install --only-upgrade docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin -y
```
This restarts the Docker daemon. All containers will go down and come back up (restart: unless-stopped).
After: verify all containers are running (docker ps -a).

### cloudflared
```bash
sudo apt install --only-upgrade cloudflared -y
```
This restarts the cloudflared service. Verify tunnels reconnect: systemctl status cloudflared.

## Step 6: Final validation

After all updates, run a full health sweep:

```bash
# Containers
docker ps -a --format '{{.Names}} {{.Status}}'

# k3s pods
/usr/local/bin/k3s kubectl get pods -A | grep -v Completed

# Key endpoints
curl -so /dev/null -w '%{http_code}' http://127.0.0.1:48100  # open-webui
curl -so /dev/null -w '%{http_code}' http://127.0.0.1:3099   # blog
curl -so /dev/null -w '%{http_code}' http://127.0.0.1:43080  # delta_neutral
curl -so /dev/null -w '%{http_code}' http://127.0.0.1:8504   # pi-web

# Disk
df -h /
```

Report any anomalies.

## Step 7: Write session note

Write a brief session summary to ~/notes/sessions/YYYY-MM-DD.md (use current date). Format:

```
# Session: YYYY-MM-DD
**Topic:** Homelab updates
**Outcome:** applied X apt updates, Y snap refreshes, updated Z Docker images
**Notes:**
- (any notable events, errors, or decisions)
```

## Safety rules

- Never run `snap refresh docker` — AppArmor profile risk
- Never run `sudo apt dist-upgrade` — only `upgrade`
- Never run `sudo aa-remove-unknown` — will break Docker
- Never update Go, Ruby, Node, or neovim without explicit user request
- Never update k3s itself without explicit user request
- If anything fails, stop and report — don't continue to the next step
- For blog/delta_neutral deploys: always check git status first, commit before deploy
- After any Docker daemon restart, verify ALL containers came back up
