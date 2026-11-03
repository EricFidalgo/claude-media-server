#!/bin/sh
# The whole app in a throwaway Jellyfin, driven by a phone-sized Chrome. Run: ./run.sh [keep]
#   keep   leaves it running afterwards at http://127.0.0.1:18096 (admin / e2e-password)
# Stop it later with: E2E_DIR=/tmp/x docker compose -p rd-e2e down
set -eu
here=$(cd "$(dirname "$0")" && pwd)
cd "$here"
E2E_DIR=$(mktemp -d)
export E2E_DIR
mkdir -p "$E2E_DIR/config" "$E2E_DIR/cache" "$E2E_DIR/library/movies" "$E2E_DIR/library/shows" "$E2E_DIR/state"

finish() {
    code=$?
    if [ "${1:-}" != keep ]; then
        docker compose logs --no-color jellyfin 2>&1 | grep -iE 'real-?debrid|\[ERR\]' | tail -20 >&2 || true
        docker compose down --volumes --remove-orphans >/dev/null 2>&1 || true
        rm -rf "$E2E_DIR" 2>/dev/null || true
    fi
    exit "$code"
}
trap 'finish "${KEEP:-}"' EXIT
KEEP=${1:-}

../plugin/build.sh "$E2E_DIR/config" >/dev/null
docker compose up -d >/dev/null
[ -d node_modules/playwright-core ] || npm install --silent --no-audit --no-fund >/dev/null
E2E_LIBRARY="$E2E_DIR/library" node run.mjs
