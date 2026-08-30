#!/usr/bin/env bash
# Install a UniFi Network API key onto the NAS without it passing through a
# terminal, a command line, or a shell history.
#
#   1. UniFi console -> Settings -> Control Plane -> Integrations
#   2. Use the existing key if you still have its value, or "Create New API Key"
#      (the value is shown exactly once)
#   3. Copy it, then run:  ./install-unifi-key.sh
#
# Then check whether it actually works on the classic endpoints:
#   ssh nas 'cd /home/youruser/wan-failover && \
#     docker compose run --rm wan-failover python verify_unifi_auth.py'

set -euo pipefail

NAS_HOST="${NAS_HOST:-nas}"
NAS_DIR="${NAS_DIR:-/home/youruser/wan-failover}"
NAS_SECRET="$NAS_DIR/secrets/unifi-api-key"

if ! command -v pbpaste > /dev/null; then
  echo "pbpaste not found — this script is macOS-only." >&2
  exit 1
fi

KEY="$(pbpaste | tr -d '[:space:]')"

if [ -z "$KEY" ]; then
  echo "Clipboard is empty. Copy the API key first." >&2
  exit 1
fi

# UniFi keys are long opaque strings. A short paste is almost certainly the
# masked display value (e.g. "*_0jU") rather than the real key.
if [ "${#KEY}" -lt 20 ]; then
  echo "That is only ${#KEY} characters — looks like the masked value from the" >&2
  echo "table, not the real key. The full key is shown only once, at creation." >&2
  exit 1
fi

echo "  key length: ${#KEY} chars"

printf '%s' "$KEY" | ssh "$NAS_HOST" "
  set -euo pipefail
  mkdir -p '$NAS_DIR/secrets'
  chmod 700 '$NAS_DIR/secrets'
  cat > '$NAS_SECRET'
  chmod 600 '$NAS_SECRET'
  echo \"written to $NAS_SECRET (\$(wc -c < '$NAS_SECRET') bytes)\"
"

echo
echo "Now prove it works on the classic endpoints:"
echo "  ssh $NAS_HOST 'cd $NAS_DIR && docker compose run --rm wan-failover python verify_unifi_auth.py'"
