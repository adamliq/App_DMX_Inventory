#!/usr/bin/env bash
# Build releases/dmx_inventory-<version>.spl (see tools/package.py). Extra args are passed through.
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 tools/package.py "$@"
