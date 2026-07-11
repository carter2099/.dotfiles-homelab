#!/usr/bin/env bash
# Pre-collection step run before the backup binary.
# Gathers root-owned host config + package manifest + the scattered SMTP
# secret into a carter-owned staging tree the backup binary then archives.
#
# Runs as the homelab-backup.service user (carter) via ExecStartPre.
# Passwordless sudo is available (/etc/sudoers.d/carter-agent NOPASSWD: ALL).
set -euo pipefail

STAGING="$HOME/homelab-backup-staging"
rm -rf "$STAGING"
mkdir -p "$STAGING/host-etc" "$STAGING/pkg-manifest" "$STAGING/secrets"

# --- Host system config (root-owned, not on GitHub, not readable by carter) ---
echo "[pre-collect] host /etc files"
for f in \
  /etc/netplan/50-cloud-init.yaml \
  /etc/rancher/k3s/config.yaml \
  /etc/ufw/user.rules
do
  if sudo test -r "$f"; then
    sudo cp "$f" "$STAGING/host-etc/$(basename "$f")"
    sudo chown carter:carter "$STAGING/host-etc/$(basename "$f")"
    sudo chmod 600 "$STAGING/host-etc/$(basename "$f")"
  else
    echo "[pre-collect] WARN: $f not readable, skipping"
  fi
done

# --- Package manifest (for bare-metal OS rebuild) ---
echo "[pre-collect] package manifest"
dpkg --get-selections                    > "$STAGING/pkg-manifest/dpkg-selections.txt" 2>/dev/null || true
apt-mark showmanual                      > "$STAGING/pkg-manifest/apt-manual.txt"       2>/dev/null || true
gem list                                 > "$STAGING/pkg-manifest/gem-list.txt"         2>/dev/null || true
pip3 list 2>/dev/null                    > "$STAGING/pkg-manifest/pip3-list.txt"        2>/dev/null || true
npm ls -g --depth=0 2>/dev/null           > "$STAGING/pkg-manifest/npm-global.txt"       2>/dev/null || true
rbenv versions 2>/dev/null               > "$STAGING/pkg-manifest/rbenv-versions.txt"   2>/dev/null || true
fnm list 2>/dev/null                     > "$STAGING/pkg-manifest/fnm-node-versions.txt" 2>/dev/null || true
systemctl --user list-units --type=service --state=enabled --no-pager 2>/dev/null \
                                          > "$STAGING/pkg-manifest/user-services-enabled.txt" || true
systemctl list-unit-files --type=service --state=enabled --no-pager 2>/dev/null \
                                          > "$STAGING/pkg-manifest/system-services-enabled.txt" || true

# --- Scattered secret file that lives inside an otherwise-already-covered dir ---
# ~/scripts/ is dotfiles-tracked (on GitHub); only .smtp_config is the secret.
echo "[pre-collect] scattered secret files"
cp "$HOME/scripts/.smtp_config" "$STAGING/secrets/smtp_config" 2>/dev/null || \
  echo "[pre-collect] WARN: ~/scripts/.smtp_config missing"

echo "[pre-collect] done"