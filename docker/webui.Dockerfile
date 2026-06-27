# docker/webui.Dockerfile
#
# OPTIONAL — only needed if the base `ghcr.io/nesquena/hermes-webui` image
# does NOT ship `node` on PATH. The WebUI runs the Hermes agent in-process, so
# the `claude-code` provider spawns `claude` inside THIS container. The npm
# build of claude is a `#!/usr/bin/env node` launcher and needs node; this
# image instead installs the self-contained NATIVE claude binary (no node
# dependency) at a fixed global path.
#
# Enable it via the commented `build:` block in docker-compose.local.yml and
# set the WebUI's HERMES_CLAUDE_CLI=/usr/local/bin/claude.
#
ARG HERMES_WEBUI_TAG=0.51.92
FROM ghcr.io/nesquena/hermes-webui:${HERMES_WEBUI_TAG}

# Root for the install; the base image's entrypoint re-drops to its runtime
# user via WANTED_UID/WANTED_GID at startup, so we do not restore USER here.
USER root

# Self-contained native claude binary — no node required at runtime.
RUN set -eu; \
    if ! command -v curl >/dev/null 2>&1; then \
        (apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*) \
        || (apk add --no-cache curl ca-certificates); \
    fi; \
    curl -fsSL https://claude.ai/install.sh | bash; \
    install_bin="$(command -v claude || echo "$HOME/.local/bin/claude")"; \
    ln -sf "$install_bin" /usr/local/bin/claude; \
    /usr/local/bin/claude --version
