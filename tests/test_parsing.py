#!/usr/bin/env python3
"""Lock the API shapes observed on 2026-08-29 against JB Dream Machine.

These are real captures, trimmed. If UniFi or Starlink change a field name,
these fail — which is the point. A watchdog that silently stops understanding
its inputs is worse than no watchdog.

    ./.venv/bin/python test_parsing.py
"""
from __future__ import annotations

import unifi

# --- real capture: /proxy/network/api/s/default/stat/device -----------------
GATEWAY = {
    "data": [
        {"model": "USW", "name": "Main Switch"},
        {
            "model": "UDMPRO",
            "name": "JB Dream Machine",
            "version": "5.1.31.34074",
            "wan1": {"ip": "203.0.113.10", "netmask": "255.255.255.0", "up": True,
                     "enable": True, "ifname": "eth8", "type": "ethernet"},
            "wan2": {"ip": "100.64.0.10", "netmask": "255.192.0.0", "up": True,
                     "enable": True, "ifname": "eth7", "type": "ethernet"},
        },
    ]
}

# --- real capture: /proxy/network/api/s/default/stat/health -----------------
HEALTH = {
    "data": [
        {"subsystem": "wlan", "status": "ok"},
        {"subsystem": "wan", "status": "ok", "wan_ip": "203.0.113.10",
         "isp_name": "Example Fiber Co", "asn": 40237,
         "uptime_stats": {"WAN": {"availability": 100, "latency_average": 7},
                          "WAN2": {"availability": 100, "latency_average": 47}}},
    ]
}

# --- real capture: system-log/all, INTERNET_OUTAGE_AND_FAILOVER -------------
SYSLOG = {
    "data": [
        {
            "event": "NETWORK_FAILED_OVER_TO_BACKUP_WAN_TEMPORARY",
            "key": "NETWORK_FAILED_OVER_TO_BACKUP_WAN_TEMPORARY_2",
            "timestamp": 1786811433981,
            "parameters": {
                "WAN_ID": {"id": "WAN"}, "WAN_NAME": {"name": "Fiber"},
                "ISP_NAME": {"name": "Example Fiber Co"},
                "ISP_ASN": {"name": "40237"}, "PORT": {"id": "9"},
            },
        },
        {
            "event": "NETWORK_WAN_FAILED",
            "key": "NETWORK_WAN_FAILED_2",
            "timestamp": 1787389004607,
            "parameters": {
                "WAN_ID": {"id": "WAN2"}, "WAN_NAME": {"name": "Starlink"},
                "ISP_NAME": {"name": "Starlink"}, "ISP_ASN": {"name": "14593"},
                "PORT": {"id": "8"}, "DURATION": {"name": "1m 6s"},
            },
        },
    ]
}


# --- real capture: subscriptions/change-options/SL-0000000-00000-00 --------
# Trimmed to the fields the cost gate reads. Prices are the live 2026-08-29
# proration, i.e. late in the billing cycle.
CHANGE_OPTIONS = {
    "currentProduct": {"productId": "us-consumer-subscription-standby-mode-0526",
                       "name": "Standby Mode", "price": 10, "proratedPrice": 0,
                       "isStandby": True},
    "pendingProduct": None,
    "changeOptions": [
        {"productResponse": {"productId": "us-consumer-subscription-mini-roam-100-0526",
                             "name": "Roam - 100GB | (Mini)", "price": 55,
                             "proratedPrice": 10.22, "isStandby": False}},
        {"productResponse": {"productId": "us-consumer-subscription-mini-roam-300",
                             "name": "Roam - 300GB | (Mini)", "price": 80,
                             "proratedPrice": 15.9, "isStandby": False}},
        {"productResponse": {"productId": "us-consumer-subscription-roam-mini-increased-0526",
                             "name": "Roam - Unlimited | (Mini)", "price": 175,
                             "proratedPrice": 37.48, "isStandby": False}},
        {"productResponse": {"productId": "us-consumer-subscription-standby-mode-0526",
                             "name": "Standby Mode", "price": 10,
                             "proratedPrice": 0, "isStandby": True}},
    ],
}


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.headers = {"x-csrf-token": "test-token"}
        self.content = b"{}"

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


