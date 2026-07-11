#!/usr/bin/env bash
# Weekly AGENTS.md truth-check: verifies ~/AGENTS.md against the live host and
# emails Carter a report (pass/drift/unverifiable per check, with proposed
# edits). REPORT-ONLY — never modifies files or touches git/dotfiles.
# Scheduled via systemd timer (agents-md-audit.timer), Saturdays 4am ET (08:00 UTC).

set -euo pipefail

export HOME="/home/carter"
export PATH="$HOME/.local/bin:$HOME/.rbenv/bin:$HOME/.rbenv/shims:$HOME/.fnm:$PATH"

TODAY="$(date +%Y-%m-%d)"

# NOTE: $(id -u) is intentionally left literal for the agent's own shell to expand.
PROMPT='You are a homelab maintenance agent running unattended via systemd. Your job: truth-check ~/AGENTS.md (path /home/carter/AGENTS.md) against the live host and email Carter a report. READ-ONLY: do NOT modify any files, do NOT run git, dotfiles, commit, systemctl restart, deploy, release.sh, up.sh, or any state-changing command. The only files you may write are your own scratch/email temp files under /tmp.

Context you must internalize: AGENTS.md was recently audited so that easily-verifiable specifics (Ruby versions, k3s version, per-app version pins, live container names, backup targets/retention, open-webui image tag, LLM model filename/context, Node version) were intentionally REMOVED and replaced with pointers (commands like "rbenv versions" / "docker ps" / "node -v", or source paths like ~/homelab-backup/config.yaml). So your job is NOT to re-add those values. Your job is to verify that (1) the pointers still resolve and (2) the remaining STRUCTURAL/SEMANTIC facts (IP roles, interface names, the two-pattern k3s deployment model, service+timer names and their schedules, ufw cni rules, docker daemon sole-ness, safety-rule applicability, CF token scope) still match reality.

FIRST COMMAND in every shell: export XDG_RUNTIME_DIR=/run/user/$(id -u)  (so systemctl --user works).

Step 1 — Read /home/carter/AGENTS.md in full. Build a mental list of (a) pointer targets and (b) structural/semantic claims to verify.

Step 2 — Pointer resolution check. Confirm each pointer target exists or runs. Sample-check at least these paths exist: /etc/netplan/50-cloud-init.yaml, /etc/rancher/k3s/config.yaml, /home/carter/k3s/config.yaml, /home/carter/homelab-backup/config.yaml, /home/carter/open-webui/docker-compose.yml, /home/carter/searxng/docker-compose.yml, /home/carter/searxng/settings.yml, /home/carter/.config/cloudflare/api-token, /home/carter/.config/pi-web/config.json, /home/carter/.config/llm-proxy/env, /home/carter/scripts/digest_runner.py, /home/carter/scripts/update_runner.py, /home/carter/scripts/send_digest.py, /home/carter/digests/template.html, /home/carter/.pi/agent/auth.json. Confirm these commands actually run: rbenv versions, node -v, docker ps, docker info, k get nodes, k3s --version, ip -4 addr show enp3s0f0. Confirm these systemd units exist: user units/timers via "systemctl --user list-units --all" and "systemctl --user list-timers --all" (look for homelab-backup, update-check, digests-daily, hyperliquid-sdk, llm-proxy, pi-web, pi-web-sessiond, dependabot-webhook); system units via "systemctl list-units --all" (docker.service, k3s.service).

Step 3 — Structural fact verification. Run live checks and compare to AGENTS.md:
 (a) Network: "ip -4 addr show enp3s0f0" and "ip route". Confirm .100 is DHCP/default-route source, .92 is k3s node IP + blog/delta_neutral ingress, .102 is tbitt/stickies ingress, wlp6s0 is DOWN. Cross-check /etc/netplan/50-cloud-init.yaml.
 (b) k3s config: /etc/rancher/k3s/config.yaml flannel-iface must equal enp3s0f0. "k get nodes" should show a Ready control-plane node. "k get pods -A" and "docker ps" together should confirm the two-pattern model (self-hosted docker-compose webapps: blog/delta_neutral; k3s-native third-party: grafana/prometheus/freshrss/uptime-kuma/traefik/node-exporter). hub/tbitt/stickies should NOT be running.
 (c) ufw cni rules: "sudo grep -E "cni0|flannel" /etc/ufw/user.rules" should show ACCEPT rules for cni0 and flannel.1.
 (d) Docker daemon sole-ness: "docker info --format {{.DockerRootDir}}" must equal /var/lib/docker. Confirm no snap docker.
 (e) Ports: "docker ps --format" ports should match AGENTS.md documented ports (blog 33099->3099, delta 43080->80, open-webui 127.0.0.1:48100, searxng 127.0.0.1:8080, pi-web 127.0.0.1:8504).
 (f) Schedule drift: "systemctl --user list-timers --all" — confirm each timer name fires at the documented UTC OnCalendar (homelab-backup 03:00, update-check 05:00, digests-daily 08:00, hyperliquid-sdk Mon/Thu 08:00). Note if the NEXT firing deviates.
 (g) CF token scope: curl the access/apps endpoint with the token and confirm it still returns 403 (read the token from ~/.config/cloudflare/api-token, account-id from ~/.config/cloudflare/account-id).

Step 4 — For EVERY check assign a status: PASS (matches reality), DRIFT (pointer broken or structural fact contradicted by reality), or UNVERIFIABLE (state why: e.g. needs sudo you cannot justify, would be destructive, service down). For each DRIFT, propose a specific edit as OLD_TEXT -> NEW_TEXT (quote the exact lines from AGENTS.md that need changing). Do NOT apply any edit.

Step 5 — Build a concise, skimmable HTML email body. Start with a one-line summary: TOTAL / PASS / DRIFT / UNVERIFIABLE counts and the run timestamp. Then a section per category (Pointer Resolution, Network, k3s, ufw, Docker, Ports, Schedules, CF token). For each item: a status badge, a one-line evidence note, and (for DRIFT) the proposed old->new edit. Style pass=green, drift=red, unverifiable=gray. Keep it readable on mobile. Write the HTML to /tmp/agents_md_audit_'"$TODAY"'.html.

Step 6 — Send the email:
 python3 /home/carter/scripts/send_digest.py --subject "AGENTS.md Audit '"$TODAY"' — <PASS>pass/<DRIFT>drift/<UNV>unverifiable" --body-file /tmp/agents_md_audit_'"$TODAY"'.html --to carter2099@pm.me
(Replace <PASS>/<DRIFT>/<UNV> in the subject with the real counts.)

Constraints recap: read-only, no file edits to AGENTS.md or anything outside /tmp, no git/dotfiles, no restarts/deploys. If a check risks being destructive or you cannot justify its effect with certainty, mark UNVERIFIABLE rather than running it — never guess. Always export XDG_RUNTIME_DIR=/run/user/$(id -u) before any systemctl --user command.'

START_TS="$(date +%s)"

pi -p --model opencode-go/deepseek-v4-pro --session-dir ~/.pi/agent/sessions-automated "$PROMPT"

END_TS="$(date +%s)"
mkdir -p "$HOME/digests/agents-md-audit"
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) agents-md-audit duration=$((END_TS - START_TS))s model=deepseek-v4-pro" >> "$HOME/digests/agents-md-audit/.runs.log"