"""Starlink consumer account client.

UNSUPPORTED AND UNVERSIONED. Every path below was confirmed against the live
account on 2026-08-29 (line SL-0000000-00000-00, Standby Mode). SpaceX can move
them without notice, which is why check_session() exists and the canary runs
weekly.

Two corrections from the first pass, both found by actually calling things:

  * `accounts/v1/.../reactivation-estimation` returns 422 ACCOUNT_NOT_SUSPENDED
    for a standby line. It is for SUSPENDED accounts and is NOT the cost gate.
    The real prorated price is a field on each option in `change-options`.
  * `webagg/v1/activate/plans/{line}` is the new-customer activation catalogue,
    not this line's options. Wrong endpoint entirely.

Requests go to www.starlink.com/api/... — the same-origin paths the account SPA
itself uses. (api.starlink.com serves some of these too but is not what the app
calls.)
"""
from __future__ import annotations

import os
import pathlib
import time
from typing import Any

import requests

BASE = "https://www.starlink.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

REFRESH_PATH = "/api/auth/auth/refresh-token"


class StarlinkError(RuntimeError):
    pass


def load_session() -> str:
    """Read the session from a file, falling back to an env var.

    Prefer the file. A real captured cookie header contains `$` (Google
    Analytics segments like `_ga_X=GS1.1.$o1$g1$t123`), and docker compose
    interpolates `$` inside env_file values — it silently ate parts of the
    string and warned about undefined variables `o1`, `g1`, `t1788046407`.
    The Starlink tokens happen not to contain `$` so it worked anyway, but
    that is luck. A mounted file is not interpolated by anything, and keeps
    the session out of `docker inspect`.
    """
    path = os.environ.get("STARLINK_SESSION_FILE", "")
    if path:
        p = pathlib.Path(path)
        if not p.exists():
            raise StarlinkError(f"STARLINK_SESSION_FILE={path} does not exist")
        value = p.read_text().strip()
        if not value:
            raise StarlinkError(f"STARLINK_SESSION_FILE={path} is empty")
        return value
    value = os.environ.get("STARLINK_SESSION", "").strip()
    if not value:
        raise StarlinkError(
            "no session: set STARLINK_SESSION_FILE (preferred) or STARLINK_SESSION")
    return value


def session_age_days() -> float | None:
    """How long ago the session file was written, or None if unknowable.

    MEASURED, because third-party write-ups get this wrong: the
    `Starlink.Com.Sso` cookie carries a **one year** expiry (read straight out
    of a browser profile: captured 2026-08-29, expires 2027-08-29). The
    widely-repeated "~15 days" figure does not match observation.

    `refresh-token` mints short-lived (15 minute) access tokens FROM the SSO
    cookie and returns no Set-Cookie for it, so nothing extends the cookie
    itself — but a year is long enough that re-capturing by hand once a year is
    a reasonable answer rather than a wart.

    The server can still invalidate a session early (password change, logout,
    security event), which the canary detects. This function exists so you are
    told the session is aging out *before* an outage discovers it.
    """
    path = os.environ.get("STARLINK_SESSION_FILE", "")
    if not path:
        return None
    p = pathlib.Path(path)
    if not p.exists():
        return None
    return (time.time() - p.stat().st_mtime) / 86400.0


class SessionExpired(StarlinkError):
    """The stored session is dead. A human must log in again — 2SV is mandatory."""


