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

STANDBY_PRODUCT_ID = "us-consumer-subscription-standby-mode-0526"
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
        r = self.s.request(method, f"{BASE}{path}", headers=self._headers(mutating),
                           json=json_body, timeout=self.timeout)

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
        r.raise_for_status()
        if not r.content:
            return {}
        body = r.json()
        # The webagg envelope reports failure in-band with HTTP 200 sometimes,
        # and with 422 otherwise. Either way, isValid=False must not look like
        # success to a caller about to spend money.
        if isinstance(body, dict) and body.get("isValid") is False:
            raise StarlinkError(f"{path} returned errors: {body.get('errors')}")
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
        r = self.s.get(f"{BASE}{REFRESH_PATH}", timeout=self.timeout)
        if r.status_code in (401, 403):
            raise SessionExpired(
                f"refresh-token rejected: HTTP {r.status_code} — the SSO cookie "
                "is dead, re-capture the session")
        r.raise_for_status()

        body = r.json() if r.content else {}
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

    def subscription(self) -> dict[str, Any]:
        content = self._content("/api/webagg/v2/accounts/service-lines")
        results = (content or {}).get("results") or []
        if not results:
            raise StarlinkError("no service lines on this account")
        line = results[0]
        sub = line.get("subscription") or {}
        address = line.get("serviceAddress") or {}
        return {
            "serviceLineNumber": line.get("serviceLineNumber"),
            "nickname": line.get("nickname"),
            "addressReferenceId": address.get("addressReferenceId") or address.get("referenceId"),
            "productId": sub.get("productId"),
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
            if not isinstance(cost, (int, float)):
                raise StarlinkError(
                    f"{product_id} has no numeric proratedPrice ({cost!r}) — "
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
                       data: dict[str, Any] | None = None) -> Any:
        return self._call(
            "POST",
            f"/api/webagg/v1/public/subscriptions/line/{line}/product/{product_id}/update",
            mutating=True, json_body=data or {})

    def back_to_standby(self, line: str) -> Any:
        return self.change_product(line, STANDBY_PRODUCT_ID)
