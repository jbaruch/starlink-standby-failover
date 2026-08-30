#!/usr/bin/env python3
"""Put the line back on Standby — on the calendar, not on Fiber recovering.

Resuming bills the prorated remainder of the current cycle immediately, and
going back to Standby refunds nothing. So dropping back the moment Fiber
returns just donates the rest of the month to SpaceX. Run this a day or two
before the billing date instead.

    STARLINK_SESSION='...' REVERT_DRY_RUN=false python3 revert_to_standby.py
"""
from __future__ import annotations

import logging
import os
import sys

from starlink import SessionExpired, Starlink, StarlinkError, load_session

log = logging.getLogger("revert")


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-8s %(message)s",
                        stream=sys.stdout)
    dry_run = os.environ.get("REVERT_DRY_RUN", "true").lower() != "false"

    try:
        sl = Starlink(load_session())
    except StarlinkError as e:
        log.error("%s", e)
        return 2
    try:
        sl.check_session()
    except SessionExpired as e:
        log.error("session dead, cannot revert: %s", e)
        return 1

    sub = sl.subscription()
    if sub["isStandby"] or sub["isStandbyPending"]:
        log.info("already on standby (pending=%s) — nothing to do",
                 sub["isStandbyPending"])
        return 0

    if dry_run:
        log.warning("DRY_RUN: would put service line %s back on standby. "
                    "Set REVERT_DRY_RUN=false to act.", sub["serviceLineNumber"])
        return 0

    try:
        result = sl.back_to_standby(sub["serviceLineNumber"])
    except StarlinkError as e:
        log.error("revert failed: %s", e)
        return 1

    log.info("service line %s returned to standby: %s",
             sub["serviceLineNumber"], result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
