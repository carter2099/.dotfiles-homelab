FROM ghcr.io/getpaseo/paseo:latest

USER root

# Install system deps (unzip for bun install, build-essential + python3 for agents)
RUN apt-get update && apt-get install -y --no-install-recommends \
    unzip \
    build-essential \
    python3 \
    && rm -rf /var/lib/apt/lists/*

# Install bun (needed by omp CLI)
RUN curl -fsSL https://bun.sh/install | bash && \
    cp /home/paseo/.bun/bin/bun /usr/local/bin/bun && \
    chmod +x /usr/local/bin/bun

# Install omp (OpenCode CLI) for agent orchestration
RUN npm install -g @oh-my-pi/pi-coding-agent@17.0.6

# Ensure omp agent directory is writable by paseo user at runtime.
# The base entrypoint runs as root initially, so we hook in before it drops privileges.
COPY --chown=root:root entrypoint-fix.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/entrypoint-fix.sh
ENTRYPOINT ["/usr/local/bin/entrypoint-fix.sh"]

# Patch: respect X-Forwarded-Proto for TLS detection behind reverse proxy (Cloudflare tunnel)
RUN sed -i 's/req\.protocol === "https"/req.protocol === "https" || req.headers["x-forwarded-proto"] === "https"/' \
    /usr/local/lib/node_modules/@getpaseo/server/dist/server/server/web-ui.js

# Leave as root - base entrypoint drops to paseo user after setup
