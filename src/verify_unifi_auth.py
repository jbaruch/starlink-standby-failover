#!/usr/bin/env python3
"""Does UniFi auth actually work, and which mode? Read-only.

The open question this settles: the classic endpoints (`stat/device`,
`system-log/all`) are the ONLY ones carrying per-WAN state, and whether they
accept `X-API-KEY` is firmware-dependent. UniFi OS returns an identical bare
401 for a bogus key and for no key, so it cannot be probed without a real one.

    docker compose run --rm wan-failover python verify_unifi_auth.py

Exit 0 if some mode can read wan1.up, 1 if none can.
"""
from __future__ import annotations

import os
import sys

from unifi import UniFi, UniFiError, load_api_key


def try_mode(label: str, api_key: str = "", username: str = "",
             password: str = "") -> bool:
    try:
        u = UniFi(host=os.environ.get("UNIFI_HOST", "192.168.1.1"),
                  api_key=api_key, username=username, password=password)
    except UniFiError as e:
        print(f"{label:<22}: skipped — {e}")
        return False

    try:
        u.login()
        state = u.wan_state()
        print(f"{label:<22}: OK  wan1(Fiber)={state['wan1_up']} "
              f"wan2(Starlink)={state['wan2_up']}  wan1_ip={state['wan1_ip']}")
    except (UniFiError, Exception) as e:  # noqa: BLE001 - report anything, hide nothing
        print(f"{label:<22}: FAILED reading wan1.up — {type(e).__name__}: {e}")
        return False

    # The POST path has its own auth story (CSRF vs API key), so prove it too —
    # it is how the watchdog learns WHICH wan dropped.
    #
    # Use a YEAR, not a week. An empty 7-day window is ambiguous: "working,
    # quiet week" and "authenticated but silently returning nothing" look
    # identical, and the difference is whether WAN attribution works at all.
    # A year is long enough that zero rows means something is wrong.
    try:
        events = u.recent_wan_events(since_seconds=365 * 24 * 3600)
        wans = sorted({e["wan"] for e in events if e["wan"]})
        print(f"{'':<22}  system-log OK: {len(events)} events in 365d, "
              f"WANs seen: {wans or 'NONE'}")
        if not events:
            print(f"{'':<22}  SUSPICIOUS: zero events in a year. Either the log "
                  f"rotated or the POST is silently returning nothing.")
            return False
        if not wans:
            print(f"{'':<22}  FAILED: events returned but no WAN_NAME on any of "
                  f"them — attribution is broken, cannot tell Fiber from Starlink.")
            return False
    except Exception as e:  # noqa: BLE001
        print(f"{'':<22}  system-log FAILED — {type(e).__name__}: {e}")
        print(f"{'':<22}  (detection would work; WAN attribution would not)")
        return False

    return True


def main() -> int:
    api_key = load_api_key()
    user = os.environ.get("UNIFI_USERNAME", "")
    pw = os.environ.get("UNIFI_PASSWORD", "")

    print(f"api key present: {'yes' if api_key else 'no'}"
          f"   local admin present: {'yes' if user and pw else 'no'}\n")

    results = {}
    if api_key:
        results["api key"] = try_mode("X-API-KEY", api_key=api_key)
    if user and pw:
        results["local admin"] = try_mode("local admin", username=user, password=pw)

    if not results:
        print("Nothing to test. Set UNIFI_API_KEY_FILE or UNIFI_USERNAME/PASSWORD.",
              file=sys.stderr)
        return 1

    print()
    working = [k for k, v in results.items() if v]
    if working:
        print(f"USABLE: {', '.join(working)}")
        if "api key" in working:
            print("Use the API key — no password stored anywhere, revocable in the UI.")
        else:
            print("The API key does not work on the classic endpoints on this "
                  "firmware. The local admin is the way.")
        return 0

    print("NONE of the configured credentials can read per-WAN state.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
