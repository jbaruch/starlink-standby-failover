#!/usr/bin/env bash
# Install the Starlink session into the NAS config without anyone hand-editing
# it, and without it ever being printed.
#
#   1. In the Starlink account tab: DevTools (Cmd+Opt+I) -> Network
#   2. Filter for  api/   then reload (Cmd+R)
#   3. Click any /api/webagg/... row -> right-click -> Copy -> "Copy as cURL"
#   4. ./install-session.sh
#
# Reads the clipboard, pulls the cookie header out, checks it actually contains
# the three auth tokens, and writes it to config.env on the NAS over ssh.

set -euo pipefail

NAS_HOST="${NAS_HOST:-nas}"
NAS_DIR="${NAS_DIR:-/home/youruser/wan-failover}"
NAS_SECRET="$NAS_DIR/secrets/starlink-session"

if ! command -v pbpaste >/dev/null; then
  echo "pbpaste not found — this script is macOS-only." >&2
  exit 1
fi

CURL_TEXT="$(pbpaste)"

if [ -z "$CURL_TEXT" ]; then
  echo "Clipboard is empty. Do the 'Copy as cURL' step first." >&2
  exit 1
fi

# Chrome emits either  -H 'cookie: ...'  or  -b '...'  depending on version.
SESSION="$(printf '%s' "$CURL_TEXT" \
  | perl -ne "print \$1 if /-H\s+'cookie:\s*([^']*)'/i || /-b\s+'([^']*)'/" \
  | head -1)"

if [ -z "$SESSION" ]; then
  echo "No cookie header found in the clipboard." >&2
  echo "Make sure you used 'Copy as cURL' on a request to starlink.com/api/..." >&2
  exit 1
fi

# Required: these two are the session. Verified present on a live logged-in
# request 2026-08-29.
missing=0
for token in Starlink.Com.Sso Starlink.Com.Access.V1; do
  case "$SESSION" in
    *"$token"*) echo "  found $token" ;;
    *)          echo "  MISSING $token" >&2; missing=1 ;;
  esac
done

# Optional: XSRF-TOKEN is NOT set on this account's session at all — it is
# absent from document.cookie and from captured request headers. The Home
# Assistant integration references it, but it is evidently not universal.
# starlink.py sends it only when present, so its absence is not fatal.
case "$SESSION" in
  *XSRF-TOKEN*) echo "  found XSRF-TOKEN (will be sent on writes)" ;;
  *)            echo "  no XSRF-TOKEN — fine, this session does not use one" ;;
esac

if [ "$missing" -ne 0 ]; then
  echo "" >&2
  echo "That request did not carry a session. Pick a request to" >&2
  echo "starlink.com/api/webagg/... while logged in, and retry." >&2
  exit 1
fi

echo "  session length: ${#SESSION} chars"

# Write to a mounted file, NOT config.env. A real cookie header contains `$`
# (Google Analytics segments), and docker compose interpolates `$` inside
# env_file values — it mangles the string and warns about undefined variables.
# The value goes over ssh on stdin so it never appears in a command line, a
# process list, or this terminal.
printf '%s' "$SESSION" | ssh "$NAS_HOST" "
  set -euo pipefail
  mkdir -p '$NAS_DIR/secrets'
  chmod 700 '$NAS_DIR/secrets'
  cat > '$NAS_SECRET'
  chmod 600 '$NAS_SECRET'
  # A stale STARLINK_SESSION in config.env would silently win in some setups.
  if [ -f '$NAS_DIR/config.env' ] && grep -q '^STARLINK_SESSION=' '$NAS_DIR/config.env'; then
    tmp=\$(mktemp)
    grep -v '^STARLINK_SESSION=' '$NAS_DIR/config.env' > \"\$tmp\"
    cat \"\$tmp\" > '$NAS_DIR/config.env'
    rm -f \"\$tmp\"
    echo 'removed stale STARLINK_SESSION from config.env'
  fi
  echo \"written to $NAS_SECRET (\$(wc -c < '$NAS_SECRET') bytes)\"
"

echo
echo "Done. Verify with:"
echo "  ssh $NAS_HOST 'cd $NAS_DIR && docker compose run --rm wan-failover python recon.py'"
