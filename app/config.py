"""Settings, and the refusal to start when production settings are missing.

A development default that silently survives into production is how apps end up
with a known cookie secret or an open CORS policy. So every setting that is
only safe locally is checked at import time, and when RECUR_ENV=production the
process fails to start rather than running with it.
"""

from __future__ import annotations

import pathlib

import os
import sys

from dotenv import load_dotenv

# Local convenience only. Render injects real environment variables, and a .env
# is never committed -- so on a deployed instance this call finds nothing and
# changes nothing.
# .env lives at the repo root, one level above this package.
load_dotenv(pathlib.Path(__file__).resolve().parents[1] / ".env", override=False)

ENV = os.environ.get("RECUR_ENV", "development").lower()
IS_PROD = ENV == "production"

# Render sets this on every web service: the full public URL, scheme included.
# Its blueprint `property: host` is the *private* network hostname and carries
# no scheme, so it must not be used for either of these -- it would produce
# verification links like "recur/verify?token=..." and pass every check.
_RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
_DEV_ORIGINS = "http://localhost:5173,http://127.0.0.1:5173"

# Comma-separated list of origins allowed to call the API with credentials.
# There is no wildcard branch: '*' and cookies are mutually exclusive anyway,
# and a wildcard here would be an invitation.
ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get(
        "RECUR_ALLOWED_ORIGINS", _RENDER_URL or _DEV_ORIGINS
    ).split(",") if o.strip()
]

PUBLIC_URL = (
    os.environ.get("RECUR_PUBLIC_URL") or _RENDER_URL or "http://localhost:5173"
).rstrip("/")

# Cookies are httpOnly always; Secure and SameSite tighten in production.
COOKIE_NAME = "recur_session"
COOKIE_SECURE = IS_PROD
COOKIE_SAMESITE = os.environ.get("RECUR_COOKIE_SAMESITE", "lax")
COOKIE_DOMAIN = os.environ.get("RECUR_COOKIE_DOMAIN") or None

# Email. Without a provider key the app prints links to the log, which is fine
# locally and unacceptable in production -- so production requires the key.
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
EMAIL_FROM = os.environ.get("RECUR_EMAIL_FROM", "Recur <noreply@localhost>")

REGISTRATION_OPEN = os.environ.get("RECUR_REGISTRATION_OPEN", "1") != "0"

# Error tracking. Optional: with no DSN the SDK is never initialised, so
# development and CI stay offline.
SENTRY_DSN = os.environ.get("SENTRY_DSN", "")


def init_error_tracking() -> None:
    """Send stack traces somewhere a person will see them.

    send_default_pii stays off and the before_send hook drops the request body,
    because on this app a captured body is somebody's bank transactions and a
    captured header is their session cookie. An error tracker that quietly
    becomes a second copy of the data is worse than no error tracker.
    """
    if not SENTRY_DSN:
        return
    import sentry_sdk

    def scrub(event, hint):
        req = event.get("request", {})
        req.pop("data", None)
        req.pop("cookies", None)
        headers = req.get("headers", {})
        for h in ("Authorization", "Cookie", "authorization", "cookie"):
            headers.pop(h, None)
        return event

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        environment=ENV,
        send_default_pii=False,
        traces_sample_rate=float(os.environ.get("SENTRY_TRACES", "0.0")),
        before_send=scrub,
    )

# Rate limits: (max attempts, window seconds)
LIMITS = {
    "login": (8, 900),        # per email and per IP
    "register": (5, 3600),
    "reset": (5, 3600),
    "upload": (20, 3600),
}

_PROD_REQUIREMENTS = [
    ("RECUR_APP_PASSWORD", "the unprivileged database role would use a known password"),
    ("RESEND_API_KEY", "verification emails would be written to the log instead of sent"),
]


def check() -> None:
    """Called at startup. Loud and fatal beats quiet and insecure."""
    if not IS_PROD:
        return
    missing = [(k, why) for k, why in _PROD_REQUIREMENTS if not os.environ.get(k)]
    if missing:
        print("RECUR_ENV=production but required settings are missing:\n", file=sys.stderr)
        for k, why in missing:
            print(f"  {k}\n      without it, {why}", file=sys.stderr)
        raise SystemExit(1)
    # A scheme-less value is the failure this checks for, not a typo: Render's
    # `property: host` yields a bare hostname, which every string check here
    # would have passed while every emailed link stayed unclickable.
    if not PUBLIC_URL.startswith("https://"):
        raise SystemExit(
            f"RECUR_PUBLIC_URL must be a full https:// URL, got {PUBLIC_URL!r}. "
            "Leave it unset on Render and RENDER_EXTERNAL_URL is used."
        )
    if not ALLOWED_ORIGINS or not all(o.startswith("https://") for o in ALLOWED_ORIGINS):
        raise SystemExit(
            f"RECUR_ALLOWED_ORIGINS must be full https:// origins, got {ALLOWED_ORIGINS!r}."
        )
    if COOKIE_SAMESITE == "none" and not COOKIE_SECURE:
        raise SystemExit("SameSite=None requires Secure cookies.")
