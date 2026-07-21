#!/bin/bash
# Fix ownership of omp agent directory before base entrypoint drops to paseo user.
# The bind-mounted omp-config may have root-owned files from a previous run.
set -e

OMP_DIR="/home/paseo/.omp"
if [ -d "$OMP_DIR" ]; then
    chown -R paseo:paseo "$OMP_DIR" 2>/dev/null || true
fi

# Also fix paseo home generally
chown -R paseo:paseo /home/paseo 2>/dev/null || true

exec /usr/bin/tini -- /usr/local/bin/paseo-docker-entrypoint "$@"
