#!/usr/bin/env python3
"""Verify email delivery before deploying, not after users can't sign up.

    RESEND_API_KEY=re_xxx RECUR_EMAIL_FROM='Recur <noreply@yourdomain>' \\
        python scripts/check_email.py you@wherever.com

The failure this exists to catch: Resend's free tier lets you send from
`onboarding@resend.dev` immediately, but ONLY to the address that owns the
Resend account. With open signup that means every user except you gets a
"check your email" screen and no email -- and the API reports success, because
from the app's side the send was accepted.

So this sends to an address you name and reports exactly what the provider
said, including the domain-verification error, which is the one that matters.
"""

from __future__ import annotations

import os
import sys

import httpx

from app import config


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    to = sys.argv[1]

    if not config.RESEND_API_KEY:
        sys.exit("RESEND_API_KEY is not set. Nothing would be sent.")
    if not config.RESEND_API_KEY.startswith("re_"):
        print("warning: a Resend key normally starts with 're_'", file=sys.stderr)

    print(f"from: {config.EMAIL_FROM}\nto:   {to}\n")

    r = httpx.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {config.RESEND_API_KEY}"},
        json={"from": config.EMAIL_FROM, "to": [to],
              "subject": "Recur delivery test",
              "text": "If you're reading this, verification emails will reach "
                      "your users.\n"},
        timeout=15,
    )

    if r.status_code < 300:
        print(f"sent  (id {r.json().get('id', '?')})")
        print("\nCheck the inbox. If it does not arrive, look at the Resend "
              "dashboard's Logs tab -- accepted is not the same as delivered.")
        return

    body = r.text
    print(f"REFUSED  (HTTP {r.status_code})\n{body}\n", file=sys.stderr)

    lowered = body.lower()
    if "domain is not verified" in lowered or "not verified" in lowered:
        print("The sending domain is not verified. Until it is, Resend will only\n"
              "deliver to the address that owns the Resend account -- so with open\n"
              "signup every other user gets nothing.\n\n"
              "Either verify a domain in the Resend dashboard, or deploy with\n"
              "RECUR_REGISTRATION_OPEN=0 until you have.", file=sys.stderr)
    elif r.status_code in (401, 403):
        print("The API key was rejected. Check it was copied whole -- Resend shows\n"
              "it once, at creation.", file=sys.stderr)
    elif r.status_code == 422:
        print("Check RECUR_EMAIL_FROM is a full address on a domain you control,\n"
              "e.g. 'Recur <noreply@yourdomain.com>'.", file=sys.stderr)
    raise SystemExit(1)


if __name__ == "__main__":
    main()
