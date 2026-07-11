---
name: update-homelab
description: Apply homelab updates that the nightly agent holds back (Docker engine, cloudflared, open-webui pin bump, k3s workloads, custom app deploys, runtimes). Use after receiving an update report email or when user says "update the homelab".
---

# Update Homelab

Apply the updates the nightly agent (update-check.timer) won't touch: Docker engine, cloudflared, open-webui version bumps, custom app deploys, and runtime upgrades. Always validate after.

## Step 1: Assess

Read the most recent summary in ~/digests/updates/ (latest .md) to see what the nightly agent flagged.

Then run a fresh check:
```
apt list --upgradable 2>/dev/null | grep -E "docker|cloudflared"
snap refresh --list 2>/dev/null | grep docker
docker images --format '{{.Repository}}:{{.Tag}} {{.CreatedAt}}' | grep -E "blog|delta|webui"
```

Present findings organized by risk:
1. **Low risk** — Docker Compose image bumps (open-webui patch, freshrss restart if nightly failed)
2. **Medium risk** — Docker engine + plugins (daemon restart, containers auto-recover)
3. **Higher risk** — cloudflared (tunnel restart), k3s version, Go/Ruby/Node upgrades
4. **Custom deploys** — blog, delta_neutral (release.sh + full validation)

## Step 2: Apply updates (ascending risk order)

### 2a. open-webui version bump
If a newer patch/minor tag exists (e.g., v0.10.2 → v0.10.3):
1. Confirm with user
2. Edit ~/open-webui/docker-compose.yml: bump the tag
3. `cd ~/open-webui && docker compose pull && docker compose up -d`
4. Wait for healthy: `docker ps --filter name=open-webui --format '{{.Status}}'`
5. Curl: `curl -so /dev/null -w '%{http_code}' http://127.0.0.1:48100`

### 2b. k3s workload image updates
If a k3s workload has a specific newer tag (not just :latest restart):
1. Edit the deployment manifest in ~/k3s/<name>/
2. Apply: `/usr/local/bin/k3s kubectl apply -f ~/k3s/<name>/<deployment>.yaml`
3. Wait for rollout: `/usr/local/bin/k3s kubectl rollout status deploy/<name> -n <ns> --timeout=120s`
4. Verify pod is Running

### 2c. Docker engine + plugins (apt)
⚠️ Restarts Docker daemon. Containers with `restart: unless-stopped` come back automatically.

1. Confirm with user
2. `sudo apt install --only-upgrade docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin -y`
3. Wait 10 seconds for daemon restart
4. `docker ps -a` — verify ALL containers are Up
5. If any container failed to restart, start it manually

### 2d. cloudflared
⚠️ Restarts the tunnel. Brief interruption to all tunnel-routed services (pi-web, open-webui, blog, delta_neutral, dependabot-webhook).

1. Confirm with user
2. `sudo apt install --only-upgrade cloudflared -y`
3. `systemctl status cloudflared` — verify active
4. Curl test the tunnel endpoints (use the same curl commands as the nightly agent)

### 2e. Custom app deploys (blog, delta_neutral)
Only if user explicitly requests. Follow the standard deploy flow per AGENTS.md:
1. Check git status in the app repo — COMMIT BEFORE DEPLOY
2. `cd ~/<app> && bash release.sh`
3. Verify container healthy
4. Curl the endpoint

### 2f. Runtime / infrastructure upgrades
Only with explicit user request. These are never auto-applied:
- Go: `sudo snap refresh go` or manual install
- Ruby: `rbenv install <version>`
- Node: `fnm install <version>`
- Neovim: rebuild from source in ~/build/neovim/
- k3s: follow official upgrade docs

## Step 3: Full validation

After all updates, run the same sweep the nightly agent uses:

```
# Containers
docker ps -a --format '{{.Names}} {{.Status}}'

# k3s pods (should show no output — only Running/Completed pods exist)
/usr/local/bin/k3s kubectl get pods -A --no-headers | grep -v -E "Running|Completed"

# Endpoints
curl -so /dev/null -w '%{http_code}' http://127.0.0.1:48100 && echo " open-webui"
curl -so /dev/null -w '%{http_code}' http://127.0.0.1:3099 && echo " blog"
curl -so /dev/null -w '%{http_code}' http://127.0.0.1:43080 && echo " delta_neutral"
curl -so /dev/null -w '%{http_code}' http://127.0.0.1:8504 && echo " pi-web"
curl -so /dev/null -w '%{http_code}' http://127.0.0.1:8082/health && echo " opencode-go-proxy"

# Disk
df -h /
```

Report any failures immediately.

## Step 4: Write session note

Write a brief session summary to ~/notes/sessions/YYYY-MM-DD.md:

```
# Session: YYYY-MM-DD
**Topic:** Homelab updates
**Applied:**
- (list what was updated with versions)
**Validation:** (passed / issues found)
**Notes:** (any decisions, errors, reversions)
```

## Safety rules

- Never run `snap refresh docker` — AppArmor profile risk
- Never run `sudo apt dist-upgrade` — only `upgrade`
- Never run `sudo aa-remove-unknown` — will break Docker
- For blog/delta_neutral: always check git status, commit before deploy
- If anything fails, stop and report — don't continue to the next step
- After Docker daemon restart, verify ALL containers came back up before proceeding
- If a rollout restart fails, investigate before trying other updates
