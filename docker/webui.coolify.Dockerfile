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

# 1) Self-contained native `claude` CLI (no node dependency) at a fixed path.
RUN set -eu; \
    if ! command -v curl >/dev/null 2>&1; then \
        (apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*) \
        || (apk add --no-cache curl ca-certificates); \
    fi; \
    curl -fsSL https://claude.ai/install.sh | bash; \
    install_bin="$(command -v claude || echo "$HOME/.local/bin/claude")"; \
    ln -sf "$install_bin" /usr/local/bin/claude; \
    /usr/local/bin/claude --version

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

CMD ["/usr/local/bin/webui-coolify-entrypoint.sh"]
