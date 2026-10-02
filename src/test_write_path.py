#!/usr/bin/env python3
"""Exercise the live plan-change round trip using the watchdog's write methods.

Starting from active standby tests an actual upgrade. Starting from pending
standby only tests canceling the queued downgrade and requesting it again;
the paid plan remains active throughout. This does not test physical failover.

After attempting the upgrade, always restore standby, including when the
upgrade request or verification fails. Never leave an unconfirmed request
without attempting cleanup.

Safe by default: prints the cost and exits unless CONFIRM_SPEND=yes.

    docker compose run --rm -e CONFIRM_SPEND=yes starlink-standby-failover python test_write_path.py
"""
from __future__ import annotations

import os
import sys

from starlink import Starlink, StarlinkError, load_session
from unifi import UniFi, load_api_key

TARGET = os.environ.get("TARGET_PRODUCT_ID",
                        "us-consumer-subscription-mini-roam-100-0526")


def show(sub: dict, label: str) -> None:
    print(f"  {label:<26} product={sub['productId']}")
    print(f"  {'':<26} isStandby={sub['isStandby']} "
          f"isStandbyPending={sub['isStandbyPending']}")
    print(f"  {'':<26} delayedProductId={sub['delayedProductId']}")


def main() -> int:
    sl = Starlink(load_session())
    sub = sl.subscription()
    line = sub["serviceLineNumber"]

    if not (sub["isStandby"] or sub["isStandbyPending"]):
        print(f"Line has no active or pending standby (product={sub['productId']}). "
              "Nothing to restore for this round trip.", file=sys.stderr)
        return 2

    print("scope  : " + ("active standby -> full plan -> standby"
                         if sub["isStandby"] else
                         "pending standby -> keep full plan -> pending standby"))
    show(sub, "before test:")
    option = sl.plan_option(line, TARGET)
    print(f"line   : {line}")
    print(f"target : {option['name']}  (monthly ${option['monthly']})")
    print(f"cost   : ${option['prorated']:.2f} live immediate charge")
    print()

    if os.environ.get("CONFIRM_SPEND", "").lower() != "yes":
        print("CONFIRM_SPEND is not 'yes' — stopping before spending anything.")
        print("Re-run with -e CONFIRM_SPEND=yes to actually do it.")
        return 0

    ceiling = float(os.environ.get("MAX_SPEND_USD", "200"))
    if option["prorated"] > ceiling:
        raise StarlinkError(f"${option['prorated']:.2f} exceeds MAX_SPEND_USD=${ceiling:.2f}")
    u = UniFi(host=os.environ.get("UNIFI_HOST", "192.168.1.1"),
              api_key=load_api_key(),
              username=os.environ.get("UNIFI_USERNAME", ""),
              password=os.environ.get("UNIFI_PASSWORD", ""),
              site=os.environ.get("UNIFI_SITE", "default"))
    u.login()
    if not u.wan_state()["wan1_up"]:
        raise StarlinkError("primary WAN is down — refusing the standby round trip")

    try:
        print(f"--- switching to {TARGET} immediately ---")
        after = sl.switch_to_product(line, TARGET)
        show(after, "after switch:")
        print("\n  FULL PLAN CONFIRMED. No standby remains queued.\n")
    finally:
        print("--- restoring the live standby option ---")
        back = sl.back_to_standby(line)
        show(back, "after restore:")

    print()
    if back["isStandby"]:
        print("ROUND TRIP PASSED. Standby is active immediately.")
    elif back["isStandbyPending"]:
        print("ROUND TRIP PASSED. Standby is queued for the next billing boundary. "
              "Its future activation has not been tested yet.")
    else:
        print("UNEXPECTED: neither standby nor standby-pending. Check "
              "starlink.com/account before the billing date.")
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except StarlinkError as e:
        print(f"\nFAILED: {type(e).__name__}: {e}", file=sys.stderr)
        raise SystemExit(1) from e
