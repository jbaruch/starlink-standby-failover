#!/usr/bin/env python3
"""Read-only recon against the live Starlink account. Spends nothing.

Runs only GETs: service-lines, reactivation-estimation, demand-surcharge, plans.
Dumps the real response shapes so _build_checkout_payload() can be completed
without guessing at a payment endpoint's field names.

    STARLINK_SESSION='...' python3 recon.py

Writes recon-output.json next to this file. Review it before arming anything.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

from starlink import SessionExpired, Starlink, StarlinkError, load_session

HERE = pathlib.Path(__file__).resolve().parent
OUT = pathlib.Path(os.environ.get("RECON_OUTPUT", HERE / "recon-output.json"))


def main() -> int:
    try:
        sl = Starlink(load_session())
    except StarlinkError as e:
        print(f"{e}. See README for how to capture the session.", file=sys.stderr)
        return 2
    report: dict[str, object] = {}

    try:
        report["auth_user"] = sl.check_session()
        print("session: OK")
    except SessionExpired as e:
        print(f"session: DEAD — {e}", file=sys.stderr)
        return 1

    sub = sl.subscription()
    report["subscription"] = sub
    print(f"service line : {sub['serviceLineNumber']} ({sub['nickname']})")
    print(f"product      : {sub['productId']}")
    print(f"standby      : {sub['isStandby']} (pending={sub['isStandbyPending']})")

    line = sub["serviceLineNumber"]
    addr = sub["addressReferenceId"]

    # Each of these is a GET. If one 404s, the SPA moved and the endpoint map
    # in FINDINGS.md §9d needs regenerating — that is a real finding, not noise,
    # so let it surface rather than swallowing it.
    for label, fn in (
        ("change_options", lambda: sl.change_options(line)),
        ("resume_cost_range", lambda: sl.resume_cost_range(line)),
        ("address_has_capacity", lambda: sl.address_has_capacity(addr) if addr else "(no address id)"),
        ("demand_surcharge", lambda: sl.demand_surcharge(line)),
    ):
        try:
            report[label] = fn()
            print(f"{label:24s}: OK")
        except StarlinkError as e:
            report[label] = {"__error": str(e)}
            print(f"{label:24s}: FAILED — {e}", file=sys.stderr)

    OUT.write_text(json.dumps(report, indent=2, default=str))
    print(f"\nwrote {OUT}")
    print("Read path verified. Next: run with DRY_RUN=true and watch it decide.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
