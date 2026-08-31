# Homelab backup restore playbook

How to restore archived data and host-local configuration after data loss, or as the
data layer of a bare-metal rebuild. This archive is **not** a standalone machine image:
restore the base OS/toolchain, the dotfiles worktree, and Git-backed app/development
checkouts first. The daily job produces
`homelab-backup-YYYYMMDD-HHMMSS.tar.gz`, with one directory per configured target.

Tracked scripts, Compose files, systemd units, notes, and most application source come
from GitHub, not this archive. On a bare host, recreate the home bare-dotfiles worktree
and the `~/dev/`/deployment checkouts described in `AGENTS.md` before running app restore
commands. After setting `$ARCHIVE` in step 0, inspect its target names with:

```bash
tar tzf "$ARCHIVE" | awk -F/ '{print $1}' | sort -u
```

## What an archive contains (22 targets)

| Group | Targets |
|---|---|
| App data | `blog-posts`, `blog-reviews`, `blog-images`, `blog-db`, `agent-state`, `omp-agent-state`, `daily-news-data` |
| FreshRSS | `freshrss-db`, `freshrss-config` |
| Open WebUI | `open-webui-db` |
| Config/code | `homelab-backup-config`, `k3s-manifests`, `host-etc`, `pkg-manifest` |
| Secrets (unencrypted) | `secrets-blog-master`, `secrets-open-webui-env`, `secrets-cloudflare`, `secrets-dependabot`, `secrets-llm-proxy`, `secrets-opencode-go-proxy`, `secrets-searxng`, `secrets-smtp-and-staged` |

For database targets **present and successfully collected**, dedicated snapshots passed
`PRAGMA integrity_check` at backup time. `verify` rechecks every embedded SQLite file,
but does not compare the archive against the configured 22-target list, detect an empty
failed-target directory, or prove non-database target completeness.

## 0. Select one archive

Protect the credential-bearing local archive and extracted tree. Run every recovery block
in the same Bash shell and stop on the first error:
```bash
set -euo pipefail
umask 077
export R2_ACCESS_KEY_ID=...
export R2_SECRET_ACCESS_KEY=...
ARCHIVE="$(~/homelab-backup/homelab-backup latest /tmp/)"
test -f "$ARCHIVE"
```

Fresh R2 credentials may be created in Cloudflare Dashboard → R2 → Manage API Tokens.
The archived `~/homelab-backup/.env` cannot help until after download. On a truly bare
host, first clone `carter2099/homelab-backup` over HTTPS and build the binary, or use an
S3-compatible client against the documented R2 endpoint; then set
`ARCHIVE=/tmp/homelab-backup-YYYYMMDD-HHMMSS.tar.gz` explicitly. Carry that exact
variable through every later step—never use a wildcard that can select multiple archives.

## 1. Verify before trusting

```bash
~/homelab-backup/homelab-backup verify "$ARCHIVE"
```

This proves the tar is readable and every embedded SQLite file passes. It can still pass
with missing/empty targets or no databases. Compare the printed top-level manifest with
the 22 expected names above and inspect critical non-database targets before a destructive
restore.

## 2. Extract privately

```bash
umask 077
rm -rf /tmp/restore
mkdir -p /tmp/restore
tar xzf "$ARCHIVE" -C /tmp/restore
```

## 3. Restore in dependency order

**Network first** (so k3s and apps can talk), then k3s, then ufw, then secrets,
then app data, then DBs.

### 3a. Host networking (from `host-etc/`)
```bash
sudo cp /tmp/restore/host-etc/50-cloud-init.yaml /etc/netplan/50-cloud-init.yaml
sudo netplan apply          # DHCP address plus static 192.168.4.92
```
Without the static `.92` IP, k3s node-IP and blog ingress break.

### 3b. k3s config and manifests
```bash
sudo mkdir -p /etc/rancher/k3s
sudo cp /tmp/restore/host-etc/config.yaml /etc/rancher/k3s/config.yaml
# flannel-iface must be enp3s0f0. Install/restart k3s before applying resources.
sudo systemctl restart k3s
sudo k3s kubectl wait --for=condition=Ready node --all --timeout=180s

mkdir -p ~/k3s
rsync -a /tmp/restore/k3s-manifests/ ~/k3s/
sudo k3s kubectl apply -f ~/k3s/traefik/traefik-helmchartconfig.yaml
sudo k3s kubectl apply -f ~/k3s/blog/deploy.yaml
sudo k3s kubectl create namespace freshrss --dry-run=client -o yaml | \
  sudo k3s kubectl apply -f -
sudo k3s kubectl apply -f ~/k3s/freshrss/
```

### 3c. ufw rules (from `host-etc/`)
```bash
sudo cp /tmp/restore/host-etc/user.rules /etc/ufw/user.rules
sudo ufw reload
```
This restores the `cni0` / `flannel.1` INPUT allow rules. **Without these, pods
can't reach the host → Traefik loads no ingresses → 404 on every k3s host.**

