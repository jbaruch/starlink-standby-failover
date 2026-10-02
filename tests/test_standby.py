"""Regression tests for the standby request observed on 2026-10-02.

Run with PYTHONPATH=src .venv/bin/python tests/test_standby.py.
All account identifiers are anonymised. No live writes or notifications.
"""
from __future__ import annotations

import copy
import datetime
import os
import unittest
from unittest.mock import Mock, patch

import requests

from starlink import BASE, Starlink, StarlinkError
import revert_to_standby
import watchdog

LINE = "SL-0000000-00000-00"
REFERENCE = "11111111-1111-1111-1111-111111111111"
ROAM = "us-consumer-subscription-mini-roam-100-0526"
STANDBY = "us-standby-mode"
BOUNDARY = "2026-10-06T00:00:00+00:00"
SERVICE_LINES = {"results": [{
    "serviceLineNumber": LINE,
    "subscription": {"subscriptionReferenceId": REFERENCE, "productId": ROAM,
                     "isStandby": False, "isStandbyPending": False,
                     "delayedProductId": None, "canChangeService": True},
}]}
OPTIONS = {"changeOptions": [{
    "productResponse": {"productId": STANDBY, "isStandby": True,
                        "name": "Standby Mode", "price": 10},
    "variants": [{"effectiveTimestamp": BOUNDARY, "immediateCharges": [],
                  "oneTimeAmount": 0, "recurringAmount": 10}],
}]}


def client(outcome: str = "pending") -> Starlink:
    sl = Starlink("Starlink.Com.Sso=y")
    sl._ensure_token = Mock()
    lines = copy.deepcopy(SERVICE_LINES)

    def content(path):
        return lines if path.endswith("service-lines") else copy.deepcopy(OPTIONS)

    def write(*args, **kwargs):
        sub = lines["results"][0]["subscription"]
        if outcome == "pending":
            sub.update(isStandbyPending=True, delayedProductId=STANDBY)
        elif outcome == "active":
            sub.update(isStandby=True, productId=STANDBY)
        response = Mock(status_code=200, content=b"{}")
        response.json.return_value = {"isValid": True, "content": None}
        return response

    sl._content = Mock(side_effect=content)
    sl.s.request = Mock(side_effect=write)
    return sl


def watch() -> watchdog.Watchdog:
    wd = watchdog.Watchdog.__new__(watchdog.Watchdog)
    wd.revert_day, wd.billing_reset_day, wd.dry_run = 3, 6, False
    wd.wan_names = {"wan1": "Fiber", "wan2": "Starlink"}
    wd._starlink_session = "Starlink.Com.Sso=y"
    wd.state = dict(watchdog.Watchdog.DEFAULT_STATE)
    wd._save_state = Mock()
    wd.tg = Mock()
    return wd


class OctoberThird(datetime.date):
    @classmethod
    def today(cls):
        return cls(2026, 10, 3)


