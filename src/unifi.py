"""UniFi gateway client — per-WAN state and outage events.

Everything here talks to the local console. No cloud, no internet dependency,
which matters because this runs precisely when the internet is broken.

Two things worth knowing before you read further:

  * UniFi's Alarm Manager has **no per-WAN and no failover trigger**. Its
    `internet_disconnected` alarm cannot be scoped to one WAN and fires for the
    backup link too. So detection here polls the gateway object rather than
    subscribing to alarms.
  * The only per-WAN outage signal UniFi exposes is the system-log query in
    `recent_wan_events()`, which carries the WAN name, ISP and duration.

Verified against UniFi OS 5.1.31 / Network 10.5.67 on a UDM SE. The gateway is
detected by shape (it is the device that has a `wan1`), not by model string, so
other gateways should work — please report what you find.
"""
from __future__ import annotations

import os
import pathlib
import time
from typing import Any

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

FAILOVER_EVENTS = [
    "NETWORK_WAN_FAILED",
    "NETWORK_WAN_FAILED_TEMPORARY",
    "NETWORK_WAN_FAILED_MULTIPLE_TIMES",
    "NETWORK_WAN_RESTORED",
    "NETWORK_FAILED_OVER_TO_BACKUP_WAN",
    "NETWORK_FAILED_OVER_TO_BACKUP_WAN_TEMPORARY",
]


class UniFiError(RuntimeError):
    """Anything that went wrong talking to the console.

    Every failure in this module surfaces as this type (or the subclass below),
    so a caller handles exactly one exception, not a zoo of requests errors.
    """


class UniFiUnavailable(UniFiError):
    """The console is not answering properly right now: unreachable, timing
    out, 5xx, or serving HTML where JSON belongs.

    Expected, not exotic. Firmware and Network-app updates restart the app
    behind the console's proxy, and for a few minutes every one of these
    shows up. On the author's console that was three separate windows in five
    weeks, and the watchdog used to exit on each one.
    """


def _snippet(r: requests.Response, limit: int = 120) -> str:
    """The start of a response body, whitespace-collapsed, for error messages."""
    return repr(" ".join(r.text[:limit * 2].split())[:limit])


def load_api_key() -> str:
    """API key from a mounted file, or an env var. Empty string if neither."""
    path = os.environ.get("UNIFI_API_KEY_FILE", "")
    if path and pathlib.Path(path).exists():
        return pathlib.Path(path).read_text().strip()
    return os.environ.get("UNIFI_API_KEY", "").strip()


