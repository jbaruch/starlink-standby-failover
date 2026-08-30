#!/usr/bin/env python3
"""The renewal alert fires once a year. It has to be right the first time.

Two failure modes, both silent:
  * Unbalanced Markdown → Telegram returns 400 and the alert never arrives.
  * A vague message → it arrives and nobody knows what to do, eleven months
    after the last time they did it.

    ./.venv/bin/python test_alerts.py
"""
from __future__ import annotations

from notify import renewal_instructions

results: list[bool] = []


def check(label: str, got, want) -> None:
    ok = got == want
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {label}: {got!r}" + ("" if ok else f" (want {want!r})"))


def balanced(text: str, ch: str) -> bool:
    """Telegram Markdown v1 needs paired delimiters OUTSIDE code spans."""
    outside, in_code = [], False
    for c in text:
        if c == "`":
            in_code = not in_code
        elif not in_code:
            outside.append(c)
    return "".join(outside).count(ch) % 2 == 0


def main() -> int:
    msg = renewal_instructions(331, "/srv/starlink")

    # --- Markdown must survive Telegram's parser ---
    check("bold markers balanced", balanced(msg, "*"), True)
    check("italic markers balanced", balanced(msg, "_"), True)
    check("code spans balanced", msg.count("`") % 2, 0)

    # --- it must actually tell you what to do ---
    check("says where to log in", "starlink.com/account" in msg, True)
    check("names the DevTools tab", "Network" in msg, True)
    check("names the exact browser action", "Copy as cURL" in msg, True)
    check("gives the install command", "install-session.sh" in msg, True)
    check("gives the restart command", "force-recreate" in msg, True)
    check("includes the deployment directory", "/srv/starlink" in msg, True)
    check("numbered steps present", all(f"{n}." in msg for n in range(1, 7)), True)

    # --- and it must not cause a panic ---
    check("says failover is unaffected", "failover is unaffected" in msg, True)

    # --- degrades sensibly when DEPLOY_DIR is unset ---
    bare = renewal_instructions(331)
    check("placeholder when no deploy dir", "<your deployment directory>" in bare, True)
    check("bare message still balanced", balanced(bare, "*") and balanced(bare, "_"), True)

    # A year is a long time; the age must be stated, not implied.
    check("states the age", "331 days" in msg, True)

    failed = results.count(False)
    print(f"\n{len(results) - failed}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
