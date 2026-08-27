# Homelab backup restore playbook

How to recover the homelab from a Cloudflare R2 backup after data loss or a
bare-metal rebuild. The daily backup (`~/homelab-backup/`, 03:00 UTC) produces
`homelab-backup-YYYYMMDD-HHMMSS.tar.gz` — a tar.gz of per-target directories.

**Each target is a top-level directory inside the archive.** Inspect one:

```bash
tar tzf homelab-backup-*.tar.gz | awk -F/ '{print $1}' | sort -u
```

## What an archive contains (23 targets)

| Group | Targets |
|---|---|
| App data | `blog-posts`, `blog-reviews`, `blog-images`, `blog-db`, `agent-state`, `omp-agent-state`, `daily-news-data` |
| FreshRSS | `freshrss-db`, `freshrss-config` |
| Open WebUI | `open-webui-db` |
| Config/code | `homelab-backup-config`, `k3s-manifests`, `omp-web-app`, `host-etc`, `pkg-manifest` |
| Secrets (unencrypted) | `secrets-blog-master`, `secrets-open-webui-env`, `secrets-cloudflare`, `secrets-dependabot`, `secrets-llm-proxy`, `secrets-opencode-go-proxy`, `secrets-searxng`, `secrets-smtp-and-staged` |

Database targets were captured with `sqlite3 .backup` and passed
`PRAGMA integrity_check` at backup time. `verify` rechecks every embedded SQLite file,
but does not compare the archive against the configured 23-target list or prove
non-database target completeness.

## 0. Get the archive locally

You need fresh R2 credentials first — the backup's own `.env` is *inside* the
archive (chicken-and-egg), so bootstrap from the Cloudflare dashboard:

1. Cloudflare dashboard → R2 → manage API tokens → create R2 read/write creds.
2. Export them:
   ```bash
   export R2_ACCESS_KEY_ID=...
   export R2_SECRET_ACCESS_KEY=...
   ```
3. Download the newest backup to the rebuilt host:
   ```bash
   ~/homelab-backup/homelab-backup latest /tmp/
   # prints /tmp/homelab-backup-YYYYMMDD-HHMMSS.tar.gz
   ```
   (On a truly bare host with no binary yet, use `rclone`/`aws s3 cp` with the
   R2 endpoint + the bucket name `homelab-backup`. Or rebuild the binary from
   the `homelab-backup-config` target first — see step 3.)

## 1. Verify before trusting

```bash
~/homelab-backup/homelab-backup verify /tmp/homelab-backup-*.tar.gz
```
Reads the archive, lists the top-level directories present, and runs
`PRAGMA integrity_check` on every embedded SQLite file. Exit 0 means the tar is readable
and those databases pass; even an archive with missing targets or no databases can pass.

## 2. Extract

