#!/usr/bin/env python3
"""A console that is down is a skipped tick, never an outage and never an exit.

Between 2026-10-02 and 2026-11-09 the watchdog exited 43 times because the
UniFi Network app was restarting, and leaned on Docker to bring it back. The
failure modes below are the ones in those logs: 502, 503, connect timeouts,
connection errors, and a 200 carrying the console's HTML instead of JSON.

    PYTHONPATH=src .venv/bin/python tests/test_unifi_outage.py
"""
from __future__ import annotations

import json
import unittest
from typing import Callable
from unittest.mock import Mock, patch

import requests

import watchdog
from notify import TelegramNotifier
from unifi import UniFi, UniFiError, UniFiUnavailable

BOOTING_HTML = ("<!DOCTYPE html><html><head><title>UniFi OS</title></head>"
                "<body><div id=\"root\"></div></body></html>")
NETWORKCONF = {"data": [
    {"purpose": "wan", "wan_networkgroup": "WAN", "name": "Fiber"},
    {"purpose": "wan", "wan_networkgroup": "WAN2", "name": "Starlink"},
    {"purpose": "corporate", "name": "LAN"},
]}


def response(status: int, body: str, ctype: str = "application/json") -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r._content = body.encode()
    r.encoding = "utf-8"
    r.headers["Content-Type"] = ctype
    return r


def gateway(wan1_up: bool) -> dict:
    return {"data": [
        {"model": "USW", "name": "Main Switch"},
        {"model": "UDMSE", "wan1": {"up": wan1_up, "ip": "203.0.113.10"},
         "wan2": {"up": True, "ip": "100.64.0.10"}},
    ]}


# Each one, verbatim in kind, from the container logs.
FAILURES: dict[str, Callable[[], requests.Response | Exception]] = {
    "502 Bad Gateway": lambda: response(502, "<html>502 Bad Gateway</html>", "text/html"),
    "503 Service Unavailable": lambda: response(503, "", "text/html"),
    "connect timeout": lambda: requests.ConnectTimeout(
        "Connection to 192.168.1.1 timed out. (connect timeout=15)"),
    "connection refused": lambda: requests.ConnectionError("Connection refused"),
    "read timeout": lambda: requests.ReadTimeout("Read timed out. (read timeout=15)"),
    "200 with HTML (app booting)": lambda: response(200, BOOTING_HTML, "text/html"),
}


class FakeConsole:
    """Stands in for requests.Session.request against a UDM."""

    def __init__(self) -> None:
        self.wan1_up = True
        self.failure: Callable[[], requests.Response | Exception] | None = None
        self.broken: dict[str, Callable[[], requests.Response | Exception]] = {}

    def __call__(self, method: str, url: str, **kwargs) -> requests.Response:
        make = self.failure or next(
            (f for suffix, f in self.broken.items() if url.endswith(suffix)), None)
        if make is not None:
            out = make()
            if isinstance(out, Exception):
                raise out
            return out
        if url.endswith("/stat/device"):
            return response(200, json.dumps(gateway(self.wan1_up)))
        if url.endswith("/rest/networkconf"):
            return response(200, json.dumps(NETWORKCONF))
        if url.endswith("/system-log/all"):
            return response(200, json.dumps({"data": []}))
        return response(404, "not found", "text/plain")


def client(fake: FakeConsole) -> UniFi:
    u = UniFi(host="192.168.1.1", api_key="test-key")
    u.s.request = fake  # type: ignore[method-assign]
    return u


def watchdog_on(u: UniFi) -> watchdog.Watchdog:
    wd = watchdog.Watchdog.__new__(watchdog.Watchdog)
    wd.state = dict(watchdog.Watchdog.DEFAULT_STATE)
    wd._save_state = Mock()
    wd._maybe_canary = Mock()
    wd._maybe_revert = Mock()
    wd._evaluate = Mock()
    wd.debounce_seconds = 1200
    wd.unifi_alert_seconds = 900
    wd.wan_names = {"wan1": "Fiber", "wan2": "Starlink"}
    wd.tg = Mock()
    wd.tg.send.return_value = True
    wd.unifi = u
    return wd


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def balanced(text: str, ch: str) -> bool:
    """Telegram Markdown v1 needs paired delimiters outside code spans."""
    outside, in_code = [], False
    for c in text:
        if c == "`":
            in_code = not in_code
        elif not in_code:
            outside.append(c)
    return "".join(outside).count(ch) % 2 == 0


