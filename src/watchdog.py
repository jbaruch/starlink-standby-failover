#!/usr/bin/env python3
"""Primary WAN down long enough? Take Starlink out of Standby Mode.

Your gateway already fails over on its own, and a Starlink line in Standby Mode
still passes traffic — just throttled to roughly 0.3-0.6 Mbps. So this is not
what keeps you online during an outage. It is what decides whether being online
means 0.5 Mbps or a real plan.

Design notes worth keeping in your head:

* Detection is per-WAN (`wan1.up` off the gateway object). UniFi's Alarm Manager
  has no per-WAN and no failover trigger, and its site-wide alarm fires for the
  backup link's own hiccups too, so alarms are not usable as the trigger.
* Because standby still carries traffic, by the time we get here failover has
  ALREADY happened and we still have a working path to Starlink's API and to
  Telegram. That is the whole reason this can work unattended.
* DEBOUNCE is the first safety property. Brief blips self-restore; a short
  debounce turns them into charges, while a sane one ignores them and still
  catches a real outage.
* MAX_SPEND_USD is the second, but treat it as a nonsense-tripwire rather than a
  budget: a guard that refuses a legitimate switch defeats the point of owning a
  backup. `change-options` carries a live per-product `proratedPrice` on a plain
  GET, so the exact price is known before anything is charged.
* APPROVAL_MODE=confirm is the third and puts a human on the trigger. Consider
  leaving it off: waiting for a tap means the backup does not fire while you are
  asleep, which is the outage you most want it for.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import pathlib
import sys
import time
from typing import Any

from notify import ApprovalServer, TelegramNotifier, renewal_instructions
from starlink import (SessionExpired, Starlink, StarlinkError, load_session,
                      session_age_days)
from unifi import UniFi, load_api_key

log = logging.getLogger("wan-watchdog")

HERE = pathlib.Path(__file__).resolve().parent
STATE = pathlib.Path(os.environ.get("WATCHDOG_STATE", HERE / "state.json"))
KILL = pathlib.Path(os.environ.get("WATCHDOG_KILL_SWITCH", HERE / "DISABLED"))


def env(name: str, default: str | None = None, required: bool = False) -> str:
    v = os.environ.get(name, default)
    if required and not v:
        raise SystemExit(f"{name} is required; see .env.example")
    return v or ""


class Watchdog:
    def __init__(self) -> None:
        self.poll_seconds = int(env("POLL_SECONDS", "30"))
        self.debounce_seconds = int(env("DEBOUNCE_MINUTES", "20")) * 60
        self.max_spend = float(env("MAX_SPEND_USD", required=True))
        self.dry_run = env("DRY_RUN", "true").lower() != "false"

        # Plan ids are region- and hardware-specific. `recon.py` prints the ones
        # your line is actually offered, with live prices — there is no sane
        # default to guess at. Naming the target explicitly also makes the cost
        # exact: Starlink's `resume` endpoint restores an unnamed previous plan
        # and could only ever be gated on a worst case.
        self.target_product = env("TARGET_PRODUCT_ID", required=True)

        # Daily. Not because the login is fragile — it is good for about a
        # year — but because the endpoints are undocumented and can move under
        # you at any time, and that is what you want to hear about quickly.
        self.canary_interval = int(env("CANARY_INTERVAL_HOURS", "24")) * 3600
        # Measured cookie life is ~365 days; warn with a month to spare.
        self.session_warn_days = float(env("SESSION_WARN_DAYS", "330"))
        # Named in the renewal alert so the message is self-contained a year
        # from now, when nobody remembers where this is deployed.
        self.deploy_dir = env("DEPLOY_DIR", "")

        # Day of month to return the line to Standby Mode; 0 disables. Set it to
        # your billing-reset day minus a few days of slack. You already paid the
        # prorated remainder and it is not refundable, so reverting early
        # donates it — but leaving it to the last day risks a queued standby
        # missing the billing boundary and costing a full month at plan rate.
        self.revert_day = int(env("REVERT_DAY_OF_MONTH", "0"))
        # Day your Starlink bill renews. With it set, the revert is attempted
        # every day from REVERT_DAY_OF_MONTH up to (not including) this day,
        # instead of only once — see in_revert_window().
        self.billing_reset_day = int(env("BILLING_RESET_DAY", "0"))

        self.approval_mode = env("APPROVAL_MODE", "auto").lower()
        if self.approval_mode not in ("confirm", "auto"):
            raise SystemExit("APPROVAL_MODE must be 'confirm' or 'auto'")

        self.unifi = UniFi(
            host=env("UNIFI_HOST", required=True),
            api_key=load_api_key(),
            username=env("UNIFI_USERNAME"),
            password=env("UNIFI_PASSWORD"),
            site=env("UNIFI_SITE", "default"),
        )
        self._starlink_session = load_session()

        self.tg = TelegramNotifier(env("TELEGRAM_BOT_TOKEN", required=True),
                                   env("TELEGRAM_CHAT_ID", required=True))
        self.approvals = ApprovalServer(
            port=int(env("APPROVAL_PORT", "8788")),
            base_url=env("APPROVAL_BASE_URL", "http://localhost:8788"),
            ttl_seconds=int(env("APPROVAL_TIMEOUT_MINUTES", "30")) * 60,
        )
        self.state = self._load_state()
        self.wan_names = {"wan1": "WAN1", "wan2": "WAN2"}

    # -- state --------------------------------------------------------------

    DEFAULT_STATE: dict[str, Any] = {
        "down_since": None, "notified_down": False, "approval_id": None,
        "last_resume_at": None, "last_estimate": None, "last_canary": 0,
        "last_revert_month": None, "last_revert_attempt": None,
    }

    def _load_state(self) -> dict[str, Any]:
        """Merge defaults over whatever is on disk.

        A state file written by an older version is missing newer keys, and a
        KeyError on startup would take the watchdog down — plausibly during the
        outage it exists to handle. Missing keys get defaults; unknown keys are
        left alone.
        """
        state = dict(self.DEFAULT_STATE)
        if STATE.exists():
            on_disk = json.loads(STATE.read_text())
            if isinstance(on_disk, dict):
                state.update(on_disk)
                for k, v in self.DEFAULT_STATE.items():
                    state.setdefault(k, v)
        return state

    def _save_state(self) -> None:
        STATE.write_text(json.dumps(self.state, indent=2))

    def _reset_outage(self) -> None:
        self.state.update({"down_since": None, "notified_down": False,
                           "approval_id": None})
        self._save_state()

    @property
    def primary(self) -> str:
        return self.wan_names["wan1"]

    @property
    def backup(self) -> str:
        return self.wan_names["wan2"]

    # -- scheduled work ------------------------------------------------------

    def _maybe_canary(self) -> None:
        """Periodic proof the whole chain still works, run in-process.

        In-process rather than cron so there is one fewer thing to install and
        one fewer thing to forget. Some NAS platforms also make installing a
        crontab awkward for unprivileged users even when cron itself runs.
        Keeps the Starlink session warm as a side effect.
        """
        last = self.state.get("last_canary") or 0
        if time.time() - last < self.canary_interval:
            return
        self.state["last_canary"] = time.time()
        self._save_state()
        try:
            sl = Starlink(self._starlink_session)
            sl.check_session()
            sub = sl.subscription()
            option = sl.plan_option(sub["serviceLineNumber"], self.target_product)
            log.info("canary OK: standby=%s, %s would cost $%.2f",
                     sub["isStandby"], option["name"], option["prorated"])

            # The login cannot be renewed without a human. Warn ahead of time
            # rather than discovering it during an outage.
            age = session_age_days()
            if age is not None and age >= self.session_warn_days:
                log.warning("Starlink session is %.1f days old", age)
                self.tg.send(renewal_instructions(age, self.deploy_dir))
        except (StarlinkError, KeyError) as e:
            log.error("CANARY FAILED: %s", e)
            self.tg.send(
                f"🔴 *Starlink auto-upgrade is NOT armed.*\n`{e}`\n\n"
                f"_Failover still works — {self.backup} carries traffic on "
                f"standby at ~0.5 Mbps. What would not happen is the switch to a "
                f"full plan, so an outage means slow internet, not no internet._")

    def in_revert_window(self, day: int) -> bool:
        """Is `day` inside [REVERT_DAY_OF_MONTH, BILLING_RESET_DAY)?

        A WINDOW, not a single day, because a single day leaves a hole: an
        outage that switches the plan *after* the revert day but before the
        billing boundary would never be reverted, and you would be charged a
        full month at plan rate. Checking every day in the run-up closes it.

        Wraps when the revert day falls after the reset day (e.g. reset on the
        1st, revert on the 27th).
        """
        if not self.revert_day:
            return False
        if not self.billing_reset_day:
            return day == self.revert_day
        if self.revert_day < self.billing_reset_day:
            return self.revert_day <= day < self.billing_reset_day
        return day >= self.revert_day or day < self.billing_reset_day

    def _maybe_revert(self, primary_up: bool) -> None:
        """Put the line back on Standby Mode ahead of the billing boundary.

        Checks once per day across the whole window. The last confirmed month
        is bookkeeping, not a gate: a later activation in that same month must
        still be reverted. Pending standby is observed without another write.

        Never reverts while the primary is still down: you are paying for the
        plan precisely because you need it right now.
        """
        today = datetime.date.today()
        if not self.in_revert_window(today.day):
            return
        if not primary_up:
            log.warning("revert window: %s still down — keeping the paid plan",
                        self.primary)
            return
        month = f"{today.year}-{today.month:02d}"
        stamp = today.isoformat()
        if self.state.get("last_revert_attempt") == stamp:
            return
        self.state["last_revert_attempt"] = stamp
        self._save_state()

        try:
            sl = Starlink(self._starlink_session)
            sub = sl.subscription()
            if sub["isStandby"] or sub["isStandbyPending"]:
                log.info("revert window: already standby (pending=%s), nothing to do",
                         sub["isStandbyPending"])
                self.state["last_revert_month"] = month
                self._save_state()
                return

            if self.dry_run:
                log.warning("DRY_RUN: would return %s to standby", sub["productId"])
                return

            log.warning("revert window: line is on %s — returning to standby",
                        sub["productId"])
            after = sl.back_to_standby(sub["serviceLineNumber"])
            result = ("standby" if after["isStandby"]
                      else "standby PENDING" if after["isStandbyPending"]
                      else f"still {after['productId']}")
            log.warning("revert result: %s", result)
            if after["isStandby"] or after["isStandbyPending"]:
                self.state["last_revert_month"] = month
                self._save_state()
            else:
                log.error("revert did not take — will retry tomorrow if the "
                          "window is still open")
            self.tg.send(
                f"🌙 *Back to Standby Mode.*\n"
                f"Was on `{sub['productId']}`, now: {result}.\n"
                f"_Reverted ahead of your billing reset on day "
                f"{self.billing_reset_day or '?'}, to leave slack._")
        except (StarlinkError, KeyError) as e:
            log.error("REVERT FAILED: %s", e)
            self.tg.send(
                f"🔴 *Failed to return Starlink to standby.*\n`{e}`\n\n"
                f"_If this is not fixed before your billing reset you will be "
                f"charged a full month at the plan rate._")

    # -- the loop -----------------------------------------------------------

    def run_once(self) -> None:
        if KILL.exists():
            log.warning("kill switch present at %s — standing down", KILL)
            return

        state = self.unifi.wan_state()
        self._maybe_canary()
        self._maybe_revert(primary_up=state["wan1_up"])
        now = state["at"]

        if state["wan1_up"]:
            if self.state["down_since"] is not None:
                outage = now - self.state["down_since"]
                log.info("%s back after %.0fs", self.primary, outage)
                if self.state["notified_down"]:
                    self.tg.send(f"✅ *{self.primary} is back* after {_dur(outage)}.\n"
                                 f"{self.backup} left on standby.")
                self._reset_outage()
            return

        # --- primary is down ---
        if self.state["down_since"] is None:
            self.state["down_since"] = now
            self._save_state()
            events = self.unifi.recent_wan_events(since_seconds=900)
            log.warning("%s DOWN (backup up=%s)", self.primary, state["wan2_up"])
            backup_state = ("up (standby, ~0.5 Mbps)" if state["wan2_up"]
                            else "*also down*")
            self.tg.send(
                f"⚠️ *{self.primary} is down.*\n"
                f"{self.backup}: {backup_state}\n"
                f"Waiting {self.debounce_seconds // 60} min before considering a "
                f"plan switch.\n"
                f"_Recent WAN events: {len(events)}_"
            )
            self.state["notified_down"] = True
            self._save_state()
            return

        down_for = now - self.state["down_since"]

        # An approval is already outstanding — check whether it landed.
        if self.state["approval_id"]:
            self._check_approval(down_for)
            return

        if down_for < self.debounce_seconds:
            log.info("%s down %.0fs of %ds debounce", self.primary, down_for,
                     self.debounce_seconds)
            return

        if not state["wan2_up"]:
            log.error("%s down %.0fs but %s is ALSO down — no path to the account "
                      "API, nothing to do", self.primary, down_for, self.backup)
            return

        log.warning("%s down %.0fs, debounce passed — evaluating switch",
                    self.primary, down_for)
        self._evaluate(down_for)

    # -- decision -----------------------------------------------------------

    def _evaluate(self, down_for: float) -> None:
        sl = Starlink(self._starlink_session)

        try:
            sl.check_session()
        except SessionExpired as e:
            log.error("STARLINK SESSION DEAD — not armed: %s", e)
            self.tg.send("🔴 *Starlink session expired.* Cannot change the plan. "
                         "Log in at starlink.com/account and re-capture the session.")
            raise

        sub = sl.subscription()
        if not (sub["isStandby"] or sub["isPaused"] or sub["isStandbyPending"]):
            log.info("already on a full plan — nothing to switch")
            self._reset_outage()
            return

        line = sub["serviceLineNumber"]

        # Cell capacity is REPORTED, never enforced. The endpoint lives under
        # /shop/ and is what the signup flow uses to decide whether a NEW
        # customer may order at an address, its false path is untested, and the
        # "paused line loses its cell slot" warning it came from was about fixed
        # Residential rather than Roam. A guard built on an untested assumption
        # can only ever stop a switch you wanted. If the switch is genuinely
        # refused, the API says so and that gets surfaced.
        if sub["addressReferenceId"]:
            try:
                if not sl.address_has_capacity(sub["addressReferenceId"]):
                    log.warning("address-has-capacity reported false — "
                                "attempting the switch anyway")
            except StarlinkError as e:
                log.warning("capacity check failed (%s) — continuing", e)

        # Exact prorated cost of the plan we actually intend to switch to.
        option = sl.plan_option(line, self.target_product)
        cost = option["prorated"]
        self.state["last_estimate"] = {"at": time.time(), "cost": cost,
                                       "plan": option["name"]}
        self._save_state()
        log.info("target %s: $%.2f prorated now (monthly $%s)",
                 option["name"], cost, option["monthly"])

        if cost > self.max_spend:
            log.error("$%.2f exceeds tripwire $%.2f — refusing", cost, self.max_spend)
            self.tg.send(f"🚫 *Switch blocked by MAX_SPEND_USD.*\n"
                         f"{self.primary} down {_dur(down_for)}. {option['name']} "
                         f"would cost *${cost:.2f}*, tripwire is "
                         f"${self.max_spend:.2f}.")
            return

        if self.dry_run:
            log.warning("DRY_RUN: would switch for $%.2f", cost)
            self.tg.send(f"🧪 *DRY RUN* — would switch to {option['name']} now.\n"
                         f"{self.primary} down {_dur(down_for)}, cost "
                         f"*${cost:.2f}*.\n_Set DRY_RUN=false to arm._")
            self._reset_outage()
            return

        if self.approval_mode == "confirm":
            self._ask(down_for, cost, sub, option["name"])
            return

        self._switch(sl, sub, cost, down_for)

    def _ask(self, down_for: float, cost: float, sub: dict[str, Any],
             plan_name: str) -> None:
        rid, approve, deny = self.approvals.create(
            {"cost": cost, "down_for": down_for, "line": sub["serviceLineNumber"]})
        self.state["approval_id"] = rid
        self._save_state()
        ttl = self.approvals.ttl // 60
        self.tg.send(
            f"🛰 *Switch {self.backup} to a full plan?*\n"
            f"{self.primary} has been down *{_dur(down_for)}*.\n"
            f"{plan_name} costs *${cost:.2f}* (prorated remainder, "
            f"non-refundable).\n\n"
            f"[✅ Switch now]({approve})\n"
            f"[❌ Stay on standby]({deny})\n\n"
            f"_Expires in {ttl} min. Links must be reachable from your phone._",
            disable_preview=True,
        )
        log.warning("approval requested (%s), cost $%.2f", rid, cost)

    def _check_approval(self, down_for: float) -> None:
        rid = self.state["approval_id"]
        decision = self.approvals.poll(rid)
        if decision == "pending":
            log.info("awaiting approval (%s), %s down %.0fs", rid, self.primary,
                     down_for)
            return

        self.state["approval_id"] = None
        self._save_state()

        if decision == "approved":
            sl = Starlink(self._starlink_session)
            sub = sl.subscription()
            cost = (self.state.get("last_estimate") or {}).get("cost") or 0.0
            self._switch(sl, sub, float(cost), down_for)
        elif decision == "denied":
            log.warning("switch denied by human — staying on standby")
            self.tg.send("👍 Staying on standby. I won't ask again this outage.")
            self.state["notified_down"] = False  # don't re-prompt this outage
            self._save_state()
        else:
            log.warning("approval %s — no answer", decision)
            self.tg.send("⌛️ Switch request expired with no answer. "
                         "Still on standby.")

    # -- the money ----------------------------------------------------------

    def _switch(self, sl: Starlink, sub: dict[str, Any], cost: float,
                down_for: float) -> None:
        line = sub["serviceLineNumber"]
        log.warning("SWITCHING service line %s to %s ($%.2f)",
                    line, self.target_product, cost)
        after = sl.switch_to_product(line, self.target_product)

        self.state["last_resume_at"] = time.time()
        # A new activation can happen after today's standby check. Re-arm the
        # revert so it is scheduled even within the same day or month.
        self.state["last_revert_attempt"] = None
        self.state["last_revert_month"] = None
        self._reset_outage()
        log.warning("switched: now on %s", after["productId"])
        self.tg.send(
            f"🚀 *{self.backup} switched to a full plan.* Full speed in a few "
            f"minutes.\n"
            f"{self.primary} was down {_dur(down_for)}. Now on "
            f"`{after['productId']}`.\n\n"
            f"_The prorated remainder is already paid and is not refundable — go "
            f"back to standby before your next billing date, not when "
            f"{self.primary} returns._")


def _dur(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
        stream=sys.stdout,
    )
    wd = Watchdog()
    wd.approvals.start()
    wd.unifi.login()
    try:
        wd.wan_names = wd.unifi.wan_names()
    except Exception as e:  # noqa: BLE001 - cosmetic only, never fatal
        log.warning("could not read WAN names (%s) — using WAN1/WAN2", e)
    log.info("watchdog up: primary=%s backup=%s poll=%ds debounce=%ds "
             "tripwire=$%.2f dry_run=%s mode=%s revert_window=%s",
             wd.primary, wd.backup, wd.poll_seconds, wd.debounce_seconds,
             wd.max_spend, wd.dry_run, wd.approval_mode,
             f"{wd.revert_day}..{wd.billing_reset_day}" if wd.revert_day else "off")
    while True:
        try:
            wd.run_once()
        except StarlinkError as e:
            log.error("switch path failed: %s", e)
        time.sleep(wd.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
