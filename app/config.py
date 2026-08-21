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

# Comma-separated list of origins allowed to call the API with credentials.
# There is no wildcard branch: '*' and cookies are mutually exclusive anyway,
# and a wildcard here would be an invitation.
ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get(
        "RECUR_ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
    ).split(",") if o.strip()
]

PUBLIC_URL = os.environ.get("RECUR_PUBLIC_URL", "http://localhost:5173").rstrip("/")

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

# Rate limits: (max attempts, window seconds)
LIMITS = {
    "login": (8, 900),        # per email and per IP
    "register": (5, 3600),
    "reset": (5, 3600),
    "upload": (20, 3600),
}

_PROD_REQUIREMENTS = [
    ("RECUR_APP_PASSWORD", "the unprivileged database role would use a known password"),
    ("RECUR_ALLOWED_ORIGINS", "CORS would allow localhost, which is not your site"),
    ("RECUR_PUBLIC_URL", "verification links would point at localhost"),
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
    if any(o.startswith("http://") for o in ALLOWED_ORIGINS):
        raise SystemExit("RECUR_ALLOWED_ORIGINS contains a plaintext http:// origin.")
    if COOKIE_SAMESITE == "none" and not COOKIE_SECURE:
        raise SystemExit("SameSite=None requires Secure cookies.")
