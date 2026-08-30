#!/usr/bin/env python3
"""Weekly proof that the backup is actually armed.

Two ways this silently dies between outages:
  1. The Starlink session rots (~15 days) and nobody notices.
  2. SpaceX ships a new SPA bundle and the endpoint paths move.

Both look identical from the outside: nothing. So we check on a schedule and
make noise, because the alternative is discovering it during the outage.

Exit codes: 0 armed, 1 not armed. Wire the non-zero to something that reaches
you — that is the entire point.
"""
from __future__ import annotations


import logging
import os
import sys

from notify import TelegramNotifier
from starlink import (SessionExpired, Starlink, StarlinkError, load_session,
                      session_age_days)
from unifi import UniFi, UniFiError, load_api_key

log = logging.getLogger("canary")


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-8s %(message)s",
                        stream=sys.stdout)
    problems: list[str] = []

    # 1. Can we still see per-WAN state?
    try:
        u = UniFi(host=os.environ.get("UNIFI_HOST", "192.168.1.1"),
                  api_key=load_api_key(),
                  username=os.environ.get("UNIFI_USERNAME", ""),
                  password=os.environ.get("UNIFI_PASSWORD", ""))
        u.login()
        state = u.wan_state()
        log.info("unifi: primary up=%s backup up=%s",
                 state["wan1_up"], state["wan2_up"])
        if not state["wan2_up"]:
            problems.append("Starlink WAN2 is down — no failover path at all")
    except (UniFiError, KeyError) as e:
        problems.append(f"unifi check failed: {e}")

    # 2. Is the Starlink session alive, and do the endpoints still exist?
    try:
        sl = Starlink(load_session())
        # check_session() goes through _call -> _ensure_token, which already
        # mints a fresh access token. Do NOT also call refresh() here: two
        # refreshes back to back and the second returns 401, which looked
        # exactly like a dead session and cried wolf on 2026-08-29.
        sl.check_session()
        sub = sl.subscription()
        log.info("starlink: line=%s standby=%s", sub["serviceLineNumber"], sub["isStandby"])
        # Exercise the cost gate itself — if change-options moves or stops
        # carrying proratedPrice, the watchdog cannot price a resume and the
        # backup is effectively disarmed.
        # Price the plan we would actually switch to, not a range. This also
        # proves change-options still carries proratedPrice, which is the only
        # thing standing between us and spending blind.
        target = os.environ.get("TARGET_PRODUCT_ID",
                                "us-consumer-subscription-mini-roam-100-0526")
        option = sl.plan_option(sub["serviceLineNumber"], target)
        log.info("starlink: %s would cost $%.2f today",
                 option["name"], option["prorated"])

        # Nothing can renew the login automatically. Say so BEFORE it dies.
        age = session_age_days()
        warn_after = float(os.environ.get("SESSION_WARN_DAYS", "12"))
        if age is not None:
            log.info("starlink: session is %.1f days old (warn at %.0f)", age, warn_after)
            if age >= warn_after:
                problems.append(
                    f"Starlink session is {age:.0f} days old and cannot be "
                    f"renewed automatically — re-capture it before it expires")
    except SessionExpired as e:
        problems.append(f"STARLINK SESSION EXPIRED — log in again: {e}")
    except (StarlinkError, KeyError) as e:
        problems.append(f"starlink endpoint check failed (SPA may have moved): {e}")

    # A canary nobody hears is not a canary. Tell Telegram when the backup is
    # not armed — that is the whole reason this runs on a schedule.
    if problems:
        for p in problems:
            log.error("NOT ARMED: %s", p)
        token, chat = os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", "")
        if token and chat:
            TelegramNotifier(token, chat).send(
                "🔴 *Starlink auto-upgrade is NOT armed.*\n" +
                "\n".join(f"• {p}" for p in problems) +
                "\n\n_Failover still works — Starlink takes over on standby at "
                "~0.5 Mbps. What would not happen is the switch to a full plan, "
                "so an outage means slow internet, not no internet._")
        else:
            log.error("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID unset — cannot alert about being unarmed")
        return 1

    log.info("ARMED: detection and resume path both healthy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
