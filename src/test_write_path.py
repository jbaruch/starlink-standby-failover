#!/usr/bin/env python3
"""Deliberately exercise the write path, once, for a few dollars.

Every endpoint this system depends on has been verified EXCEPT the two that
spend money. `product/update` has never been called, so the CSRF question is
open and the first real outage would be a terrible time to find out.

This also answers the question the API will not: when you go BACK to standby,
does it apply immediately or queue for the next billing boundary? The
subscription model has an `isStandbyPending` field and standby last began
exactly on a boundary (2026-08-06T00:00:00Z), which hints at "queued". If it
queues, the revert must be requested with days of slack before the cycle rolls,
or a $6 test becomes a $55 month.

Safe by default: prints the cost and exits unless CONFIRM_SPEND=yes.

    docker compose run --rm -e CONFIRM_SPEND=yes wan-failover python test_write_path.py
"""
from __future__ import annotations

import os
import sys
import time

from starlink import STANDBY_PRODUCT_ID, Starlink, StarlinkError, load_session

TARGET = os.environ.get("TARGET_PRODUCT_ID",
                        "us-consumer-subscription-mini-roam-100-0526")


def show(sl: Starlink, line: str, label: str) -> dict:
    sub = sl.subscription()
    print(f"  {label:<26} product={sub['productId']}")
    print(f"  {'':<26} isStandby={sub['isStandby']} "
          f"isStandbyPending={sub['isStandbyPending']}")
    return sub


def main() -> int:
    sl = Starlink(load_session())
    sub = sl.subscription()
    line = sub["serviceLineNumber"]

    if not sub["isStandby"]:
        print(f"Line is not on standby (product={sub['productId']}). "
              "Nothing to test — revert it first.", file=sys.stderr)
        return 2

    option = sl.plan_option(line, TARGET)
    print(f"line   : {line}")
    print(f"target : {option['name']}  (monthly ${option['monthly']})")
    print(f"cost   : ${option['prorated']:.2f} prorated, non-refundable")
    print()

    if os.environ.get("CONFIRM_SPEND", "").lower() != "yes":
        print("CONFIRM_SPEND is not 'yes' — stopping before spending anything.")
        print("Re-run with -e CONFIRM_SPEND=yes to actually do it.")
        return 0

    print(f"--- switching to {TARGET} ---")
    result = sl.change_product(line, TARGET)
    print(f"  change_product returned: {str(result)[:200]}")
    time.sleep(5)
    after = show(sl, line, "after switch:")

    if after["isStandby"]:
        print("\nFAILED: still on standby after the call. The write path does "
              "not work — this is exactly what the test was for.", file=sys.stderr)
        return 1
    print("\n  WRITE PATH WORKS. Plan change applied.\n")

    print(f"--- requesting standby again ({STANDBY_PRODUCT_ID}) ---")
    result = sl.change_product(line, STANDBY_PRODUCT_ID)
    print(f"  change_product returned: {str(result)[:200]}")
    time.sleep(5)
    back = show(sl, line, "after revert:")

    print()
    if back["isStandby"]:
        print("REVERT IS IMMEDIATE. Standby applied straight away — the revert "
              "job can run any time before the billing date.")
    elif back["isStandbyPending"]:
        print("REVERT IS QUEUED (isStandbyPending=True). It applies at the next "
              "billing boundary, so the revert must be requested with slack "
              "before the cycle rolls, never on the last day.")
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
