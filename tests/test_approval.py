#!/usr/bin/env python3
"""Functional test of the approval path. Starts a real server on a spare port.

The approval link is the human gate on a non-refundable charge, so "it probably
works" is not good enough: single-use, expiry, and deny all get exercised.

    ./.venv/bin/python test_approval.py
"""
from __future__ import annotations

import time
import urllib.error
import urllib.request

from notify import ApprovalServer

PORT = 8799
results: list[bool] = []


def check(label: str, got, want) -> None:
    ok = got == want
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {label}: {got!r}" + ("" if ok else f" (want {want!r})"))


def hit(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def main() -> int:
    srv = ApprovalServer(port=PORT, base_url=f"http://127.0.0.1:{PORT}", ttl_seconds=2)
    srv.start()

    # healthz
    check("healthz responds", hit(f"http://127.0.0.1:{PORT}/healthz"), 200)

    # --- approve path ---
    rid, approve, deny = srv.create({"cost": 64.5})
    check("starts pending", srv.poll(rid), "pending")
    check("approve link 200", hit(approve), 200)
    check("becomes approved", srv.poll(rid), "approved")
    check("link is single-use", hit(approve), 410)
    check("stays approved after reuse", srv.poll(rid), "approved")

    # --- deny path ---
    rid2, approve2, deny2 = srv.create({"cost": 121.0})
    check("deny link 200", hit(deny2), 200)
    check("becomes denied", srv.poll(rid2), "denied")
    check("approving after deny is refused", hit(approve2), 410)

    # --- expiry (ttl=2s) ---
    rid3, approve3, _ = srv.create({"cost": 9.99})
    time.sleep(2.5)
    check("expires on its own", srv.poll(rid3), "expired")
    check("expired link refused", hit(approve3), 410)

    # --- unknown id ---
    check("unknown id is expired, not approved", srv.poll("nope"), "expired")
    check("unknown link 410", hit(f"http://127.0.0.1:{PORT}/approve/bogus"), 410)

    # An approval must never be guessable — it authorises a real charge.
    rid4, _, _ = srv.create({})
    check("token is long enough to be unguessable", len(rid4) >= 32, True)

    srv.stop()
    failed = results.count(False)
    print(f"\n{len(results) - failed}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