def sent(wd: watchdog.Watchdog) -> list[str]:
    return [c.args[0] for c in wd.tg.send.call_args_list]


class ClientTests(unittest.TestCase):
    """Every way the console can be unavailable surfaces as UniFiUnavailable."""

    def test_every_outage_mode_is_unifi_unavailable(self):
        for name, make in FAILURES.items():
            with self.subTest(name):
                fake = FakeConsole()
                fake.failure = make
                with self.assertRaises(UniFiUnavailable):
                    client(fake).wan_state()

    def test_html_body_says_what_came_back(self):
        fake = FakeConsole()
        fake.failure = FAILURES["200 with HTML (app booting)"]
        with self.assertRaises(UniFiUnavailable) as caught:
            client(fake).wan_state()
        msg = str(caught.exception)
        self.assertIn("stat/device", msg)
        self.assertIn("text/html", msg)
        self.assertIn("<!DOCTYPE html>", msg)

    def test_moved_endpoint_is_an_error_but_not_transient(self):
        fake = FakeConsole()
        fake.failure = lambda: response(404, "not found", "text/plain")
        with self.assertRaises(UniFiError) as caught:
            client(fake).wan_state()
        self.assertNotIsInstance(caught.exception, UniFiUnavailable)

    def test_rejected_api_key_points_at_the_verify_script(self):
        fake = FakeConsole()
        fake.failure = lambda: response(401, "", "text/plain")
        with self.assertRaises(UniFiError) as caught:
            client(fake).wan_state()
        self.assertNotIsInstance(caught.exception, UniFiUnavailable)
        self.assertIn("verify_unifi_auth.py", str(caught.exception))

    def test_local_admin_relogs_in_once_on_401(self):
        u = UniFi(host="192.168.1.1", username="admin", password="pw")
        u.s.request = Mock(side_effect=[  # type: ignore[method-assign]
            response(401, ""), response(200, "{}"),
            response(200, json.dumps(gateway(True)))])
        self.assertTrue(u.wan_state()["wan1_up"])
        methods = [c.args[0] for c in u.s.request.call_args_list]
        self.assertEqual(methods, ["GET", "POST", "GET"])

    def test_login_against_a_dead_console_is_unavailable(self):
        u = UniFi(host="192.168.1.1", username="admin", password="pw")
        u.s.request = Mock(side_effect=[  # type: ignore[method-assign]
            response(401, ""), requests.ConnectTimeout("connect timeout=15")])
        with self.assertRaises(UniFiUnavailable):
            u.wan_state()

    def test_json_without_data_is_unifi_error(self):
        u = UniFi(host="192.168.1.1", api_key="test-key")
        u.s.request = Mock(return_value=response(200, '{"meta": {"rc": "ok"}}'))  # type: ignore[method-assign]
        with self.assertRaises(UniFiError):
            u.wan_state()

    def test_wan_names_raises_instead_of_returning_placeholders(self):
        fake = FakeConsole()
        fake.failure = FAILURES["502 Bad Gateway"]
        with self.assertRaises(UniFiUnavailable):
            client(fake).wan_names()

    def test_healthy_console_still_reads(self):
        fake = FakeConsole()
        fake.wan1_up = False
        u = client(fake)
        self.assertFalse(u.wan_state()["wan1_up"])
        self.assertEqual(u.wan_names(), {"wan1": "Fiber", "wan2": "Starlink"})