```bash
mkdir -p /tmp/restore && tar xzf /tmp/homelab-backup-*.tar.gz -C /tmp/restore
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

### 3b. k3s config (from `host-etc/`)
```bash
sudo mkdir -p /etc/rancher/k3s
sudo cp /tmp/restore/host-etc/config.yaml /etc/rancher/k3s/config.yaml
# flannel-iface must be enp3s0f0 (WiFi is down). Verify before starting k3s.
```
Then install/restart k3s. Regenerate the cluster if needed; re-apply manifests
from the `k3s-manifests/` target.

### 3c. ufw rules (from `host-etc/`)
```bash
sudo cp /tmp/restore/host-etc/user.rules /etc/ufw/user.rules
sudo ufw reload
```
This restores the `cni0` / `flannel.1` INPUT allow rules. **Without these, pods
can't reach the host → Traefik loads no ingresses → 404 on every k3s host.**

### 3d. Secrets (from `secrets-*/`)
```bash
cp /tmp/restore/secrets-blog-master/master.key       ~/blog/blog/config/master.key
cp /tmp/restore/secrets-open-webui-env/.env          ~/open-webui/.env
cp -r /tmp/restore/secrets-cloudflare/*              ~/.config/cloudflare/
cp /tmp/restore/secrets-dependabot/env                ~/.config/dependabot-webhook/env
cp /tmp/restore/secrets-llm-proxy/env                 ~/.config/llm-proxy/env
cp /tmp/restore/secrets-opencode-go-proxy/config.json ~/.config/opencode-go-proxy/config.json
cp /tmp/restore/secrets-searxng/settings.yml          ~/searxng/core-config/settings.yml
cp /tmp/restore/secrets-smtp-and-staged/smtp_config  ~/scripts/.smtp_config
chmod 600 ~/.config/cloudflare/* ~/.config/dependabot-webhook/env \
           ~/.config/llm-proxy/env ~/.config/opencode-go-proxy/config.json \
           ~/open-webui/.env ~/scripts/.smtp_config \
           ~/blog/blog/config/master.key
```
Also restore `~/homelab-backup/.env` (R2 creds) from `homelab-backup-config/`
**or** keep the fresh creds you made in step 0.

### 3e. App content + DBs (from data targets)
```bash
# Stop database writers before replacing their files
docker stop blog-web-1 open-webui || true

# Blog content
rsync -a /tmp/restore/blog-posts/  ~/blog/blog/app/posts/
rsync -a /tmp/restore/blog-reviews/ ~/blog/blog/app/reviews/
rsync -a /tmp/restore/blog-images/  ~/blog/blog/app/assets/images/

# Blog DB — restore into the container's volume
docker cp /tmp/restore/blog-db/production.sqlite3 blog-web-1:/rails/storage/production.sqlite3

# Open WebUI DB
sudo cp /tmp/restore/open-webui-db/webui.db /var/lib/docker/volumes/open-webui_open-webui/_data/webui.db

# agent-state
rsync -a /tmp/restore/agent-state/ ~/agent-state/

# OMP config/runtime state; interactive and automated transcript trees were excluded
mkdir -p ~/.omp/agent
rsync -a /tmp/restore/omp-agent-state/ ~/.omp/agent/

# Daily News publications, attention observations, and mail markers
mkdir -p ~/digests/news
rsync -a /tmp/restore/daily-news-data/ ~/digests/news/
NEWS_DATE="$(python3 -c 'import json; from pathlib import Path; p=json.loads((Path.home() / "digests/news/publications/manifest.json").read_text()); print(max(x["date"] for x in p["dates"]))')"
python3 ~/scripts/news_publish.py --date "$NEWS_DATE" --skip-email
# Per-category stories-in-flight.json trackers are not in daily-news-data.
```

### 3f. FreshRSS (k3s) — paths are in the FreshRSS PVC
Restore the raw PVC tree first and the consistent database snapshot last, while the pod
is stopped. Locate the live PVC path (`kubectl get pvc -n freshrss`) and substitute it
for `<pvc>`:
```bash
kubectl -n freshrss scale deployment/freshrss --replicas=0
sudo rsync -a --delete /tmp/restore/freshrss-config/ <pvc>/
sudo rm -f <pvc>/users/carter2099/db.sqlite-wal <pvc>/users/carter2099/db.sqlite-shm
sudo install -o 33 -g 33 -m 664 /tmp/restore/freshrss-db/db.sqlite <pvc>/users/carter2099/db.sqlite
kubectl -n freshrss scale deployment/freshrss --replicas=1
```

### 3g. Restore and rebuild homelab-backup
The archive contains the deployed Go source and config:
```bash
mkdir -p ~/homelab-backup
rsync -a /tmp/restore/homelab-backup-config/ ~/homelab-backup/
cd ~/homelab-backup && go build -o homelab-backup .
```

### 3h. Restore OMP Web source
`omp-web-app` is the intentionally uncommitted OMP port source:
```bash
mkdir -p ~/dev/omp-web-worktrees/phase0
rsync -a /tmp/restore/omp-web-app/ ~/dev/omp-web-worktrees/phase0/
```
Build, validate, and install its private artifact using
`~/notes/docs/homelab/omp-web.md`; do not substitute the retired Pi Web service.

## 4. Restart services & verify
```bash
# k3s pods
k get pods -A
# host apps
bash ~/blog/up.sh
bash ~/open-webui/up.sh
# timers and user services
systemctl --user start homelab-backup.timer llm-proxy.service \
  omp-web-sessiond.service omp-web.service
```

## 5. Prove the restore

The `verify` command in step 1 checked the selected archive. Run application health and
behavioral checks against the restored services. `restore-drill.sh` is a separate monthly
check that always downloads and verifies the **newest** R2 object; it does not select or
prove an older archive used above.

## Notes
- Secrets are stored **unencrypted** in R2. Bucket access = full compromise by
  design; protecting the bucket is the trust boundary.
- Retention baseline: 14 scheduled dailies + 1 monthly + 1 yearly (about 1.0 GB at roughly 55–63 MB each). Manual runs remain as additional daily objects until the 14-day window expires.
- The Open WebUI `cache/` (1.1 GB of regenerable embeddings) is intentionally
  **not** backed up — only `webui.db` is. Re-open the UI to regenerate it.
- The `pkg-manifest/` target lists `dpkg --get-selections`, `apt-mark showmanual`,
  gem/pip/npm/rbenv/fnm versions, and enabled services — use it to reproduce the
  installed package set on a bare rebuild.
- `verify` validates archive readability and embedded databases, not the configured target count or every non-database file. Confirm the expected 23 top-level targets before a destructive restore.