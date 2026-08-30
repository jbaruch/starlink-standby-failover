#!/usr/bin/env python3
"""The monthly revert decides whether you pay $0 or $55. Test the gate.

Billing resets on the 6th. The revert runs on the 3rd, at most once per
calendar month, and must be a no-op when the line is already on standby (the
normal case — it only really fires in a month where an outage triggered a
switch).

    ./.venv/bin/python test_scheduling.py
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import tempfile

_d = tempfile.mkdtemp()
os.environ.update({
    "WATCHDOG_STATE": str(pathlib.Path(_d) / "state.json"),
    "WATCHDOG_KILL_SWITCH": str(pathlib.Path(_d) / "DISABLED"),
    "MAX_SPEND_USD": "200", "UNIFI_API_KEY": "x",
    "STARLINK_SESSION": "Starlink.Com.Sso=y", "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c",
    "APPROVAL_MODE": "auto", "REVERT_DAY_OF_MONTH": "3",
})

import watchdog  # noqa: E402

results: list[bool] = []


def check(label: str, got, want) -> None:
    ok = got == want
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {label}: {got!r}" + ("" if ok else f" (want {want!r})"))


class FakeDate(datetime.date):
    _today = datetime.date(2026, 9, 3)

    @classmethod
    def today(cls):
        return cls._today


def build(state: dict | None = None) -> watchdog.Watchdog:
    sf = pathlib.Path(os.environ["WATCHDOG_STATE"])
    if state is None:
        sf.unlink(missing_ok=True)
    else:
        sf.write_text(json.dumps(state))
    wd = watchdog.Watchdog.__new__(watchdog.Watchdog)
    wd.revert_day = 3
    wd._starlink_session = "Starlink.Com.Sso=y"
    wd.state = watchdog.Watchdog._load_state(wd)
    wd._save_state = lambda: None  # type: ignore[method-assign]
    return wd


def attempted(wd: watchdog.Watchdog) -> bool:
    """Did _maybe_revert get past the date/month gate and try to talk to Starlink?"""
    calls = []
    orig = watchdog.Starlink
    watchdog.Starlink = lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(  # type: ignore[assignment]
        watchdog.StarlinkError("stopped in test"))
    wd.tg = type("T", (), {"send": staticmethod(lambda *a, **k: None)})()
    try:
        wd._maybe_revert()
    finally:
        watchdog.Starlink = orig  # type: ignore[assignment]
    return bool(calls)


def main() -> int:
    real_date = watchdog.datetime.date
    watchdog.datetime.date = FakeDate  # type: ignore[misc]
    try:
        FakeDate._today = datetime.date(2026, 9, 3)
        check("fires on the 3rd", attempted(build()), True)

        FakeDate._today = datetime.date(2026, 9, 2)
        check("silent on the 2nd", attempted(build()), False)

        FakeDate._today = datetime.date(2026, 9, 6)
        check("silent on the 6th (billing day, too late)", attempted(build()), False)

        # Once per month, not once per 30-second tick.
        FakeDate._today = datetime.date(2026, 9, 3)
        wd = build()
        first = attempted(wd)
        second = attempted(wd)
        check("first run on the 3rd fires", first, True)
        check("second run same day does NOT re-fire", second, False)

        # A new month re-arms it.
        wd.state["last_revert_month"] = "2026-08"
        FakeDate._today = datetime.date(2026, 9, 3)
        check("new month re-arms", attempted(wd), True)

        # Records the month it ran, so a restart mid-month does not re-fire.
        check("records the month", wd.state["last_revert_month"], "2026-09")
    finally:
        watchdog.datetime.date = real_date  # type: ignore[misc]

    failed = results.count(False)
    print(f"\n{len(results) - failed}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
