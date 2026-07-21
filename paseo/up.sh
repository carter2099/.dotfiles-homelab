#!/usr/bin/env bash
# Paseo daemon + web UI (replaces pi-web)
# Access via CF tunnel at paseo.carter2099.com -> loopback 6767
set -euo pipefail
cd "$(dirname "$0")"

echo "=== Building paseo-local image ==="
docker build -t paseo-local .

echo "=== Starting Paseo ==="
docker compose up -d

echo ""
echo "Paseo running at http://localhost:6767"
echo "CF tunnel: https://paseo.carter2099.com"
