#!/bin/sh
# docker/webui-coolify-entrypoint.sh
#
# Wrapper around the stock Hermes WebUI entrypoint (/hermeswebui_init.bash).
# On a FRESH persistent volume it seeds config.yaml with `claude-code` as the
# default provider, so the provider works out of the box and shows in the model
# picker. (In the multi-container setup the gateway creates config.yaml; in this
# single-WebUI image nothing else does, so we create it here.)
#
# Only writes when config.yaml is ABSENT — never clobbers an existing user
# config, so redeploys (which keep the volume) leave your settings untouched.
set -eu

HOME_DIR="${HERMES_HOME:-/home/hermeswebui/.hermes}"
CFG="$HOME_DIR/config.yaml"

if [ ! -f "$CFG" ]; then
    mkdir -p "$HOME_DIR"
    cat > "$CFG" <<'YAML'
model:
  provider: claude-code
  default: sonnet
providers:
  claude-code:
    models:
    - sonnet
    - opus
    - haiku
fallback_providers: []
YAML
    # The stock entrypoint chowns ~/.hermes to WANTED_UID anyway; do it here too
    # so the file is correct even before that step runs.
    chown 1000:1000 "$CFG" 2>/dev/null || true
    rm -f "$HOME_DIR/webui/models_cache.json" 2>/dev/null || true
    echo "[coolify-entrypoint] seeded fresh config.yaml with claude-code as default provider"
fi

exec /hermeswebui_init.bash
