# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

**Maintenance:** Keep this file up to date. When deploying a new app, adding a service, changing ports/IPs, or making any structural changes to the homelab, update the relevant sections here as part of that work.

## Overview

Single-node homelab running on Ubuntu Server (2017 MacBook Pro, Intel i5, 8GB RAM). A k3s Kubernetes cluster routes traffic via Traefik ingress to apps running in Docker Compose on the host machine.

## Repository Structure

This is the home directory, managed as a bare git repo for dotfiles:
- `blog/` - Rails 8 blog app (blog.carter2099.com)
- `hub/` - React + Rails API landing page/portfolio (carter2099.com)
- `tbitt/` - React + Express memecoin tracker, **deprecated** (tbitt.carter2099.com)
- `stickies/` - Sticky notes app (stickiesapi.carter2099.com)
- `delta_neutral/` - Rails 8 Hyperliquid rebalancer (deltaneutral.carter2099.com)
- `k3s/` - Kubernetes manifests organized by service
- `ddns/` - Cloudflare DDNS updater for WireGuard endpoint
- `build/` - Source builds (neovim)
- `.dotfiles-homelab/` - Bare git repo tracking dotfiles

## Dotfiles Management

```bash
# The 'dotfiles' alias manages the bare repo
dotfiles status
dotfiles add <file>
dotfiles commit -m "message"
dotfiles push
```

Alias defined in `.zshrc`: `dotfiles='/usr/bin/git --git-dir="$HOME/.dotfiles-homelab/" --work-tree="$HOME"'`

## App Deployment Pattern

All apps follow the same deploy flow:
1. `release.sh` - pulls latest code, tears down containers, removes old images, calls `up.sh`
2. `up.sh` - starts Docker Compose in detached mode with production config

Rails apps (blog, hub) pass `RAILS_MASTER_KEY` from `config/master.key` at startup.

### Orphaned docker-proxy / port-in-use on restart

Containers occasionally crash (exit 255) and leave orphaned `docker-proxy` processes holding the host port, causing `up.sh` to fail with `address already in use`. The app may still appear "up" because the orphaned Puma process continues serving the old image.

Diagnosis:
```bash
docker ps -a                          # container shows Exited
ps aux | grep docker-proxy            # look for proxy on the stuck port
```

Fix:
```bash
sudo kill <proxy-pid(s)>              # free the port
bash up.sh                            # start the new container
```

The running app serving during this stuck state is from the **old image** — any code changes deployed since the last build won't be live until the container is properly restarted.

## Kubernetes (k3s)

```bash
k get pods          # 'k' is aliased to 'kubectl'
k get svc
k logs -n <namespace> -l app=<appname>
k delete pod <name>  # k3s auto-recreates
```

**Architecture pattern:** Apps run on host in Docker Compose. K3s uses ExternalService + Endpoints to route Traefik ingress traffic to host IPs (blog/delta_neutral at 192.168.4.92, hub/tbitt/stickies at 192.168.4.102).

Each service in `k3s/` has its own directory with granular YAML manifests (deployment, service, ingress, etc.).

## App Details

### Blog (Rails 8 + SQLite)
- Port: 3099 (internal) / 33099 (exposed)
- Markdown-based content in `app/posts/` and `app/reviews/` (git-ignored)
- Obsidian vault image syntax (`![[file.jpg]]`) converted automatically
- Has its own `AGENTS.md` with detailed architecture docs
- Ruby 3.4.3, Propshaft, Importmap, Turbo/Stimulus

### Hub (React + Rails API)
- Client: React 19 + TypeScript + Vite (port 3000/13000)
- Server: Rails 8 API-only (port 3001/13001)
- Hyperliquid SDK for crypto price data

### Tbitt (React + Express) - Deprecated
- Client: React 18 + TypeScript (port 3000/13000)
- Server: Express + TypeScript + PostgreSQL (port 3001/13001)
- Deprecated Aug 2025 (Jupiter API discontinued)

### Delta Neutral (Rails 8 + SQLite)
- Port: 80 (internal) / 43080 (exposed)
- Automated rebalancer for Hyperliquid short hedges on Uniswap V3 positions
- Background jobs via Solid Queue (in-process with Puma via `SOLID_QUEUE_IN_PUMA=1`)
- Env vars in `config/master.key` (credentials) + `.env.production` (API keys/SMTP)
- Required env vars: `HYPERLIQUID_PRIVATE_KEY`, `HYPERLIQUID_WALLET_ADDRESS`, `UNISWAP_SUBGRAPH_URL`, `THEGRAPH_API_KEY`
- Dockerfile requires extra build deps: `autoconf automake libtool libsecp256k1-dev libssl-dev` (for `rbsecp256k1` gem)
- Ruby 3.4.8, Thruster, Propshaft, Tailwind

## Environment

- **Shell:** zsh with vim keybindings
- **Editor:** neovim (built from source in `build/neovim/`)
- **Ruby:** managed via rbenv
- **Node:** managed via fnm
- **Tmux prefix:** Ctrl+Space
- **Git user:** carter2099 <carter2099@pm.me>