def build_client() -> unifi.UniFi:
    u = unifi.UniFi(host="192.168.1.1", username="x", password="y")
    u._get = lambda path: GATEWAY if "stat/device" in path else HEALTH  # type: ignore[method-assign]
    u.s.post = lambda *a, **k: FakeResponse(SYSLOG)  # type: ignore[method-assign]
    u.s.get = lambda *a, **k: FakeResponse({})  # type: ignore[method-assign]
    return u


def check(label: str, got, want) -> bool:
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: {got!r}" + ("" if ok else f" (want {want!r})"))
    return ok


def main() -> int:
    u = build_client()
    results = []

    state = u.wan_state()
    results.append(check("wan1 (Fiber) up", state["wan1_up"], True))
    results.append(check("wan2 (Starlink) up", state["wan2_up"], True))
    results.append(check("wan1 ip", state["wan1_ip"], "203.0.113.10"))
    results.append(check("wan2 ip is Starlink CGNAT",
                         state["wan2_ip"].startswith("100."), True))

    isp = u.isp()
    results.append(check("upstream isp", isp["isp"], "Example Fiber Co"))
    results.append(check("upstream asn", isp["asn"], 40237))

    events = u.recent_wan_events()
    results.append(check("event count", len(events), 2))
    results.append(check("Fiber event attributed correctly", events[0]["wan"], "Fiber"))
    results.append(check("Starlink event attributed correctly",
                         events[1]["wan"], "Starlink"))
    results.append(check("duration parsed", events[1]["duration"], "1m 6s"))

    # The distinction the whole design rests on: an alarm cannot tell these
    # apart, this query can.
    united = [e for e in events if e["wan"] == "Fiber"]
    results.append(check("can isolate Fiber events from Starlink noise",
                         len(united), 1))

    # --- cost guardrail, against the real change-options capture -----------
    import starlink

    sl = starlink.Starlink.__new__(starlink.Starlink)  # no session needed
    sl.change_options = lambda line: CHANGE_OPTIONS  # type: ignore[method-assign]

    rng = sl.resume_cost_range("SL-0000000-00000-00")
    results.append(check("cheapest resume option", rng["min"]["prorated"], 10.22))
    results.append(check("worst-case resume option (what we gate on)",
                         rng["max"]["prorated"], 37.48))
    results.append(check("cheapest is Roam 100GB", rng["min"]["productId"],
                         "us-consumer-subscription-mini-roam-100-0526"))
    results.append(check("standby itself excluded from priced options",
                         all(o["productId"] != CHANGE_OPTIONS["currentProduct"]["productId"]
                             for o in rng["all"]), True))
    results.append(check("all non-standby options priced", len(rng["all"]), 3))

    # --- exact target plan (what the watchdog actually gates on) -----------
    ROAM100 = "us-consumer-subscription-mini-roam-100-0526"
    opt = sl.plan_option("SL-0000000-00000-00", ROAM100)
    results.append(check("target plan exact prorated cost", opt["prorated"], 10.22))
    results.append(check("target plan monthly", opt["monthly"], 55))
    results.append(check("target plan name", opt["name"], "Roam - 100GB | (Mini)"))

    # Asking for a plan this line is not offered must raise, never fall back to
    # some other plan and charge for it.
    try:
        sl.plan_option("SL-0000000-00000-00", "us-consumer-subscription-nonsense")
        results.append(check("unknown target raises", "no raise", "StarlinkError"))
    except starlink.StarlinkError:
        results.append(check("unknown target plan raises, no silent fallback", True, True))

    # No priced options must raise, not silently return a cost of zero — a zero
    # would sail straight through the ceiling and spend money.
    sl.change_options = lambda line: {"changeOptions": []}  # type: ignore[method-assign]
    try:
        sl.resume_cost_range("x")
        results.append(check("empty options raises", "no raise", "StarlinkError"))
    except starlink.StarlinkError:
        results.append(check("empty options raises rather than costing $0", True, True))

    failed = results.count(False)
    print(f"\n{len(results) - failed}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