class Starlink:
    def __init__(self, session_header: str, timeout: int = 30) -> None:
        if not session_header or "Starlink.Com.Sso" not in session_header:
            raise StarlinkError("session header must contain Starlink.Com.Sso — see README")
        self.timeout = timeout
        self._access_token = ""
        self._token_type = "Bearer"
        self._token_expires_at = 0.0
        self.s = requests.Session()
        self._session_header = session_header.strip()
        self.s.headers.update({"User-Agent": UA, "Accept": "application/json"})
        for part in self._session_header.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                self.s.cookies.set(k, v, domain=".starlink.com")

    # -- plumbing -----------------------------------------------------------

    def _headers(self, mutating: bool = False) -> dict[str, str]:
        """Deliberately NO manual `cookie` header — let the jar send cookies.

        `Starlink.Com.Access.V1` is short-lived (it expired within the hour).
        Pinning the captured header re-sends the dead token forever: measured
        401 with a frozen header vs 200 from the jar, on the same session,
        seconds apart. The jar picks up refreshed cookies; a frozen string
        cannot.
        """
        h: dict[str, str] = {}
        if self._access_token:
            # Required IN ADDITION to the cookie — see refresh().
            h["Authorization"] = f"{self._token_type} {self._access_token}"
        if mutating:
            h["Content-Type"] = "application/json"
            # Send the CSRF token when we have it. This account's session has
            # none at all, so usually absent. Let the server object rather than
            # refusing on a precondition we inferred.
            token = self.s.cookies.get("XSRF-TOKEN", domain=".starlink.com")
            if token:
                h["x-xsrf-token"] = token
        return h

    def _call(self, method: str, path: str, mutating: bool = False,
              json_body: Any = None, _retried: bool = False) -> Any:
        if path != REFRESH_PATH:
            self._ensure_token()
        try:
            r = self.s.request(method, f"{BASE}{path}", headers=self._headers(mutating),
                               json=json_body, timeout=self.timeout)
        except requests.RequestException as e:
            raise StarlinkError(f"{method} {path} failed: {e}") from e

        # A 401 usually means the short-lived access token aged out, not that
        # the session is gone. Mint a new one from the long-lived SSO cookie
        # and retry once before declaring the backup unarmed.
        if r.status_code == 401 and not _retried and path != REFRESH_PATH:
            self.refresh()
            return self._call(method, path, mutating, json_body, _retried=True)

        if r.status_code in (401, 403):
            raise SessionExpired(f"{method} {path} rejected: HTTP {r.status_code}")
        if r.status_code == 404:
            raise StarlinkError(f"404 on {path} — SPA endpoints moved, re-run recon")
        try:
            body = r.json() if r.content else {}
        except ValueError as e:
            raise StarlinkError(
                f"{method} {path} returned non-JSON (HTTP {r.status_code})") from e
        # The webagg envelope reports failure in-band with HTTP 200 sometimes,
        # and with 422 otherwise. Either way, isValid=False must not look like
        # success to a caller about to spend money.
        if isinstance(body, dict) and body.get("isValid") is False:
            raise StarlinkError(f"{path} returned errors: {body.get('errors')}")
        try:
            r.raise_for_status()
        except requests.RequestException as e:
            raise StarlinkError(f"{method} {path} rejected: HTTP {r.status_code}") from e
        return body

    def _content(self, path: str) -> Any:
        body = self._call("GET", path)
        return body.get("content") if isinstance(body, dict) else body

    # -- session ------------------------------------------------------------

    def check_session(self) -> Any:
        return self._call("GET", "/api/auth-rp/auth/user")

    def refresh(self) -> None:
        """Mint a fresh access token from the long-lived SSO cookie, and apply it.

        Measured, because none of this is guessable:
          * It is a GET. POST and PUT both return 405.
          * It returns {accessToken, expiresIn, tokenType} and sets NO cookie,
            so the caller must apply the token itself.
          * The API needs it in BOTH places. Measured on one session, seconds
            apart: Bearer header alone -> 401. Fresh token in the Access.V1
            cookie alone -> 401. Both together -> 200.

        Also the canary: if this fails the backup is not armed, and somebody
        needs to know before an outage discovers it for them.
        """
        try:
            r = self.s.get(f"{BASE}{REFRESH_PATH}", timeout=self.timeout)
        except requests.RequestException as e:
            raise StarlinkError(f"refresh-token failed: {e}") from e
        if r.status_code in (401, 403):
            raise SessionExpired(
                f"refresh-token rejected: HTTP {r.status_code} — the SSO cookie "
                "is dead, re-capture the session")
        try:
            r.raise_for_status()
            body = r.json() if r.content else {}
        except (requests.RequestException, ValueError) as e:
            raise StarlinkError(f"refresh-token failed (HTTP {r.status_code})") from e
        token = body.get("accessToken") or ""
        if not token:
            raise StarlinkError(
                f"refresh-token returned no accessToken (keys: {sorted(body)})")

        self._access_token = token
        self._token_type = body.get("tokenType") or "Bearer"
        expires_in = float(body.get("expiresIn") or 0)
        # Renew a minute early; a token that expires mid-resume is the worst
        # possible moment to find out.
        self._token_expires_at = time.time() + max(expires_in - 60, 0)
        self.s.cookies.set("Starlink.Com.Access.V1", token, domain=".starlink.com")

    def _ensure_token(self) -> None:
        if not self._access_token or time.time() >= self._token_expires_at:
            self.refresh()

    # -- reads (safe, no side effects) --------------------------------------

    def subscription(self, service_line: str | None = None) -> dict[str, Any]:
        content = self._content("/api/webagg/v2/accounts/service-lines")
        results = (content or {}).get("results") or []
        if not results:
            raise StarlinkError("no service lines on this account")
        line = results[0]
        if service_line is not None:
            line = next((r for r in results
                         if r.get("serviceLineNumber") == service_line), None)
            if line is None:
                raise StarlinkError(f"service line {service_line} not found on this account")
        sub = line.get("subscription") or {}
        address = line.get("serviceAddress") or {}
        return {
            "serviceLineNumber": line.get("serviceLineNumber"),
            "subscriptionReferenceId": sub.get("subscriptionReferenceId"),
            "nickname": line.get("nickname"),
            "addressReferenceId": address.get("addressReferenceId") or address.get("referenceId"),
            "productId": sub.get("productId"),
            "delayedProductId": sub.get("delayedProductId"),
            "isStandby": bool(sub.get("isStandby")),
            "isStandbyPending": bool(sub.get("isStandbyPending")),
            "isPaused": bool(sub.get("isPaused")),
            "canChangeService": bool(sub.get("canChangeService")),
        }

    def change_options(self, line: str) -> dict[str, Any]:
        """Available plans with their LIVE prorated price.

        `proratedPrice` is what leaving standby actually costs today, and it is
        a plain GET — this is the cost gate.
        """
        return self._content(
            f"/api/webagg/v1/public/subscriptions/change-options/{line}") or {}

    def plan_option(self, line: str, product_id: str) -> dict[str, Any]:
        """Exact live prorated cost of switching to ONE named plan.

        Preferred over resume_cost_range: naming the target plan turns the
        price from a bound into a number. `PUT .../resume` restores whatever
        plan preceded standby and will not say which, so it can only ever be
        gated on a worst case.
        """
        opts = self.change_options(line).get("changeOptions") or []
        for o in opts:
            p = o.get("productResponse") or {}
            if p.get("productId") != product_id:
                continue
            cost = p.get("proratedPrice")
            if "variants" in o:
                immediate = next((v for v in o.get("variants") or []
                                  if "effectiveTimestamp" in v
                                  and v["effectiveTimestamp"] is None), None)
                if immediate is None:
                    raise StarlinkError(f"{product_id} has no immediate change option")
                # The current plan's proratedPrice can be nonzero even when
                # keeping it (canceling queued standby) costs nothing. The
                # selected variant is what the account site actually charges.
                cost = immediate.get("oneTimeAmount")
            if not isinstance(cost, (int, float)):
                raise StarlinkError(
                    f"{product_id} has no numeric immediate charge ({cost!r}) — "
                    "refusing to price this blind")
            return {"productId": product_id, "name": p.get("name"),
                    "prorated": float(cost), "monthly": p.get("price")}
        offered = [(o.get("productResponse") or {}).get("productId") for o in opts]
        raise StarlinkError(
            f"target plan {product_id} is not offered on this line. "
            f"Available: {offered}")

    def resume_cost_range(self, line: str) -> dict[str, Any]:
        """Worst- and best-case prorated cost of leaving standby right now.

        `resume` restores the pre-standby plan and does not let us name it, so
        the ceiling is checked against the WORST case. Conservative on purpose:
        the alternative is discovering the price on the invoice.
        """
        opts = self.change_options(line).get("changeOptions") or []
        priced = []
        for o in opts:
            p = o.get("productResponse") or {}
            if p.get("isStandby"):
                continue
            cost = p.get("proratedPrice")
            if isinstance(cost, (int, float)):
                priced.append({"productId": p.get("productId"), "name": p.get("name"),
                               "prorated": float(cost), "monthly": p.get("price")})
        if not priced:
            raise StarlinkError("no priced non-standby options in change-options")
        priced.sort(key=lambda x: x["prorated"])
        return {"min": priced[0], "max": priced[-1], "all": priced}

    def address_has_capacity(self, address_reference_id: str) -> bool:
        """A standby line is not guaranteed a slot back in a congested cell."""
        return bool(self._content(
            f"/api/webagg/v1/shop/address-has-capacity/{address_reference_id}"))

    def demand_surcharge(self, line: str) -> Any:
        return self._content(f"/api/webagg/v1/demand-surcharge/{line}")

    # -- writes (money) -----------------------------------------------------

    def resume(self, line: str) -> Any:
        """LEAVES STANDBY AND BILLS THE PRORATED REMAINDER. No request body.

        Callers must have passed the cost ceiling first. Never call this from
        anywhere but Watchdog._resume().
        """
        return self._call(
            "PUT", f"/api/webagg/v1/public/subscriptions/line/{line}/resume",
            mutating=True)

    def change_product(self, line: str, product_id: str,
                       data: dict[str, Any] | None = None, *,
                       schedule: bool = False) -> Any:
        """Change a service line's plan using its subscription UUID.

        The account site's /line/{id}/product/... endpoint takes a
        subscriptionReferenceId, despite the path's name. schedule is a query
        parameter, not a JSON field. Upgrades are immediate by default.
        """
        reference = self.subscription(line).get("subscriptionReferenceId")
        if not reference:
            raise StarlinkError(f"service line {line} has no subscriptionReferenceId")
        return self._call(
            "POST",
            f"/api/webagg/v1/public/subscriptions/line/{reference}/product/{product_id}"
            f"/update?schedule={str(schedule).lower()}",
            mutating=True, json_body=data or {})

    def standby_option(self, line: str) -> dict[str, Any]:
        """Discover the standby product and timing offered to this line.

        The US product ID changed between August and October 2026. The
        isStandby flag is the source of truth, not a remembered product ID.
        Prefer the billing-boundary variant, as the account site does.
        """
        for option in self.change_options(line).get("changeOptions") or []:
            product = option.get("productResponse") or {}
            if product.get("isStandby") is not True or not product.get("productId"):
                continue
            variants = option.get("variants") or []
            if not variants or any("effectiveTimestamp" not in v for v in variants):
                raise StarlinkError("standby option has no usable timing variants")
            variant = next((v for v in variants if v["effectiveTimestamp"] is not None),
                           variants[0])
            return {"productId": product["productId"], "name": product.get("name"),
                    "monthly": product.get("price"),
                    "effectiveTimestamp": variant["effectiveTimestamp"],
                    "schedule": variant["effectiveTimestamp"] is not None}
        raise StarlinkError("no standby plan offered on this service line")

    def switch_to_product(self, line: str, product_id: str) -> dict[str, Any]:
        """Apply the named full plan and verify that no standby remains queued."""
        self.change_product(line, product_id, schedule=False)
        for attempt in range(6):
            after = self.subscription(line)
            if (after["productId"] == product_id and not after["isStandby"]
                    and not after["isStandbyPending"] and not after["isPaused"]):
                return after
            if attempt < 5:
                time.sleep(2)
        raise StarlinkError(
            f"plan switch was not confirmed for {line}: "
            f"product={after['productId']}, standby={after['isStandby']}, "
            f"pending={after['isStandbyPending']}, paused={after['isPaused']}")

    def back_to_standby(self, line: str) -> dict[str, Any]:
        """Request standby and return its verified current or pending state."""
        sub = self.subscription(line)
        if sub["isStandby"] or sub["isStandbyPending"]:
            return sub
        option = self.standby_option(line)
        self.change_product(line, option["productId"], schedule=option["schedule"])
        for attempt in range(6):
            after = self.subscription(line)
            if after["isStandby"] or after["isStandbyPending"]:
                return after
            if attempt < 5:
                time.sleep(2)
        raise StarlinkError(
            f"standby request was not confirmed for {line}: "
            f"product={after['productId']}, pending={after['isStandbyPending']}")