@patch("watchdog.KILL")
class WatchdogTests(unittest.TestCase):

    def setUp(self):
        self.fake = FakeConsole()
        self.wd = watchdog_on(client(self.fake))

    def test_run_once_survives_every_outage_mode_without_starting_an_outage(self, kill):
        kill.exists.return_value = False
        for name, make in FAILURES.items():
            with self.subTest(name):
                fake = FakeConsole()
                fake.failure = make
                wd = watchdog_on(client(fake))
                wd.run_once()
                self.assertIsNone(wd.state["down_since"])
                self.assertIsNotNone(wd.state["unifi_down_since"])
                wd._maybe_revert.assert_not_called()   # it gates on primary up
                wd._maybe_canary.assert_not_called()
                wd._evaluate.assert_not_called()
                wd.tg.send.assert_not_called()

    def test_blind_time_does_not_count_toward_the_debounce(self, kill):
        kill.exists.return_value = False
        clock = Clock(100)
        with patch("time.time", clock):
            self.fake.wan1_up = False
            self.wd.run_once()                       # t=100: Fiber seen down
            self.assertEqual(self.wd.state["down_since"], 100)

            self.fake.failure = FAILURES["502 Bad Gateway"]
            for t in range(130, 1400, 30):           # dark longer than the debounce
                clock.now = t
                self.wd.run_once()
            self.fake.failure = None

            clock.now = 1400                         # back; Fiber still down
            self.wd.run_once()
            # Counting the blind spot would make this 1300s > 1200s and switch.
            self.wd._evaluate.assert_not_called()
            self.assertEqual(self.wd.state["down_since"], 100 + (1400 - 130))

            clock.now = self.wd.state["down_since"] + 1200   # full debounce, observed
            self.wd.run_once()
        self.wd._evaluate.assert_called_once_with(1200)

    def test_long_dark_spell_alerts_once_then_announces_recovery(self, kill):
        kill.exists.return_value = False
        clock = Clock(0)
        with patch("time.time", clock):
            self.fake.failure = FAILURES["connect timeout"]
            for t in (0, 30, 899):
                clock.now = t
                self.wd.run_once()
            self.wd.tg.send.assert_not_called()

            clock.now = 900
            self.wd.run_once()
            clock.now = 930
            self.wd.run_once()
            self.assertEqual(len(sent(self.wd)), 1)
            alert = sent(self.wd)[0]
            self.assertIn("Can't read WAN state from UniFi", alert)
            self.assertIn("15m 0s", alert)
            self.assertIn("firmware or Network app update", alert)
            for ch in "*_":
                self.assertTrue(balanced(alert, ch), f"unbalanced {ch!r}")

            self.fake.failure = None
            clock.now = 960
            self.wd.run_once()
        self.assertEqual(len(sent(self.wd)), 2)
        self.assertIn("answering again", sent(self.wd)[1])
        self.assertIsNone(self.wd.state["unifi_down_since"])
        self.assertFalse(self.wd.state["unifi_alerted"])

    def test_undelivered_alert_is_retried_not_marked_sent(self, kill):
        kill.exists.return_value = False
        clock = Clock(0)
        self.wd.tg.send.return_value = False         # no internet either
        with patch("time.time", clock):
            self.fake.failure = FAILURES["connection refused"]
            for t in (0, 900, 930):
                clock.now = t
                self.wd.run_once()
            self.assertEqual(self.wd.tg.send.call_count, 2)
            self.assertFalse(self.wd.state["unifi_alerted"])

            self.wd.tg.send.return_value = True
            clock.now = 960
            self.wd.run_once()
        self.assertTrue(self.wd.state["unifi_alerted"])

    def test_short_blip_recovers_silently(self, kill):
        kill.exists.return_value = False
        clock = Clock(0)
        with patch("time.time", clock):
            self.fake.failure = FAILURES["503 Service Unavailable"]
            self.wd.run_once()
            self.fake.failure = None
            clock.now = 60
            self.wd.run_once()
        self.wd.tg.send.assert_not_called()
        self.assertIsNone(self.wd.state["unifi_down_since"])

    def test_persistent_error_is_logged_as_error_and_not_called_transient(self, kill):
        kill.exists.return_value = False
        clock = Clock(0)
        self.fake.failure = lambda: response(401, "", "text/plain")
        with patch("time.time", clock), \
                self.assertLogs("wan-watchdog", level="ERROR"):
            self.wd.run_once()
            clock.now = 900
            self.wd.run_once()
        self.assertIn("does not look transient", sent(self.wd)[0])

    def test_recovery_loads_real_names_when_startup_could_not(self, kill):
        kill.exists.return_value = False
        self.wd.wan_names = {"wan1": "WAN1", "wan2": "WAN2"}
        self.fake.failure = FAILURES["connect timeout"]
        self.wd.run_once()
        self.fake.failure = None
        self.wd.run_once()
        self.assertEqual(self.wd.wan_names, {"wan1": "Fiber", "wan2": "Starlink"})

    def test_failed_name_refresh_keeps_the_names_we_have(self, kill):
        kill.exists.return_value = False
        self.fake.failure = FAILURES["connect timeout"]
        self.wd.run_once()
        self.fake.failure = None
        self.fake.broken["/rest/networkconf"] = FAILURES["502 Bad Gateway"]
        self.wd.run_once()
        self.assertEqual(self.wd.wan_names, {"wan1": "Fiber", "wan2": "Starlink"})

    def test_down_alert_survives_a_failed_event_lookup(self, kill):
        kill.exists.return_value = False
        self.fake.wan1_up = False
        self.fake.broken["/system-log/all"] = FAILURES["502 Bad Gateway"]
        self.wd.run_once()
        self.assertIn("Fiber is down", sent(self.wd)[0])
        self.assertIn("Recent WAN events: unavailable", sent(self.wd)[0])
        self.assertTrue(self.wd.state["notified_down"])