class StandbyTests(unittest.TestCase):
    def test_live_product_uuid_schedule_and_empty_body(self):
        sl = client()
        after = sl.back_to_standby(LINE)
        self.assertTrue(after["isStandbyPending"])
        self.assertEqual(after["delayedProductId"], STANDBY)
        args, kwargs = sl.s.request.call_args
        self.assertEqual(args, ("POST", f"{BASE}/api/webagg/v1/public/subscriptions/line/"
                               f"{REFERENCE}/product/{STANDBY}/update?schedule=true"))
        self.assertEqual(kwargs["json"], {})
        self.assertEqual(sl.s.request.call_count, 1)

    def test_already_pending_is_idempotent(self):
        sl = client()
        sl.back_to_standby(LINE)
        sl.back_to_standby(LINE)
        self.assertEqual(sl.s.request.call_count, 1)

    def test_immediate_standby(self):
        sl = client("active")
        opts = copy.deepcopy(OPTIONS)
        opts["changeOptions"][0]["variants"][0]["effectiveTimestamp"] = None
        sl.change_options = Mock(return_value=opts)
        self.assertTrue(sl.back_to_standby(LINE)["isStandby"])
        self.assertTrue(sl.s.request.call_args.args[1].endswith("schedule=false"))

    def test_scheduled_variant_preferred(self):
        sl = client()
        opts = copy.deepcopy(OPTIONS)
        opts["changeOptions"][0]["variants"].insert(0, {"effectiveTimestamp": None})
        sl.change_options = Mock(return_value=opts)
        self.assertEqual(sl.standby_option(LINE)["effectiveTimestamp"], BOUNDARY)

    def test_product_id_is_discovered_when_it_changes_again(self):
        sl = client()
        opts = copy.deepcopy(OPTIONS)
        opts["changeOptions"][0]["productResponse"]["productId"] = "future-standby"
        sl.change_options = Mock(return_value=opts)
        self.assertEqual(sl.standby_option(LINE)["productId"], "future-standby")

    def test_no_standby_option_does_not_write(self):
        sl = client()
        sl.change_options = Mock(return_value={"changeOptions": []})
        with self.assertRaisesRegex(StarlinkError, "no standby plan offered"):
            sl.back_to_standby(LINE)
        sl.s.request.assert_not_called()

    def test_missing_timing_does_not_write(self):
        sl = client()
        opts = copy.deepcopy(OPTIONS)
        opts["changeOptions"][0]["variants"] = []
        sl.change_options = Mock(return_value=opts)
        with self.assertRaisesRegex(StarlinkError, "timing variants"):
            sl.back_to_standby(LINE)
        sl.s.request.assert_not_called()

    @patch("starlink.time.sleep")
    def test_http_success_without_standby_is_failure(self, sleep):
        sl = client("unchanged")
        with self.assertRaisesRegex(StarlinkError, "not confirmed"):
            sl.back_to_standby(LINE)
        self.assertEqual(sl.s.request.call_count, 1)
        self.assertEqual(sleep.call_count, 5)

    @patch("starlink.time.sleep")
    def test_polls_until_pending_is_visible(self, sleep):
        sl = client("unchanged")
        sub = sl.subscription()
        pending = dict(sub, isStandbyPending=True, delayedProductId=STANDBY)
        sl.subscription = Mock(side_effect=[sub, sub, sub, pending])
        self.assertTrue(sl.back_to_standby(LINE)["isStandbyPending"])
        sleep.assert_called_once_with(2)

    def test_named_line_never_writes_to_the_first_unrelated_line(self):
        sl = client()
        lines = copy.deepcopy(SERVICE_LINES)
        other = copy.deepcopy(lines["results"][0])
        other["serviceLineNumber"] = "SL-other"
        other["subscription"]["subscriptionReferenceId"] = "other-reference"
        lines["results"].insert(0, other)
        sl._content = Mock(return_value=lines)
        sl.change_product(LINE, ROAM)
        self.assertIn(f"/line/{REFERENCE}/", sl.s.request.call_args.args[1])
        self.assertTrue(sl.s.request.call_args.args[1].endswith("schedule=false"))

    def test_missing_reference_does_not_write(self):
        sl = client()
        sl.subscription = Mock(return_value={"subscriptionReferenceId": None})
        with self.assertRaisesRegex(StarlinkError, "no subscriptionReferenceId"):
            sl.change_product(LINE, ROAM)
        sl.s.request.assert_not_called()

    def test_transport_failure_is_starlink_error(self):
        sl = client()
        sl.s.request.side_effect = requests.Timeout("timed out")
        with self.assertRaisesRegex(StarlinkError, "timed out"):
            sl.change_product(LINE, ROAM)

    def test_http_422_surfaces_api_validation_error(self):
        sl = client()
        response = Mock(status_code=422, content=b"{}")
        response.json.return_value = {"isValid": False, "errors": ["invalid product"]}
        sl.s.request.side_effect = None
        sl.s.request.return_value = response
        with self.assertRaisesRegex(StarlinkError, "invalid product"):
            sl.change_product(LINE, ROAM)

    @patch("watchdog.datetime.date", OctoberThird)
    def test_dry_run_never_reverts(self):
        wd = watch()
        wd.dry_run = True
        sl = client()
        with patch("watchdog.Starlink", return_value=sl):
            wd._maybe_revert(primary_up=True)
        sl.s.request.assert_not_called()

    @patch("watchdog.datetime.date", OctoberThird)
    def test_live_outage_blocks_revert_without_consuming_attempt(self):
        wd = watch()
        with patch("watchdog.Starlink") as constructor:
            wd._maybe_revert(primary_up=False)
        constructor.assert_not_called()
        self.assertIsNone(wd.state["last_revert_attempt"])

    @patch("watchdog.datetime.date", OctoberThird)
    def test_later_activation_in_same_month_still_reverts(self):
        wd = watch()
        wd.state["last_revert_month"] = "2026-10"
        sl = client()
        with patch("watchdog.Starlink", return_value=sl):
            wd._maybe_revert(primary_up=True)
        sl.s.request.assert_called_once()
        self.assertEqual(wd.state["last_revert_month"], "2026-10")

    @patch("watchdog.datetime.date", OctoberThird)
    @patch("starlink.time.sleep")
    def test_failed_revert_does_not_claim_success(self, sleep):
        wd = watch()
        sl = client("unchanged")
        with patch("watchdog.Starlink", return_value=sl):
            wd._maybe_revert(primary_up=True)
        self.assertIsNone(wd.state["last_revert_month"])
        self.assertIn("Failed to return", wd.tg.send.call_args.args[0])

    def test_wan_is_read_before_scheduled_revert(self):
        wd = watch()
        sequence = []
        wd.unifi = Mock()
        wd.unifi.wan_state.side_effect = lambda: sequence.append("WAN") or {
            "wan1_up": True, "wan2_up": True, "at": 1}
        wd._maybe_canary = Mock()
        wd._maybe_revert = Mock(side_effect=lambda **kw: sequence.append(kw["primary_up"]))
        with patch("watchdog.KILL") as kill:
            kill.exists.return_value = False
            wd.run_once()
        self.assertEqual(sequence, ["WAN", True])

    @patch.dict(os.environ, {"REVERT_DRY_RUN": "false"})
    def test_manual_revert_refuses_when_primary_down(self):
        sl = client()
        u = Mock()
        u.wan_state.return_value = {"wan1_up": False}
        with patch("revert_to_standby.Starlink", return_value=sl), \
             patch("revert_to_standby.load_session", return_value="Starlink.Com.Sso=y"), \
             patch("revert_to_standby.UniFi", return_value=u), \
             patch.object(sl, "check_session"):
            self.assertEqual(revert_to_standby.main(), 1)
        sl.s.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
