#!/usr/bin/env bash
# Phase 0: check the DMX endpoints on a real stack and save the responses.
#
#   export SPLUNK_STACK=mystack            # <stack>.splunkcloud.com
#   export SPLUNK_TOKEN=$(cat token.txt)   # sc_admin (or test-role) token
#   scripts/phase0_probe.sh
#
# Your IP must be on the stack's ACS allow list for search-api (port 8089).
# Responses go to tests/fixtures/live/ (git-ignored). They may contain
# sensitive configuration: review and scrub before committing any of them.
set -euo pipefail
: "${SPLUNK_STACK:?set SPLUNK_STACK}" "${SPLUNK_TOKEN:?set SPLUNK_TOKEN}"
base="${DMX_BASE_PATH:-/services/v2/dmx}"
out="$(dirname "$0")/../tests/fixtures/live"
mkdir -p "$out"

for ep in pipelines destinations datasets; do
  url="https://${SPLUNK_STACK}.splunkcloud.com:8089${base}/${ep}?output_mode=json&count=5"
  code=$(curl -sS -o "$out/${ep}.json" -w '%{http_code}' \
    -H "Authorization: Bearer ${SPLUNK_TOKEN}" "$url" || echo "curl-failed")
  printf '%-13s HTTP %s  -> %s\n' "$ep" "$code" "$out/${ep}.json"
  if command -v jq >/dev/null && [ "$code" = 200 ]; then
    jq -c 'if type=="array" then {array_len: length, first_keys: (.[0]|keys?)}
           else {top_keys: keys} end' "$out/${ep}.json" || true
  fi
done
