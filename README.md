# starlink-standby-failover

**Your primary ISP drops. Starlink is sitting in Standby Mode at ~0.5 Mbps.
This switches it to a real plan, automatically, then puts it back before your
next bill.**

Starlink's Standby Mode costs about $10/month and keeps the dish online at
roughly 0.3–0.6 Mbps — enough for alarms, locks and messaging, not enough to
work. That makes it a good, cheap backup link *if* something upgrades it when
the primary actually fails, and downgrades it again afterwards. That is all this
does.

To be clear about what this is **not**: your gateway already fails over on its
own, and a standby line already carries traffic. Nothing here keeps you online.
This only decides whether being online means 0.5 Mbps or 100 Mbps.

Currently supports **UniFi** gateways for detection. The Starlink half is
independent of your router, so adding another platform means implementing one
class with `wan_state()`.

---

## Why this needed reverse engineering

Starlink has an official API, but it is enterprise-only — consumer accounts
cannot get credentials. So the plan-change endpoints here were mapped by reading
the account site's own JavaScript and confirming each call against a live
account. **This is unofficial and unversioned. SpaceX can move it without
notice**, which is why there is a canary that tells you when it breaks.

Everything below was measured, not guessed. If you are here because you are
trying to automate Starlink yourself, this section is probably the useful part.

### The account API

Base is `https://www.starlink.com/api/...` — the same-origin paths the account
SPA itself uses.

```
read   GET /webagg/v2/accounts/service-lines
         → subscription.isStandby, productId, serviceLineNumber
       GET /webagg/v1/public/subscriptions/change-options/{line}
         → currentProduct + changeOptions[].productResponse.proratedPrice
           ← the live cost gate, and it is a plain GET
       GET /webagg/v1/shop/address-has-capacity/{addressRefId}
       GET /webagg/v1/demand-surcharge/{line}

write  POST /webagg/v1/public/subscriptions/line/{line}/product/{productId}/update
         → change plan, including back to standby
       PUT  /webagg/v1/public/subscriptions/line/{line}/resume
         → leaves standby, no body, restores the PREVIOUS plan (unnamed, so its
           price cannot be known in advance — this tool uses /update instead)
```

### Auth, which is where the time goes

```
1. Capture the `cookie:` request header from a logged-in browser (DevTools →
   Network → any /api/ request → Copy as cURL). The auth cookies are HttpOnly,
   so page JS cannot read them — but browser automation can, so this step is
   manual by choice rather than by necessity (see "Credential lifetimes"). 2SV
   is mandatory and cannot be disabled, but it only challenges at sign-in.

2. Seed a cookie jar from it. Do NOT send a captured `cookie` header verbatim:
   the token inside `Starlink.Com.Access.V1` lasts 15 minutes, so a frozen
   header re-sends a dead token forever. Measured, same session, seconds apart:
   frozen header → 401, cookie jar → 200. (The cookie *container* carries a
   one-year expiry — do not confuse the two, as this project did for a while.)

3. Refresh with GET /api/auth/auth/refresh-token   (POST and PUT return 405)
   It returns {accessToken, expiresIn, tokenType} and sets NO cookie, so you
   must apply the token yourself — and to BOTH places:

       Authorization: Bearer <token>          alone → 401
       cookie Starlink.Com.Access.V1=<token>  alone → 401
       both together                                → 200

4. Do not call refresh twice in quick succession; the second returns 401 and
   looks exactly like a dead session. (This cost the author a false alarm.)
```

Some accounts have no `XSRF-TOKEN` at all, so the CSRF header is sent only when
present.

### Billing, so you can predict the cost

You are charged the **difference** between standby and the target plan, prorated
by days remaining. On the author's account this matched all five offered options
to the cent:

```
prorated = (new monthly - standby monthly) x days_remaining / days_in_cycle
```

So a $55/month plan against $10 standby costs at most ~$45 — on day one of a
cycle — and a couple of dollars near the end. `recon.py` prints the live numbers
for your line. Your cycle probably does not reset on the 1st; check your payment
history.

### What UniFi will and will not tell you

- Alarm Manager has **no per-WAN and no failover trigger**. `internet_disconnected`
  cannot be scoped to one WAN and fires for your *backup* link's hiccups too. On
  the author's network that was 13 backup events to 2 primary ones. Do not build
  a trigger on it.
- Detection therefore polls `wan1.up` from `stat/device`.
- The only per-WAN outage record is
  `POST /proxy/network/v2/api/site/{site}/system-log/all` filtered to
  `INTERNET_OUTAGE_AND_FAILOVER`, which carries `WAN_NAME`, `ISP_NAME` and
  `DURATION`.
- An **API key** works on these classic endpoints (UniFi OS 5.1.31), so no admin
  password is needed. Create one at
  `https://<console>/network/default/integrations` — a page with no sidebar icon
  and no menu entry, reachable only by URL. Whether your firmware accepts it is
  version-dependent, and a bogus key and no key return an identical bare 401, so
  it cannot be probed: run `verify_unifi_auth.py`.

---

## Install

Requires a dual-WAN UniFi gateway with Starlink as the secondary, and somewhere
always-on to run a container.

```bash
git clone https://github.com/OWNER/starlink-standby-failover
cd starlink-standby-failover
git config core.hooksPath .githooks     # blocks committing credentials

cp .env.example .env && $EDITOR .env
mkdir -p secrets && chmod 700 secrets
```

