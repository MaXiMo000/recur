"""Outbound email: verification and password reset.

With no provider key configured, links are written to the log instead of sent.
That is deliberate for local development and forbidden in production --
config.check() refuses to start a production process without RESEND_API_KEY,
because "the verification link is in the server log" is a way of saying anyone
with log access can take over any account.
"""

from __future__ import annotations

import logging

import httpx

from app import config

log = logging.getLogger("recur.mail")


def _send(to: str, subject: str, body: str) -> None:
    if not config.RESEND_API_KEY:
        log.warning("EMAIL NOT SENT (no provider configured)\n"
                    "  to: %s\n  subject: %s\n%s", to, subject, body)
        return
    try:
        r = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {config.RESEND_API_KEY}"},
            json={"from": config.EMAIL_FROM, "to": [to],
                  "subject": subject, "text": body},
            timeout=10,
        )
        r.raise_for_status()
    except Exception:
        # A provider outage must not turn into a 500 that tells the caller
        # whether the address exists.
        log.exception("failed to send %r to a user", subject)


def send_verification(to: str, token: str) -> None:
    link = f"{config.PUBLIC_URL}/verify?token={token}"
    _send(to, "Confirm your Recur account",
          f"Confirm your email address to finish setting up Recur:\n\n{link}\n\n"
          "The link is good for 24 hours. If you didn't sign up, ignore this "
          "message -- no account is active until it's used.\n")


def send_reset(to: str, token: str) -> None:
    link = f"{config.PUBLIC_URL}/reset?token={token}"
    _send(to, "Reset your Recur password",
          f"Use this link to set a new password:\n\n{link}\n\n"
          "It expires in an hour and can only be used once. If you didn't ask "
          "for it, nothing has changed on your account.\n")