### 3d. Secrets (from `secrets-*/`)
Create the destination directories first, then restore:
```bash
mkdir -p ~/blog/blog/config ~/open-webui ~/searxng/core-config ~/scripts \
  ~/.config/{cloudflare,dependabot-webhook,llm-proxy,opencode-go-proxy}
cp /tmp/restore/secrets-blog-master/master.key       ~/blog/blog/config/master.key
cp /tmp/restore/secrets-open-webui-env/.env          ~/open-webui/.env
rsync -a /tmp/restore/secrets-cloudflare/             ~/.config/cloudflare/
rsync -a /tmp/restore/secrets-dependabot/             ~/.config/dependabot-webhook/
cp /tmp/restore/secrets-llm-proxy/env                 ~/.config/llm-proxy/env
cp /tmp/restore/secrets-opencode-go-proxy/config.json ~/.config/opencode-go-proxy/config.json
cp /tmp/restore/secrets-searxng/settings.yml          ~/searxng/core-config/settings.yml
cp /tmp/restore/secrets-smtp-and-staged/smtp_config  ~/scripts/.smtp_config
chmod 600 ~/.config/cloudflare/* ~/.config/dependabot-webhook/env \
  ~/.config/llm-proxy/env ~/.config/opencode-go-proxy/config.json \
  ~/open-webui/.env ~/scripts/.smtp_config ~/blog/blog/config/master.key
```

`/etc/cloudflared/token` is root-owned and **not backed up**. Provision/rotate the named
tunnel token from Cloudflare, install it as `root:root` mode 0600, and restart the system
`cloudflared.service`. Restore `~/homelab-backup/.env` later from
`homelab-backup-config/`, or retain the fresh R2 credentials from step 0.

The Git-backed deployment directories must already exist. Run this from a plain SSH shell,
not an OMP session. Confirm Docker is reachable and stop existing database writers:
```bash
set -euo pipefail
docker info >/dev/null
for container in blog-web-1 open-webui; do
  if docker container inspect "$container" >/dev/null 2>&1; then
    docker stop "$container"
  fi
done
if pgrep -af '[o]mp( |$)' >/dev/null; then
  echo "stop ordinary OMP CLI writers before restoring ~/.omp/agent" >&2
  exit 1
fi

# Blog content and primary DB (storage is a host bind mount)
rsync -a /tmp/restore/blog-posts/   ~/blog/blog/app/posts/
rsync -a /tmp/restore/blog-reviews/ ~/blog/blog/app/reviews/
rsync -a /tmp/restore/blog-images/  ~/blog/blog/app/assets/images/
rm -f ~/blog/blog/storage/production.sqlite3-wal \
  ~/blog/blog/storage/production.sqlite3-shm
install -o carter -g carter -m 644 /tmp/restore/blog-db/production.sqlite3 \
  ~/blog/blog/storage/production.sqlite3

# Open WebUI volume and DB
docker network inspect homelab-chat-search >/dev/null 2>&1 || \
  docker network create homelab-chat-search
docker volume create open-webui_open-webui >/dev/null
sudo rm -f /var/lib/docker/volumes/open-webui_open-webui/_data/webui.db-wal \
  /var/lib/docker/volumes/open-webui_open-webui/_data/webui.db-shm
sudo install -o root -g root -m 644 /tmp/restore/open-webui-db/webui.db \
  /var/lib/docker/volumes/open-webui_open-webui/_data/webui.db

# Agent state
rsync -a /tmp/restore/agent-state/ ~/agent-state/
mkdir -p ~/.omp/agent
find ~/.omp/agent -type f \( -name '*.db-wal' -o -name '*.db-shm' \) -delete
rsync -a /tmp/restore/omp-agent-state/ ~/.omp/agent/

# Daily News publications, attention observations, and mail markers. The normal
# publisher imports topic dirs, which are not backed up; build directly from the archive.
mkdir -p ~/digests/news
rsync -a /tmp/restore/daily-news-data/ ~/digests/news/
python3 -c 'from pathlib import Path; import sys; sys.path.insert(0, str(Path.home() / "scripts")); import news_publish as n; editions=n.load_publications(); assert editions; print(n.build_site(editions, n.NEWS_DIR, n.ASSET_DIR))'
```

Not restored here: Blog cache/queue/cable DBs, Beatz `plays.jsonl`, Daily News
`stories-in-flight.json`, and OMP CLI transcript trees. They are not backup targets.