class LoopTests(unittest.TestCase):

    def test_tick_survives_a_unifi_error_raised_mid_tick(self):
        wd = watchdog_on(client(FakeConsole()))
        wd.run_once = Mock(side_effect=UniFiUnavailable("GET x: HTTP 502"))
        wd.tick()

    def test_tick_still_crashes_on_a_bug(self):
        wd = watchdog_on(client(FakeConsole()))
        wd.run_once = Mock(side_effect=KeyError("wan1_up"))
        with self.assertRaises(KeyError):
            wd.tick()

    def test_startup_with_the_console_down_reaches_the_loop(self):
        class LoopReached(Exception):
            pass

        fake = FakeConsole()
        fake.failure = FAILURES["connect timeout"]
        u = UniFi(host="192.168.1.1", username="admin", password="pw")
        u.s.request = fake  # type: ignore[method-assign]
        wd = watchdog_on(u)
        wd.wan_names = {"wan1": "WAN1", "wan2": "WAN2"}
        wd.approvals = Mock()
        wd.poll_seconds, wd.max_spend, wd.dry_run = 30, 200.0, True
        wd.approval_mode, wd.revert_day, wd.billing_reset_day = "auto", 0, 0
        with patch("watchdog.Watchdog", return_value=wd), \
                patch("watchdog.KILL") as kill, \
                patch("logging.basicConfig"), \
                patch("time.sleep", side_effect=LoopReached):
            kill.exists.return_value = False
            with self.assertRaises(LoopReached):
                watchdog.main()
        wd.approvals.start.assert_called_once()
        self.assertIsNotNone(wd.state["unifi_down_since"])


class TelegramTests(unittest.TestCase):

    def test_send_survives_no_internet_and_keeps_the_token_out_of_logs(self):
        tg = TelegramNotifier("123456789:AAfake", "chat")
        err = requests.ConnectionError(
            "HTTPSConnectionPool(host='api.telegram.org', port=443): Max retries "
            "exceeded with url: /bot123456789:AAfake/sendMessage")
        with patch("requests.post", side_effect=err), \
                self.assertLogs("notify", level="ERROR") as logs:
            self.assertFalse(tg.send("hi"))
        self.assertNotIn("AAfake", "\n".join(logs.output))

    def test_send_reports_delivery(self):
        tg = TelegramNotifier("123456789:AAfake", "chat")
        with patch("requests.post", return_value=Mock(status_code=200)):
            self.assertTrue(tg.send("hi"))
        with patch("requests.post", return_value=Mock(status_code=400, text="bad")):
            self.assertFalse(tg.send("hi"))


if __name__ == "__main__":
    unittest.main()
