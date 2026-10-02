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

import requests

from starlink import Starlink, StarlinkError, load_session
from unifi import UniFi, UniFiError, load_api_key

log = logging.getLogger("revert")


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-8s %(message)s",
                        stream=sys.stdout)
    dry_run = os.environ.get("REVERT_DRY_RUN", "true").lower() != "false"

    try:
        sl = Starlink(load_session())
        sl.check_session()
        sub = sl.subscription()
        if sub["isStandby"] or sub["isStandbyPending"]:
            log.info("standby %s confirmed — nothing to do",
                     "pending" if sub["isStandbyPending"] else "active")
            return 0

        line = sub["serviceLineNumber"]
        option = sl.standby_option(line)
        log.info("standby option: %s ($%s/month), effective=%s",
                 option["productId"], option["monthly"],
                 option["effectiveTimestamp"] or "immediately")
        if dry_run:
            log.warning("DRY_RUN: would put service line %s back on standby. "
                        "Set REVERT_DRY_RUN=false to act.", line)
            return 0

        u = UniFi(host=os.environ.get("UNIFI_HOST", "192.168.1.1"),
                  api_key=load_api_key(),
                  username=os.environ.get("UNIFI_USERNAME", ""),
                  password=os.environ.get("UNIFI_PASSWORD", ""),
                  site=os.environ.get("UNIFI_SITE", "default"))
        u.login()
        if not u.wan_state()["wan1_up"]:
            log.error("primary WAN is down — keeping the paid Starlink plan")
            return 1

        after = sl.back_to_standby(line)
        log.info("service line %s: standby %s confirmed (product=%s, pending=%s)",
                 line, "active" if after["isStandby"] else "pending",
                 after["productId"], after["delayedProductId"])
    except (StarlinkError, UniFiError, requests.RequestException, KeyError) as e:
        log.error("revert failed: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