class UniFi:
    """Talks to the local console with either an API key or a local admin.

    An API key is preferred: scoped, revocable, and no password stored anywhere.
    Create one at `https://<console>/network/default/integrations` — note that
    page has no sidebar icon and no menu entry on some versions, so you may have
    to navigate to the URL directly.

    Whether the *classic* endpoints (the only ones carrying per-WAN state)
    accept `X-API-KEY` is version-dependent, and UniFi OS returns an identical
    bare 401 for a bogus key and for no key, so it cannot be probed without a
    real one. Run `verify_unifi_auth.py` to settle it for your console; fall
    back to a read-only local admin if it says no.
    """

    def __init__(self, host: str, username: str = "", password: str = "",
                 api_key: str = "", site: str = "default",
                 verify_tls: bool = False, timeout: int = 15) -> None:
        self.base = f"https://{host}"
        self.site = site
        self.timeout = timeout
        self._username = username
        self._password = password
        self._api_key = api_key
        if not api_key and not (username and password):
            raise UniFiError("need UNIFI_API_KEY(_FILE) or UNIFI_USERNAME+UNIFI_PASSWORD")
        self.s = requests.Session()
        self.s.verify = verify_tls
        if api_key:
            self.s.headers["X-API-KEY"] = api_key

    @property
    def uses_api_key(self) -> bool:
        return bool(self._api_key)

    # -- auth ---------------------------------------------------------------

    def login(self) -> None:
        """No-op when using an API key — there is no session to establish."""
        if self._api_key:
            return
        path = "/api/auth/login"
        r = self._request("POST", path, json={"username": self._username,
                                              "password": self._password})
        if r.status_code >= 500:
            raise UniFiUnavailable(f"POST {path}: HTTP {r.status_code} {_snippet(r)}")
        if r.status_code != 200:
            raise UniFiError(f"UniFi login failed: HTTP {r.status_code} {r.text[:200]}")

    # -- transport ------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        """One HTTP round trip. A console that cannot be reached at all —
        refused, reset, timed out — becomes UniFiUnavailable here."""
        try:
            return self.s.request(method, f"{self.base}{path}",
                                  timeout=self.timeout, **kwargs)
        except requests.RequestException as e:
            raise UniFiUnavailable(f"{method} {path}: {type(e).__name__}: {e}") from e

    def _send(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        """A request with status handling: one re-login on 401 under local-admin
        auth, 5xx as UniFiUnavailable, any other 4xx as UniFiError."""
        r = self._request(method, path, **kwargs)
        if r.status_code == 401 and not self._api_key:
            self.login()
            r = self._request(method, path, **kwargs)
        if r.status_code == 401:
            if self._api_key:
                raise UniFiError(
                    f"401 on {path}. With an API key this usually means the "
                    "classic API does not accept X-API-KEY on this firmware — "
                    "run verify_unifi_auth.py, and use a local admin if it says no.")
            raise UniFiError(f"401 on {path} right after a successful login — "
                             "the local admin may lack access to this endpoint")
        if r.status_code >= 500:
            raise UniFiUnavailable(f"{method} {path}: HTTP {r.status_code} {_snippet(r)}")
        if r.status_code >= 400:
            raise UniFiError(f"{method} {path}: HTTP {r.status_code} {_snippet(r)}")
        return r

    @staticmethod
    def _json(r: requests.Response, what: str) -> Any:
        """Parse a body as JSON, or say exactly what came back instead.

        While the Network app boots, the console answers API paths with a 200
        and its own HTML page. raise_for_status() waves that through, and a bare
        r.json() dies with "Expecting value: line 1 column 1 (char 0)" — which
        names neither the endpoint nor the cause.
        """
        try:
            return r.json()
        except ValueError as e:  # requests' JSONDecodeError is a ValueError
            ctype = r.headers.get("Content-Type", "no Content-Type")
            raise UniFiUnavailable(
                f"{what}: HTTP {r.status_code} but the body is not JSON "
                f"({ctype}): {_snippet(r)}") from e

    def _csrf(self) -> str | None:
        """Any GET hands the CSRF token back in a response header.

        Returns None under API-key auth: CSRF defends cookie sessions, and there
        is no cookie session to defend.
        """
        if self._api_key:
            return None
        r = self._send("GET", "/api/users/self")
        token = r.headers.get("x-csrf-token")
        if not token:
            raise UniFiError("no x-csrf-token header on /api/users/self")
        return token

    def _get(self, path: str) -> Any:
        return self._json(self._send("GET", path), f"GET {path}")

    def _data(self, path: str) -> Any:
        """The `data` member that every classic endpoint wraps its payload in."""
        body = self._get(path)
        if not isinstance(body, dict) or "data" not in body:
            raise UniFiError(f"GET {path}: no 'data' in response: {str(body)[:120]!r}")
        return body["data"]

    # -- state --------------------------------------------------------------

    def _gateway(self) -> dict[str, Any]:
        """The gateway is whichever device exposes a `wan1`.

        Detected by shape rather than by model string, so this is not tied to
        one piece of hardware.
        """
        data = self._data(f"/proxy/network/api/s/{self.site}/stat/device")
        for d in data:
            if isinstance(d.get("wan1"), dict) and "up" in d["wan1"]:
                return d
        models = sorted({d.get("model", "?") for d in data})
        raise UniFiError(
            f"no gateway with a wan1 found in stat/device (saw models: {models}). "
            "Dual-WAN must be configured for this tool to have anything to watch.")

    def wan_state(self) -> dict[str, Any]:
        """Per-WAN up/down straight off the gateway object.

        `wan1` is the primary and `wan2` the failover, matching the console's
        own numbering and `wan_failover_priority` in the network config.
        """
        gw = self._gateway()
        wan1, wan2 = gw.get("wan1") or {}, gw.get("wan2") or {}
        if "up" not in wan1:
            raise UniFiError("gateway object has no wan1.up — API shape changed")
        if not wan2:
            raise UniFiError(
                "gateway has no wan2 — no backup WAN is configured, so there is "
                "nothing to fail over to")
        return {
            "wan1_up": bool(wan1.get("up")),
            "wan2_up": bool(wan2.get("up")),
            "wan1_ip": wan1.get("ip"),
            "wan2_ip": wan2.get("ip"),
            "at": time.time(),
        }

    def wan_names(self) -> dict[str, str]:
        """The names you gave your WANs in the console, for readable alerts.

        Raises UniFiError when the console will not say. Deciding that cosmetic
        data is not worth failing over is the caller's job, and only the caller
        knows which names it already has: quietly returning WAN1/WAN2 here would
        overwrite good names with placeholders after every console blip.
        """
        names = {"wan1": "WAN1", "wan2": "WAN2"}
        data = self._data(f"/proxy/network/api/s/{self.site}/rest/networkconf")
        for n in data:
            if n.get("purpose") != "wan":
                continue
            group = (n.get("wan_networkgroup") or "").upper()
            if group == "WAN" and n.get("name"):
                names["wan1"] = n["name"]
            elif group == "WAN2" and n.get("name"):
                names["wan2"] = n["name"]
        return names

    def isp(self) -> dict[str, Any]:
        """Which ISP the gateway currently considers upstream."""
        data = self._data(f"/proxy/network/api/s/{self.site}/stat/health")
        wan = next((d for d in data if d.get("subsystem") == "wan"), None)
        if wan is None:
            raise UniFiError("no 'wan' subsystem in stat/health")
        return {"isp": wan.get("isp_name"), "asn": wan.get("asn"),
                "ip": wan.get("wan_ip"), "status": wan.get("status")}

    def recent_wan_events(self, since_seconds: int = 3600) -> list[dict[str, Any]]:
        """INTERNET_OUTAGE_AND_FAILOVER log entries, with the WAN identity attached.

        This is the only per-WAN signal UniFi exposes. Alarm Manager triggers
        cannot be scoped to a single WAN, so an alarm fires for backup-link
        hiccups too — on the author's network, 13 backup events to 2 primary
        ones in the same window. Use this, not alarms, to attribute an outage.
        """
        now_ms = int(time.time() * 1000)
        body = {
            "searchText": "",
            "severities": ["LOW", "MEDIUM", "HIGH", "VERY_HIGH"],
            "timestampFrom": now_ms - since_seconds * 1000,
            "timestampTo": now_ms,
            "subcategories": ["INTERNET_OUTAGE_AND_FAILOVER"],
            "categories": ["INTERNET_AND_WAN"],
            "type": "GENERAL",
            "pageNumber": 0,
            "pageSize": 100,
            "events": FAILOVER_EVENTS,
            "adminIds": [],
            "clientDeviceMacs": [],
        }
        headers = {}
        csrf = self._csrf()
        if csrf:
            headers["X-CSRF-Token"] = csrf
        path = f"/proxy/network/v2/api/site/{self.site}/system-log/all"
        r = self._send("POST", path, json=body, headers=headers)
        out = []
        for e in self._json(r, f"POST {path}").get("data", []):
            p = e.get("parameters", {})
            out.append({
                "at": e.get("timestamp", 0) / 1000,
                "event": e.get("event"),
                "wan": (p.get("WAN_NAME") or {}).get("name"),
                "wan_id": (p.get("WAN_ID") or {}).get("id"),
                "isp": (p.get("ISP_NAME") or {}).get("name"),
                "duration": (p.get("DURATION") or {}).get("name"),
            })
        return out