### 3f. FreshRSS PVC
Restore the raw tree first and the consistent database snapshot last. Resolve the
local-path PV host directory explicitly and wait for the writer pod to exit:
```bash
set -euo pipefail
sudo k3s kubectl -n freshrss scale deployment/freshrss --replicas=0
PODS="$(sudo k3s kubectl -n freshrss get pod -l app=freshrss -o name)"
if [ -n "$PODS" ]; then
  sudo k3s kubectl -n freshrss wait --for=delete pod -l app=freshrss --timeout=120s
fi
PV="$(sudo k3s kubectl -n freshrss get pvc freshrss-data -o jsonpath='{.spec.volumeName}')"
PVC_PATH="$(sudo k3s kubectl get pv "$PV" -o jsonpath='{.spec.local.path}')"
case "$PVC_PATH" in
  /var/lib/rancher/k3s/storage/*) ;;
  *) echo "unsafe FreshRSS PV path: $PVC_PATH" >&2; exit 1 ;;
esac
sudo rsync -a --delete /tmp/restore/freshrss-config/ "$PVC_PATH/"
sudo chown -R 33:33 "$PVC_PATH"
sudo rm -f "$PVC_PATH/users/carter2099/db.sqlite-wal" \
  "$PVC_PATH/users/carter2099/db.sqlite-shm"
sudo install -o 33 -g 33 -m 664 /tmp/restore/freshrss-db/db.sqlite \
  "$PVC_PATH/users/carter2099/db.sqlite"
sudo k3s kubectl -n freshrss scale deployment/freshrss --replicas=1
sudo k3s kubectl -n freshrss rollout status deployment/freshrss --timeout=180s
```

### 3g. Restore and rebuild homelab-backup
Directory target modes are normalized during collection, so reset secrets and executable
bits explicitly:
```bash
mkdir -p ~/homelab-backup
rsync -a /tmp/restore/homelab-backup-config/ ~/homelab-backup/
chmod 600 ~/homelab-backup/.env
chmod +x ~/homelab-backup/{pre-collect.sh,restore-drill.sh,email-template.sh,notify-failure.sh}
cd ~/homelab-backup && go build -o homelab-backup .
```

The backup service also depends on Carter's root-owned privilege policy, which is not in
the archive. Recreate and validate it before enabling the timer:
```bash
printf '%s\n' 'Defaults:carter !requiretty' \
  'carter ALL=(ALL) NOPASSWD: ALL' | sudo tee /etc/sudoers.d/carter-agent >/dev/null
sudo chmod 0440 /etc/sudoers.d/carter-agent
sudo visudo -cf /etc/sudoers.d/carter-agent
```

## 4. Reinstall, enable, and verify services

Rebuild/install each custom binary from its `~/dev/<repo>/` checkout using the subsystem
runbook. Restore `~/beatz-selected/{audio,artwork}/` from its independent source before
starting Beatz; otherwise leave it stopped. Then start the recovered container apps:
```bash
(cd ~/blog && bash up.sh)
(cd ~/searxng && bash up.sh)
(cd ~/open-webui && bash up.sh)
(cd ~/news && bash up.sh)
sudo k3s kubectl get pods -A
```

After the dotfiles unit files and custom binaries are restored, preserve the user manager
across logout/reboot and enable the recovered units:
```bash
sudo loginctl enable-linger carter
systemctl --user daemon-reload
systemctl --user enable --now \
  dependabot-webhook.service llm-proxy.service opencode-go-proxy.service \
  rig-dashboard.service cleanup-rig-requests.timer digests-daily.timer \
  homelab-backup.timer homelab-backup-restore-drill.timer \
  homelab-steward.timer hyperliquid-sdk.timer
# Recreate ~/dev/prompt-guard/.env (HF_TOKEN and service settings) before enabling prompt-guard.
```

Use `pkg-manifest/` plus the tracked `default.target.wants/` and
`timers.target.wants/` links as the full enablement checklist; `systemctl start` alone
does not persist a unit across reboot.

## 5. Prove the restore

The `verify` command in step 1 checked the selected archive. Run application health and
behavioral checks against the restored services. `restore-drill.sh` is a separate monthly
check that always downloads and verifies the **newest** R2 object; it does not select or
prove an older archive used above.

## Notes
- Secrets are **unencrypted** in R2, local archives are normally mode 0644, and directory
  source modes are not preserved. Bucket access is the trust boundary; use `umask 077`
  while recovering and reset modes as shown.
- Retention baseline: 14 scheduled dailies + 1 monthly + 1 yearly. Manual runs remain
  additional daily objects until expiry.
- The Open WebUI cache is intentionally excluded. Beatz play history and the entire
  `~/beatz-selected/` media/artwork library, Blog's secondary SQLite DBs, Daily News
  trackers, Prompt-Guard's `.env`, the backup sudoers policy, and the cloudflared tunnel
  token are also outside the current target set.
- `pkg-manifest/` inventories packages and records service command output, but Git-backed
  unit definitions/enablement links remain authoritative for reconstruction.
- `verify` validates archive readability and embedded databases, not the configured target
  count or every non-database file. Confirm all 22 names and inspect critical targets
  before a destructive restore; partial archives are uploaded when a target fails.