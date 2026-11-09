#!/usr/bin/env bash
# Build dist/dmx_inventory-<version>.tgz for upload to Splunk Cloud.
set -euo pipefail
cd "$(dirname "$0")/.."

version=$(awk -F' *= *' '/^\[id\]/{s=1} s && $1=="version"{print $2; exit}' dmx_inventory/default/app.conf)
out="dist/dmx_inventory-${version}.tgz"
mkdir -p dist

# Splunk Cloud vetting rejects local/ content, bytecode and hidden files.
COPYFILE_DISABLE=1 tar -czf "$out" \
  --exclude='__pycache__' --exclude='*.pyc' --exclude='.*' \
  --exclude='dmx_inventory/local' --exclude='dmx_inventory/metadata/local.meta' \
  --owner=0 --group=0 --mode='u+rwX,go+rX,go-w' \
  dmx_inventory
echo "$out"
