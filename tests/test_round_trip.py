"""Verify exact upgrade state, live variant pricing, and round-trip cleanup."""
import os
import unittest
from unittest.mock import Mock, patch

from starlink import Starlink, StarlinkError
import test_write_path
import watchdog

LINE = "SL-0000000-00000-00"
ROAM = "us-consumer-subscription-mini-roam-100-0526"
PENDING = {"serviceLineNumber": LINE, "productId": ROAM,
           "delayedProductId": "us-standby-mode", "isStandby": False,
           "isStandbyPending": True, "isPaused": False, "addressReferenceId": None}
ACTIVE = dict(PENDING, isStandbyPending=False, delayedProductId=None)
PRICE = {"productId": ROAM, "name": "Roam 100GB", "monthly": 55, "prorated": 0.0}


def client():
    sl = Starlink("Starlink.Com.Sso=y")
    sl.change_product = Mock()
    return sl


class RoundTripTests(unittest.TestCase):
    def test_immediate_variant_quotes_zero_to_cancel_pending(self):
        sl = client()
        sl.change_options = Mock(return_value={"changeOptions": [{
            "productResponse": {"productId": ROAM, "proratedPrice": 1.14},
            "variants": [{"effectiveTimestamp": None, "oneTimeAmount": 0}],
        }]})
        self.assertEqual(sl.plan_option(LINE, ROAM)["prorated"], 0.0)

    def test_no_immediate_variant_cannot_be_priced_as_an_upgrade(self):
        sl = client()
        sl.change_options = Mock(return_value={"changeOptions": [{
            "productResponse": {"productId": ROAM, "proratedPrice": 1.14},
            "variants": [{"effectiveTimestamp": "2026-10-06", "oneTimeAmount": 0}],
        }]})
        with self.assertRaisesRegex(StarlinkError, "no immediate change"):
            sl.plan_option(LINE, ROAM)

    @patch("starlink.time.sleep")
    def test_pending_standby_is_not_a_successful_upgrade(self, sleep):
        sl = client()
        sl.subscription = Mock(return_value=PENDING)
        with self.assertRaisesRegex(StarlinkError, "not confirmed"):
            sl.switch_to_product(LINE, ROAM)
        sl.change_product.assert_called_once_with(LINE, ROAM, schedule=False)

    @patch("starlink.time.sleep")
    def test_wrong_full_plan_is_not_a_successful_upgrade(self, sleep):
        sl = client()
        sl.subscription = Mock(return_value=dict(ACTIVE, productId="wrong-plan"))
        with self.assertRaisesRegex(StarlinkError, "not confirmed"):
            sl.switch_to_product(LINE, ROAM)

    @patch("starlink.time.sleep")
    def test_upgrade_polls_until_pending_is_cleared(self, sleep):
        sl = client()
        sl.subscription = Mock(side_effect=[PENDING, ACTIVE])
        self.assertEqual(sl.switch_to_product(LINE, ROAM), ACTIVE)
        sleep.assert_called_once_with(2)

    def round_trip_client(self):
        sl = Mock()
        sl.subscription.return_value = PENDING
        sl.plan_option.return_value = PRICE
        sl.switch_to_product.return_value = ACTIVE
        sl.back_to_standby.return_value = PENDING
        return sl

    @patch.dict(os.environ, {"CONFIRM_SPEND": "yes", "MAX_SPEND_USD": "200"})
    def test_pending_standby_round_trip_verifies_both_writes(self):
        sl = self.round_trip_client()
        u = Mock()
        u.wan_state.return_value = {"wan1_up": True}
        with patch("test_write_path.Starlink", return_value=sl), \
             patch("test_write_path.load_session", return_value="Starlink.Com.Sso=y"), \
             patch("test_write_path.UniFi", return_value=u):
            self.assertEqual(test_write_path.main(), 0)
        sl.switch_to_product.assert_called_once_with(LINE, ROAM)
        sl.back_to_standby.assert_called_once_with(LINE)

    @patch.dict(os.environ, {"CONFIRM_SPEND": "yes", "MAX_SPEND_USD": "200"})
    def test_failed_upgrade_still_restores_standby(self):
        sl = self.round_trip_client()
        sl.switch_to_product.side_effect = StarlinkError("upgrade timed out")
        u = Mock()
        u.wan_state.return_value = {"wan1_up": True}
        with patch("test_write_path.Starlink", return_value=sl), \
             patch("test_write_path.load_session", return_value="Starlink.Com.Sso=y"), \
             patch("test_write_path.UniFi", return_value=u):
            with self.assertRaisesRegex(StarlinkError, "upgrade timed out"):
                test_write_path.main()
        sl.back_to_standby.assert_called_once_with(LINE)

    @patch.dict(os.environ, {"CONFIRM_SPEND": ""})
    def test_preview_makes_no_write(self):
        sl = self.round_trip_client()
        with patch("test_write_path.Starlink", return_value=sl), \
             patch("test_write_path.load_session", return_value="Starlink.Com.Sso=y"):
            self.assertEqual(test_write_path.main(), 0)
        sl.switch_to_product.assert_not_called()
        sl.back_to_standby.assert_not_called()

    def test_outage_debounce_cost_and_switch_cancel_pending_standby(self):
        wd = watchdog.Watchdog.__new__(watchdog.Watchdog)
        wd.state = dict(watchdog.Watchdog.DEFAULT_STATE)
        wd._save_state = Mock()
        wd._maybe_canary = Mock()
        wd._maybe_revert = Mock()
        wd._starlink_session = "Starlink.Com.Sso=y"
        wd.target_product, wd.max_spend, wd.dry_run = ROAM, 200, False
        wd.approval_mode, wd.debounce_seconds = "auto", 1200
        wd.wan_names = {"wan1": "Fiber", "wan2": "Starlink"}
        wd.tg, wd.unifi = Mock(), Mock()
        wd.unifi.recent_wan_events.return_value = []
        wd.unifi.wan_state.side_effect = [
            {"wan1_up": False, "wan2_up": True, "at": t} for t in (100, 1299, 1300)]
        sl = self.round_trip_client()
        with patch("watchdog.Starlink", return_value=sl), patch("watchdog.KILL") as kill:
            kill.exists.return_value = False
            wd.run_once()
            wd.run_once()
            sl.switch_to_product.assert_not_called()
            wd.run_once()
        sl.switch_to_product.assert_called_once_with(LINE, ROAM)
        self.assertEqual(wd.state["last_estimate"]["cost"], 0)
        self.assertIsNone(wd.state["last_revert_attempt"])
        self.assertIsNone(wd.state["last_revert_month"])


if __name__ == "__main__":
    unittest.main()
