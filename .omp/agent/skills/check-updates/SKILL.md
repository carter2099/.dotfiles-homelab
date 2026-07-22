---
name: check-updates
description: Audit the homelab for outdated software and report findings. Use when user says "check for updates", "what's outdated", "audit the homelab", "run update check", or wants a system health report.
---

# Check Updates

Audit every layer of the homelab for outdated software and report findings directly to the user. This is the interactive equivalent of the nightly update-check agent — but report-only, no auto-apply.

## Step 1: System packages (apt)

```bash
apt list --upgradable 2>/dev/null
```

Categorize:
- **Auto-applied nightly:** docker-ce, docker-ce-cli, containerd.io, docker-buildx-plugin, docker-compose-plugin, cloudflared (these are auto-applied by the nightly update agent at 1am ET with rollback on failure)
- **Routine:** everything else (libs, tools)

Note the count and highlight any security-related or kernel packages.

## Step 2: Snap packages

```bash
snap refresh --list 2>/dev/null
```

Flag the docker snap separately — never refresh it (AppArmor risk). Other snaps are safe.

## Step 3: Docker images — Compose apps

```bash
docker images --format '{{.Repository}}:{{.Tag}} {{.CreatedAt}}' | grep -E "blog|delta|webui"
docker ps --format '{{.Names}} {{.Image}} {{.Status}}'
```

- **open-webui:** pinned tag. Use web_search ("open-webui latest release") to check for newer than v0.10.2.
- **blog-web:** locally built. Note build age. Flag if >2 weeks.
- **delta_neutral-web:** locally built. Note build age. Flag if >2 weeks.

## Step 4: Docker images — k3s workloads

```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u)
/usr/local/bin/k3s kubectl get deploy,ds,sts -A -o wide 2>/dev/null
```

- **freshrss:** `:latest` tag — the nightly agent does a rollout restart to pull latest. Note when it last restarted (check pod age).
- **uptime-kuma:** `:1` tag — same pattern as freshrss.
- **traefik:** `rancher/mirrored-library-traefik:3.3.6`. Use web_search to check for newer 3.3.x.
- **kube-system images:** tied to k3s version. Note them but no action needed unless upgrading k3s.

## Step 5: npm global packages

```bash
npm outdated -g 2>/dev/null || echo "(all up to date or check unavailable)"
```

## Step 6: Language runtimes

```bash
go version
rbenv versions 2>/dev/null
node --version
```

Use web_search to check latest stable for each. Flag anything more than one minor version behind. These are manual-only — the nightly agent never touches them.

## Step 7: Infrastructure

```bash
/usr/local/bin/k3s --version 2>/dev/null | head -1
nvim --version 2>/dev/null | head -1
cd ~/dev/dependabot-webhook && git fetch origin 2>/dev/null && git status -sb 2>/dev/null | head -3
```

Check k3s and neovim against latest releases via web_search. Check if dependabot-webhook dev clone is behind origin.

## Step 8: System health

```bash
df -h / /home
free -h
uptime
test -f /var/run/reboot-required && echo "REBOOT REQUIRED" || echo "No reboot required"
docker ps -a --format '{{.Names}} {{.Status}}'
/usr/local/bin/k3s kubectl get pods -A --no-headers | grep -v -E "Running|Completed" || echo "(all pods healthy)"
```

## Step 9: Report to user

Present findings in a clean format:

```
## Homelab Audit — <date>

### 🔴 Needs Attention
- item — current → target — reason

### 🟡 Behind but Safe
- item — current → target

### 🟢 Up to Date
- item, item, item

### 📊 System Health
- Disk: X% on /
- Memory: X available
- Uptime: X days
- Reboot needed: yes/no
- Containers: X running, Y issues
```

If there are actionable items in 🔴 or 🟡, suggest: "Run `/update-homelab` to apply these."

## Step 10: Write session memoir

Write a brief session memoir to `~/notes/sessions/YYYY-MM-DD.md` per the Persistent Memory convention in AGENTS.md.

## Notes

- This skill is report-only. It never installs, upgrades, or restarts anything.
- The nightly agent (update-check.timer, 1am ET) is a deterministic Python orchestrator (`update_runner.py`). It auto-applies apt upgrades, Docker engine/plugins, cloudflared, open-webui stable tags, and k3s workload restarts — each with pre-version capture and automatic rollback on tunnel failure. It emails a full HTML report daily.
- Use web_search sparingly — only for items where the user would reasonably care about being behind (open-webui, traefik, Go, neovim, k3s). Skip web_search for routine apt packages.
