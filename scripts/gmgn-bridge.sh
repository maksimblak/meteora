#!/usr/bin/env sh
# GMGN browser bridge (G25) on Ubuntu with a desktop: a visible Chromium window that the
# worker in Docker asks for GMGN metrics (GMGN_URL=http://host.docker.internal:8765).
#
#   sh scripts/gmgn-bridge.sh
#
# The official Playwright Chromium opens in a visible window with a persistent profile in
# your user folder (GMGN_BRIDGE_PROFILE_DIR, default
# ~/.local/share/meteora-volume-bot/gmgn-bridge-profile). Keep the window open and the PC
# awake; closing the window stops the bridge. To work in your own GMGN session, sign in once
# in that window by hand - the bridge never signs in and never reads, copies or logs
# cookies. Using GMGN this way is against its rules: the account you sign in with is at risk.
#
# Settings come from the environment, else from .env in the repository root:
# GMGN_BRIDGE_TOKEN (required, the same value the worker gets), GMGN_BRIDGE_HOST,
# GMGN_BRIDGE_PORT, GMGN_BRIDGE_PROFILE_DIR, GMGN_BRIDGE_REQUESTS_PER_MINUTE and the shared
# GMGN_* service settings; GMGN_BRIDGE_TOKEN_FILE only from the environment. No value is
# printed. Without GMGN_BRIDGE_HOST the bridge listens on 127.0.0.1 and on the docker0
# address, which containers reach as host.docker.internal (host-gateway). The first start
# downloads Chromium for Playwright; a fresh Ubuntu may also need its libraries once:
# (cd services/gmgn && sudo "$(command -v uv)" run --frozen playwright install-deps chromium).
set -eu

root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
service="$root/services/gmgn"

if [ -f "$root/.env" ]; then
  for name in GMGN_BRIDGE_TOKEN GMGN_BRIDGE_HOST GMGN_BRIDGE_PORT GMGN_BRIDGE_PROFILE_DIR \
    GMGN_BRIDGE_REQUESTS_PER_MINUTE GMGN_BASE_URL GMGN_PAGE_TIMEOUT_SECONDS \
    GMGN_CACHE_TTL_SECONDS GMGN_RESPONSE_WAIT_SECONDS GMGN_BREAKER_COOLDOWN_SECONDS; do
    eval "current=\${$name:-}"
    if [ -n "$current" ]; then
      continue
    fi
    value=$(sed -n "s/^[[:space:]]*$name[[:space:]]*=[[:space:]]*//p" "$root/.env" | tail -n 1 |
      sed -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/")
    if [ -n "$value" ]; then
      export "$name=$value"
    fi
  done
fi

if [ -z "${GMGN_BRIDGE_HOST:-}" ]; then
  docker0=$(ip -4 -o addr show docker0 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -n 1)
  if [ -n "$docker0" ]; then
    export GMGN_BRIDGE_HOST="127.0.0.1,$docker0"
  fi
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is not installed: https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 2
fi

cd "$service"
# The pinned Playwright of services/gmgn/uv.lock and its own Chromium (no-op when present).
uv run --frozen --no-dev playwright install --no-shell chromium
echo "GMGN bridge: a Chromium window opens now. Keep it open; Ctrl+C here stops the bridge."
exec uv run --frozen --no-dev python -m gmgn_service.bridge
