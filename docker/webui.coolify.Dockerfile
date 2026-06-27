# docker/webui.coolify.Dockerfile
#
# Self-contained Hermes WebUI image for Coolify (or any single-image deploy).
# Bakes THIS repo's agent source (including the `claude-code` subscription
# provider) and the self-contained native `claude` CLI into the community
# WebUI image, so every build ships fresh code with NO shared source volume —
# which means "redeploy on push" actually deploys new code. See
# DEPLOY-COOLIFY.md.
#
# The heavy `uv pip install hermes-agent[all]` runs at first container BOOT
# (the stock entrypoint stages /opt/hermes -> /tmp and installs it), not at
# build time, so this image builds quickly; the first boot is the slow one.
#
ARG HERMES_WEBUI_TAG=0.51.92
FROM ghcr.io/nesquena/hermes-webui:${HERMES_WEBUI_TAG}

USER root

# 1) Self-contained native `claude` CLI (no node dependency).
#    IMPORTANT: install with HOME=/opt/claude, NOT the default /root. The
#    installer drops the binary under $HOME/.local/share/claude and symlinks
#    $HOME/.local/bin/claude to it. /root is mode 0700, so the WebUI's runtime
#    user (uid 1000, after the WANTED_UID remap) cannot traverse it and `which`
#    finds nothing → "claude CLI not installed". Installing under /opt/claude
#    (world-traversable 0755) makes the binary reachable by uid 1000.
RUN set -eu; \
    if ! command -v curl >/dev/null 2>&1; then \
        (apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*) \
        || (apk add --no-cache curl ca-certificates); \
    fi; \
    export HOME=/opt/claude; \
    mkdir -p /opt/claude; \
    curl -fsSL https://claude.ai/install.sh | bash; \
    chmod -R a+rX /opt/claude/.local; \
    ln -sf /opt/claude/.local/bin/claude /usr/local/bin/claude; \
    /usr/local/bin/claude --version

# 1b) GitHub CLI + token-based git auth so the agent can clone PRIVATE repos.
#     `gh` and git both read the GH_TOKEN (or GITHUB_TOKEN) env var you set in
#     Coolify — nothing is stored in the container fs, so it survives redeploys.
#     git uses gh as its credential helper for github.com (system-wide, so it
#     applies to the uid-1000 runtime user without a per-user ~/.gitconfig).
RUN set -eu; \
    apt-get update; \
    apt-get install -y --no-install-recommends gnupg; \
    mkdir -p -m 0755 /etc/apt/keyrings; \
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /etc/apt/keyrings/githubcli-archive-keyring.gpg; \
    chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg; \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends gh; \
    rm -rf /var/lib/apt/lists/*; \
    git config --system credential."https://github.com".helper '!gh auth git-credential'; \
    git config --system credential."https://gist.github.com".helper '!gh auth git-credential'; \
    git config --system safe.directory '*'; \
    gh --version

# 2) Bake this fork's agent source at /opt/hermes — the WebUI entrypoint's
#    SECOND search path, deliberately NOT under the persistent ~/.hermes volume
#    so redeploys pick up fresh code. Owned by uid 1000 (the WebUI's runtime
#    user after its WANTED_UID remap) so the entrypoint's `rsync -a` staging
#    copy into /tmp is deletable — avoids the read-only-dir crash loop that the
#    root-owned local image hits.
COPY --chown=1000:1000 . /opt/hermes

# 3) Config-seed wrapper: ensures config.yaml lists the claude-code provider so
#    it appears in the model picker even on a fresh persistent volume, then
#    hands off to the stock WebUI entrypoint. Idempotent.
COPY --chmod=0755 docker/webui-coolify-entrypoint.sh /usr/local/bin/webui-coolify-entrypoint.sh

ENV HERMES_CLAUDE_CLI=/usr/local/bin/claude

# `hermes` lives in the WebUI's venv (/app/venv/bin), which isn't on PATH in an
# interactive container shell. Add a shim on PATH so `hermes ...` works in the
# Coolify terminal (the venv is created at first boot, so this delegates to it).
RUN printf '#!/bin/sh\nexec /app/venv/bin/hermes "$@"\n' > /usr/local/bin/hermes \
    && chmod 0755 /usr/local/bin/hermes

CMD ["/usr/local/bin/webui-coolify-entrypoint.sh"]
