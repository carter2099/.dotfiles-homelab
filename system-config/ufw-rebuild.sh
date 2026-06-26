#!/bin/bash
# Rebuild UFW firewall rules for homelab (idempotent — safe to re-run)
# Default policies are Ubuntu defaults: deny incoming, allow outgoing, deny routed
# See /etc/default/ufw

set -e

echo "=== Ensuring UFW is installed ==="
if ! command -v ufw &>/dev/null; then
    sudo apt-get update && sudo apt-get install -y ufw
fi

echo "=== Allowing SSH from LAN ==="
sudo ufw allow from 192.168.4.0/22 to any port 22 proto tcp

echo "=== Enabling UFW ==="
sudo ufw --force enable

echo "=== Current rule set ==="
sudo ufw status verbose

echo ""
echo "Done. Next: install docker-user-rules.service + script to block LAN Docker ports."
