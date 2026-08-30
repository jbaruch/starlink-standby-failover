"""Telegram alerts, and a human approval path that does not need a bot.

Approval is a one-tap URL rather than an inline keyboard button, and that is
deliberate.

Telegram allows exactly **one** `getUpdates` consumer per bot token. If you
point this at a token some other bot already long-polls, the second consumer
gets HTTP 409 Conflict and one of the two stops working. Plenty of people
already run a home-automation bot and would naturally reuse its token here, so
this is a real trap rather than a hypothetical one.

`sendMessage` has no such exclusivity. So *pushing* alerts through an existing
bot is free and safe; only *receiving* collides. Hence: alerts go out through
whatever token you configure, and approvals come back over an HTTP link this
process serves itself.

If you would rather have real buttons, add a command to the bot that already
owns the token and have it call this process's HTTP API — that keeps Telegram
handling in one place. The URL flow works with no bot changes at all.
"""
from __future__ import annotations

import json
import logging
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Literal

import requests

log = logging.getLogger("notify")

TELEGRAM_API = "https://api.telegram.org"
Decision = Literal["approved", "denied", "pending", "expired"]


def renewal_instructions(age_days: float, deploy_dir: str = "") -> str:
    """The whole renewal procedure, in the alert itself.

    This fires roughly once a year. Nobody remembers a procedure they last ran
    eleven months ago, and "re-capture the session" is not a procedure — so the
    message carries every step, including where the deployment lives.
    """
    where = deploy_dir or "<your deployment directory>"
    return (
        f"🔑 *Starlink session needs renewing*\n"
        f"Captured {age_days:.0f} days ago; the cookie lasts about a year.\n\n"
        f"*Takes ~2 minutes:*\n"
        f"1. Log in at starlink.com/account in a browser\n"
        f"2. Open DevTools → *Network* tab, type `api/` in the filter\n"
        f"3. Reload the page, then right-click any `/api/webagg/...` row\n"
        f"4. Choose *Copy → Copy as cURL*\n"
        f"5. Run: `cd {where} && ./scripts/install-session.sh`\n"
        f"6. Restart: `docker compose up -d --force-recreate`\n\n"
        f"The script reads your clipboard, checks the session is complete and "
        f"writes it over ssh — nothing lands in your shell history.\n\n"
        f"_No rush: failover is unaffected. A dead session only means an outage "
        f"leaves you on standby at ~0.5 Mbps instead of upgrading._"
    )


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, timeout: int = 20) -> None:
        if not bot_token or not chat_id:
            raise ValueError("BOT_TOKEN and CHAT_ID are required")
        self._token = bot_token
        self._chat_id = chat_id
        self.timeout = timeout

    def send(self, text: str, disable_preview: bool = True) -> None:
        """Fire-and-log. A failed notification must never crash the watchdog —
        but it must never be silent either."""
        r = requests.post(
            f"{TELEGRAM_API}/bot{self._token}/sendMessage",
            json={
                "chat_id": self._chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": disable_preview,
            },
            timeout=self.timeout,
        )
        if r.status_code != 200:
            log.error("telegram sendMessage failed: HTTP %s %s",
                      r.status_code, r.text[:300])
            return
        log.info("telegram: sent (%d chars)", len(text))


class ApprovalServer:
    """Single-use, expiring approval links. LAN/Tailscale only."""

    def __init__(self, port: int, base_url: str, ttl_seconds: int = 1800) -> None:
        self.port = port
        self.base_url = base_url.rstrip("/")
        self.ttl = ttl_seconds
        self._lock = threading.Lock()
        self._requests: dict[str, dict[str, Any]] = {}
        self._httpd: ThreadingHTTPServer | None = None

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                path = urllib.parse.urlparse(self.path).path
                parts = [p for p in path.split("/") if p]
                if len(parts) == 2 and parts[0] in ("approve", "deny"):
                    ok, msg = outer._decide(parts[1], parts[0])
                    body = _page(msg, ok)
                    self.send_response(200 if ok else 410)
                elif path == "/healthz":
                    body = b'{"ok":true}'
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                else:
                    body = _page("Not found.", False)
                    self.send_response(404)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

        self._httpd = ThreadingHTTPServer(("0.0.0.0", self.port), Handler)
        t = threading.Thread(target=self._httpd.serve_forever, daemon=True,
                             name="approval-server")
        t.start()
        log.info("approval server listening on :%d (base %s)", self.port, self.base_url)

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()

    # -- api ----------------------------------------------------------------

    def create(self, context: dict[str, Any]) -> tuple[str, str, str]:
        """Returns (request_id, approve_url, deny_url)."""
        rid = secrets.token_urlsafe(24)
        with self._lock:
            self._requests[rid] = {
                "created": time.time(),
                "decision": "pending",
                "context": context,
            }
        return rid, f"{self.base_url}/approve/{rid}", f"{self.base_url}/deny/{rid}"

    def poll(self, rid: str) -> Decision:
        with self._lock:
            req = self._requests.get(rid)
            if req is None:
                return "expired"
            if req["decision"] == "pending" and time.time() - req["created"] > self.ttl:
                req["decision"] = "expired"
            return req["decision"]  # type: ignore[return-value]

    def _decide(self, rid: str, action: str) -> tuple[bool, str]:
        with self._lock:
            req = self._requests.get(rid)
            if req is None:
                return False, "Unknown or already-used link."
            if req["decision"] != "pending":
                return False, f"Already {req['decision']}."
            if time.time() - req["created"] > self.ttl:
                req["decision"] = "expired"
                return False, "This request expired."
            req["decision"] = "approved" if action == "approve" else "denied"
            ctx = json.dumps(req["context"], default=str)
        log.warning("approval %s via link: %s", req["decision"], ctx)
        verb = "Resuming Starlink now." if action == "approve" else "Left on standby."
        return True, verb


def _page(message: str, ok: bool) -> bytes:
    colour = "#0a7" if ok else "#a33"
    return (
        "<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
        "<body style='font:16px/1.5 -apple-system,system-ui,sans-serif;"
        "display:flex;align-items:center;justify-content:center;height:90vh;margin:0'>"
        f"<p style='color:{colour};font-weight:600;text-align:center;padding:1rem'>"
        f"{message}</p></body>"
    ).encode()
