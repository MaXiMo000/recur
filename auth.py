"""Passwords, sessions, and the tokens that go in email.

Design rules, each of which exists because the obvious version is wrong:

  - Passwords are argon2id. Never stored, never logged, never in a URL.
  - Session tokens are opaque random strings; only their SHA-256 is stored, so
    a database dump does not hand the reader a set of live sessions. Not JWTs:
    a JWT cannot be revoked before it expires, and "log out everywhere" and
    "disable this account now" are features, not edge cases.
  - Login answers identically whether the email is unknown or the password is
    wrong, and spends the same time either way. A faster "no" for unknown
    addresses is an account-enumeration oracle.
  - Email tokens are single-use and hashed at rest, same as sessions.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

import db

_ph = PasswordHasher()

SESSION_TTL = timedelta(days=int(os.environ.get("RECUR_SESSION_DAYS", "14")))
VERIFY_TTL = timedelta(hours=24)
RESET_TTL = timedelta(hours=1)

MIN_PASSWORD = 12          # length beats mandated punctuation
MAX_PASSWORD = 1024        # argon2 on a 10MB password is a free DoS

# Burned on a miss so that "no such user" costs the same as "wrong password".
_DUMMY_HASH = _ph.hash("this hash exists only to be compared against")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    """32 bytes from the OS CSPRNG. Not uuid4, not random.choice."""
    return secrets.token_urlsafe(32)


# ------------------------------------------------------------------ users --

class AuthError(Exception):
    """Deliberately vague. The message reaches the user, so it must not reveal
    which half of the credential was wrong."""


def validate_password(password: str) -> None:
    if not MIN_PASSWORD <= len(password) <= MAX_PASSWORD:
        raise AuthError(f"Password must be at least {MIN_PASSWORD} characters.")


def register(email: str, password: str) -> tuple[int, str]:
    """-> (user_id, verification_token). Raises AuthError on a bad password.

    An email already in use does NOT raise: it returns the existing id with a
    fresh verification token, and the caller sends the same "check your email"
    response either way. Telling an anonymous caller which addresses have
    accounts is a disclosure, and on a finance app a meaningful one.
    """
    validate_password(password)
    email = email.strip().lower()
    with db.admin() as conn:
        row = conn.execute(
            "INSERT INTO app_user (email, password_hash) VALUES (%s, %s) "
            "ON CONFLICT (email) DO NOTHING RETURNING id",
            (email, _ph.hash(password)),
        ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT id FROM app_user WHERE email = %s", (email,)).fetchone()
            conn.commit()
            if row is None:
                raise AuthError("Could not create the account.")
            return row[0], issue_email_token(row[0], "verify")
        conn.commit()
    return row[0], issue_email_token(row[0], "verify")


def authenticate(email: str, password: str) -> int:
    """-> user_id. Raises AuthError with one message for every failure mode."""
    email = (email or "").strip().lower()
    with db.admin() as conn:
        row = conn.execute(
            "SELECT id, password_hash, email_verified_at, disabled_at "
            "FROM app_user WHERE email = %s", (email,)).fetchone()

        if row is None:
            # Same work, same latency, same answer as a wrong password.
            try:
                _ph.verify(_DUMMY_HASH, password)
            except Exception:
                pass
            raise AuthError("Email or password is incorrect.")

        user_id, stored, verified_at, disabled_at = row
        try:
            _ph.verify(stored, password)
        except (VerifyMismatchError, InvalidHashError, Exception):
            raise AuthError("Email or password is incorrect.")

        if _ph.check_needs_rehash(stored):
            conn.execute("UPDATE app_user SET password_hash = %s WHERE id = %s",
                         (_ph.hash(password), user_id))
            conn.commit()

    if disabled_at is not None:
        raise AuthError("This account is disabled.")
    if verified_at is None:
        raise AuthError("Confirm your email address before signing in.")
    return user_id


def set_password(user_id: int, password: str) -> None:
    validate_password(password)
    with db.admin() as conn:
        conn.execute("UPDATE app_user SET password_hash = %s WHERE id = %s",
                     (_ph.hash(password), user_id))
        # Changing a password ends every existing session. If the reason for the
        # change was a compromise, leaving the attacker's session alive defeats
        # the point of changing it.
        conn.execute("DELETE FROM session WHERE user_id = %s", (user_id,))
        conn.commit()


def delete_user(user_id: int) -> None:
    """GDPR erasure. Every tenant table cascades from app_user, so this is the
    whole deletion -- there is no second place the data lives."""
    with db.admin() as conn:
        conn.execute("DELETE FROM app_user WHERE id = %s", (user_id,))
        conn.commit()


# --------------------------------------------------------------- sessions --

def start_session(user_id: int, user_agent: str | None = None) -> tuple[str, datetime]:
    token = new_token()
    expires = _now() + SESSION_TTL
    with db.admin() as conn:
        conn.execute(
            "INSERT INTO session (token_hash, user_id, expires_at, user_agent) "
            "VALUES (%s, %s, %s, %s)",
            (_hash_token(token), user_id, expires, (user_agent or "")[:200]))
        conn.commit()
    return token, expires


def resolve_session(token: str | None) -> int | None:
    """-> user_id, or None. Expired and disabled accounts resolve to None."""
    if not token:
        return None
    with db.admin() as conn:
        row = conn.execute(
            "SELECT s.user_id FROM session s JOIN app_user u ON u.id = s.user_id "
            "WHERE s.token_hash = %s AND s.expires_at > now() "
            "AND u.disabled_at IS NULL AND u.email_verified_at IS NOT NULL",
            (_hash_token(token),)).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE session SET last_seen_at = now() WHERE token_hash = %s",
                     (_hash_token(token),))
        conn.commit()
        return row[0]


def end_session(token: str | None) -> None:
    if not token:
        return
    with db.admin() as conn:
        conn.execute("DELETE FROM session WHERE token_hash = %s", (_hash_token(token),))
        conn.commit()


def purge_expired() -> int:
    """Expired sessions and used tokens are data with no purpose. Keeping them
    is retention without a reason."""
    with db.admin() as conn:
        n = conn.execute("DELETE FROM session WHERE expires_at < now()").rowcount
        conn.execute("DELETE FROM email_token WHERE expires_at < now()")
        conn.commit()
        return n


# ---------------------------------------------------------- email tokens --

def issue_email_token(user_id: int, purpose: str) -> str:
    ttl = VERIFY_TTL if purpose == "verify" else RESET_TTL
    token = new_token()
    with db.admin() as conn:
        # One live token per purpose: issuing a new reset link invalidates the
        # previous one, so an old email forwarded on is inert.
        conn.execute("DELETE FROM email_token WHERE user_id = %s AND purpose = %s",
                     (user_id, purpose))
        conn.execute(
            "INSERT INTO email_token (token_hash, user_id, purpose, expires_at) "
            "VALUES (%s, %s, %s, %s)",
            (_hash_token(token), user_id, purpose, _now() + ttl))
        conn.commit()
    return token


def consume_email_token(token: str, purpose: str) -> int:
    """-> user_id. Single use: the row is deleted as it is read, in one
    statement, so two simultaneous requests cannot both succeed."""
    with db.admin() as conn:
        row = conn.execute(
            "DELETE FROM email_token WHERE token_hash = %s AND purpose = %s "
            "AND expires_at > now() AND used_at IS NULL RETURNING user_id",
            (_hash_token(token), purpose)).fetchone()
        if row is None:
            conn.commit()
            raise AuthError("That link is invalid or has expired.")
        if purpose == "verify":
            conn.execute(
                "UPDATE app_user SET email_verified_at = coalesce(email_verified_at, now()) "
                "WHERE id = %s", (row[0],))
        conn.commit()
        return row[0]


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())