Credentials are **files**, never environment variables — a captured Starlink
cookie contains `$`, which docker compose interpolates inside `env_file` values
and silently mangles.

```bash
./scripts/install-unifi-key.sh    # UniFi API key   → secrets/unifi-api-key
./scripts/install-session.sh      # Starlink cookie → secrets/starlink-session
```

Both read your clipboard and write over ssh stdin, so nothing lands in your
shell history. They also work locally — see `NAS_HOST`/`NAS_DIR` at the top.

Build as the user that owns `secrets/` (bind mounts keep host ownership, and
those files are `0600`):

```bash
docker compose build --build-arg APP_UID=$(id -u) --build-arg APP_GID=$(id -g)
```

Then, in order:

```bash
docker compose run --rm starlink-standby-failover python verify_unifi_auth.py
docker compose run --rm starlink-standby-failover python recon.py   # read-only
# put a TARGET_PRODUCT_ID from recon output into .env
docker compose up -d
```

Leave `DRY_RUN=true` until you have watched it decide correctly at least once.

## Operating

```bash
docker compose run --rm starlink-standby-failover python canary.py
docker compose run --rm starlink-standby-failover python revert_to_standby.py
docker exec starlink-standby-failover touch /data/DISABLED   # stand down
```

The canary also runs in-process every `CANARY_INTERVAL_HOURS` and alerts
Telegram when the chain breaks. No cron required — one fewer thing to install,
and some NAS platforms make installing a crontab awkward for unprivileged
users even where cron itself runs fine.

## Credential lifetimes

| Credential | Lifetime | Renewal |
|---|---|---|
| Starlink access token | 15 minutes | Automatic |
| Starlink `Starlink.Com.Sso` cookie | **~1 year** | Manual, once a year |
| UniFi API key | whatever you set at creation | Manual; check the Integrations page |

The one-year figure is **measured**, read out of a browser profile: captured
2026-08-29, expires 2027-08-29. Several write-ups (and earlier versions of this
README) repeat a "~15 day" figure that does not match observation — if you are
building against this API, check it yourself rather than trusting either of us.

`refresh-token` mints the 15-minute access tokens from the SSO cookie and
returns no `Set-Cookie` for it, so nothing extends the cookie — but at a year,
re-capturing by hand annually is a fair trade rather than a wart. `SESSION_WARN_DAYS`
(default 330) tells you a month ahead.

A server-side invalidation can still end a session early — password change,
logout, a security event. The daily canary catches that and alerts Telegram.

Could renewal be automated? Yes, though it is not done here. HttpOnly does not
stop browser automation (`context.cookies()` returns HttpOnly cookies; verified),
and 2SV challenges at sign-in from a new browser rather than on a schedule, so a
headless browser with a persistent profile would stay logged in. At a one-year
cookie life the payoff is small against a ~400MB Chromium and a profile
directory to protect, which is why this asks you for two minutes once a year
instead. PRs welcome if your situation differs.

**Failover is never affected by any of this.** A dead session means an outage
leaves you on throttled standby instead of upgrading — slow internet, not no
internet.

Re-capture with `./scripts/install-session.sh` and restart the container.

## Going back to standby

Set `REVERT_DAY_OF_MONTH` and `BILLING_RESET_DAY` and the line is returned to
Standby Mode before your bill renews. It is attempted **every day in that
window**, not on one fixed day: a switch triggered after a single trigger day
would never be reverted and would cost a full month at plan rate. It retries
daily until standby is confirmed, and will not yank the plan while your primary
is still down.

Note the money only flows one way — the prorated remainder you paid is not
refundable, so reverting early donates it. Revert late, but not so late that a
queued standby misses the boundary.

## Proving the write path before you need it

Every read is verified on startup, but the calls that spend money are only
exercised when an outage happens. Finding out then that they 403 is the worst
possible time:

```bash
docker compose run --rm -e CONFIRM_SPEND=yes \
  starlink-standby-failover python test_write_path.py
```

It switches, verifies, switches back, and reports whether the revert applied
immediately or queued for the next billing boundary — which determines how much
slack `REVERT_DAY_OF_MONTH` needs. Costs a few dollars if you run it late in a
cycle. It refuses to spend without `CONFIRM_SPEND=yes`.

## A note on guards

It is tempting to wrap something that spends money in safety checks. Resist it.
A backup link that refuses to engage because a guard fired is worse than no
automation, because you believed you had one.

Exactly two things stop a switch here: `MAX_SPEND_USD` (a tripwire for absurd
values, not a budget) and Starlink itself refusing. The cell-capacity check is
*reported and ignored* — its endpoint belongs to the signup flow, its false path
is untested, and a guard built on an untested assumption can only ever stop a
switch you wanted.

## Tests

```bash
python -m venv .venv && ./.venv/bin/pip install -r requirements.txt
cd tests && PYTHONPATH=../src ../.venv/bin/python test_parsing.py
```

The fixtures are real captured API responses, trimmed and anonymised. If UniFi
or Starlink rename a field, they fail — which is the point. A watchdog that
silently stops understanding its inputs is worse than no watchdog.

## Contributing

Especially useful: other UniFi hardware and firmware versions, non-US Starlink
plan structures, and other router platforms. If an endpoint has moved, a PR with
the new shape and how you confirmed it is worth more than a bug report.

## License

Apache-2.0. Not affiliated with SpaceX, Starlink or Ubiquiti.
