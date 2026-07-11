# Homelab backup restore playbook

How to recover the homelab from a Cloudflare R2 backup after data loss or a
bare-metal rebuild. The daily backup (`~/homelab-backup/`, 03:00 UTC) produces
`homelab-backup-YYYYMMDD-HHMMSS.tar.gz` — a tar.gz of per-target directories.

**Each target is a top-level directory inside the archive.** Inspect one:

```bash
tar tzf homelab-backup-*.tar.gz | awk -F/ '{print $1}' | sort -u
```

## What an archive contains (22 targets)

| Group | Targets |
|---|---|
| App data | `blog-posts`, `blog-reviews`, `blog-images`, `blog-db`, `delta_neutral-db`, `agent-state` |
| FreshRSS | `freshrss-db`, `freshrss-config` |
| Open WebUI | `open-webui-db` |
| Config/code | `homelab-backup-config`, `k3s-manifests`, `host-etc`, `pkg-manifest` |
| Secrets (unencrypted) | `secrets-blog-master`, `secrets-delta-master`, `secrets-open-webui-env`, `secrets-cloudflare`, `secrets-dependabot`, `secrets-llm-proxy`, `secrets-pi-web`, `secrets-searxng`, `secrets-smtp-and-staged` |

DBs were captured with `sqlite3 .backup` + passed `PRAGMA integrity_check` at
backup time, and the restore drill re-checks integrity after download.

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
Reads the archive, lists the target manifest, and runs `PRAGMA integrity_check`
on every `*.db`/`*.sqlite3` inside. Exit 0 = all DBs intact.

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
sudo netplan apply          # brings up enp3s0f0 with .100/.92/.102
```
Without the static `.92` IP, k3s node-IP and blog/delta_neutral ingress break.

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
cp /tmp/restore/secrets-blog-master/master.key      ~/blog/blog/config/master.key
cp /tmp/restore/secrets-delta-master/master.key      ~/delta_neutral/delta_neutral/config/master.key
cp /tmp/restore/secrets-open-webui-env/.env          ~/open-webui/.env
cp -r /tmp/restore/secrets-cloudflare/*              ~/.config/cloudflare/
cp /tmp/restore/secrets-dependabot/env               ~/.config/dependabot-webhook/env
cp /tmp/restore/secrets-llm-proxy/env                ~/.config/llm-proxy/env
cp /tmp/restore/secrets-pi-web/config.json          ~/.config/pi-web/config.json
cp /tmp/restore/secrets-searxng/settings.yml         ~/searxng/core-config/settings.yml
cp /tmp/restore/secrets-smtp-and-staged/smtp_config ~/scripts/.smtp_config
chmod 600 ~/.config/cloudflare/* ~/.config/dependabot-webhook/env \
           ~/.config/llm-proxy/env ~/open-webui/.env ~/scripts/.smtp_config \
           ~/blog/blog/config/master.key ~/delta_neutral/delta_neutral/config/master.key
```
Also restore `~/homelab-backup/.env` (R2 creds) from `homelab-backup-config/`
**or** keep the fresh creds you made in step 0.

### 3e. App content + DBs (from data targets)
```bash
# Blog content
rsync -a /tmp/restore/blog-posts/  ~/blog/blog/app/posts/
rsync -a /tmp/restore/blog-reviews/ ~/blog/blog/app/reviews/
rsync -a /tmp/restore/blog-images/  ~/blog/blog/app/assets/images/

# Blog DB — restore into the container's volume
docker cp /tmp/restore/blog-db/production.sqlite3 blog-web-1:/rails/storage/production.sqlite3

# Delta neutral DB
docker cp /tmp/restore/delta_neutral-db/production.sqlite3 delta_neutral-web-1:/rails/storage/production.sqlite3

# Open WebUI DB
sudo cp /tmp/restore/open-webui-db/webui.db /var/lib/docker/volumes/open-webui_open-webui/_data/webui.db

# agent-state
rsync -a /tmp/restore/agent-state/ ~/agent-state/
```

### 3f. FreshRSS (k3s) — paths are in the freshrss PVC
The `freshrss-db` and `freshrss-config` targets restore into the k3s
local-path PVC. Locate the live PVC path (`k get pvc -n freshrss`) and copy back:
```bash
sudo cp /tmp/restore/freshrss-db/db.sqlite <pvc>/users/carter2099/db.sqlite
sudo rsync -a --delete /tmp/restore/freshrss-config/ <pvc>/
```

### 3g. Rebuild the homelab-backup binary (from `homelab-backup-config/`)
The archive contains your Go source + config. After restoring deps:
```bash
cd ~/homelab-backup && go build -o homelab-backup .
```

## 4. Restart services & verify
```bash
# k3s pods
k get pods -A
# host apps
docker compose -f ~/blog/docker-compose.yml up -d
docker compose -f ~/delta_neutral/docker-compose.yml up -d
# timers
systemctl --user start homelab-backup.timer llm-proxy.service pi-web.service
```

## 5. Prove the restore
Run the restore drill against the archive you just used (or the next daily):
```bash
bash ~/homelab-backup/restore-drill.sh
```

## Notes
- Secrets are stored **unencrypted** in R2. Bucket access = full compromise by
  design; protecting the bucket is the trust boundary.
- Retention: 14 daily + 1 monthly + 1 yearly (~240 MB, well under R2 free 10 GB).
- The Open WebUI `cache/` (1.1 GB of regenerable embeddings) is intentionally
  **not** backed up — only `webui.db` is. Re-open the UI to regenerate it.
- The `pkg-manifest/` target lists `dpkg --get-selections`, `apt-mark showmanual`,
  gem/pip/npm/rbenv/fnm versions, and enabled services — use it to reproduce the
  installed package set on a bare rebuild.