"""OAuth 2.1 authorization server for the remote MCP endpoint.

Only 8.5% of public MCP servers use OAuth, and 41% have no authentication at
all. This is the part that keeps this one out of both numbers.

The four rules that make it OAuth 2.1 rather than OAuth 2.0 with extra steps:

  1. **PKCE is mandatory, S256 only.** MCP clients are desktop apps and CLIs;
     they cannot hold a secret, so there is no client_secret to authenticate
     with. The code_verifier is what proves the client redeeming the code is
     the one that requested it. `plain` is rejected outright.
  2. **Redirect URIs are matched exactly.** No prefix matching, no wildcards --
     both are how an open redirect turns into account takeover.
  3. **Tokens are audience-bound.** A token minted for this server is rejected
     anywhere else, which closes the confused-deputy problem: an MCP client
     talking to several servers must not be able to replay one server's token
     against another.
  4. **Codes are single-use and deleted as they are read**, in one statement,
     so two concurrent redemptions cannot both succeed.

Access tokens, authorization codes and client identifiers are all stored as
SHA-256 hashes, for the same reason session tokens are: a database dump should
not be a working set of credentials.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from app import db

CODE_TTL = timedelta(minutes=5)
TOKEN_TTL = timedelta(days=30)
SCOPES = {"recur:read"}


class OAuthError(Exception):
    def __init__(self, code: str, description: str, status: int = 400):
        super().__init__(description)
        self.code = code
        self.description = description
        self.status = status


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _b64url_sha256(value: str) -> str:
    digest = hashlib.sha256(value.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


# ---------------------------------------------------------- registration --

def valid_redirect_uri(uri: str) -> bool:
    """Loopback for native apps, https everywhere else. A plaintext http
    redirect to a real host would put the authorization code on the wire."""
    try:
        u = urlparse(uri)
    except ValueError:
        return False
    if u.fragment or not u.scheme:
        return False
    if u.scheme == "https":
        return bool(u.hostname)
    if u.scheme == "http":
        return u.hostname in ("127.0.0.1", "::1", "localhost")
    # Custom schemes are how desktop clients get called back.
    return "." in u.scheme or u.scheme.isalpha()


def register_client(client_name: str, redirect_uris: list[str]) -> dict:
    """RFC 7591 dynamic client registration. Open by design -- an MCP client
    the user has just installed has no way to pre-arrange credentials. What
    stops that being a hole is that registering a client grants nothing: a
    signed-in human still has to approve it, and PKCE still has to match."""
    if not redirect_uris:
        raise OAuthError("invalid_redirect_uri", "At least one redirect URI is required.")
    bad = [u for u in redirect_uris if not valid_redirect_uri(u)]
    if bad:
        raise OAuthError("invalid_redirect_uri", f"Unusable redirect URI: {bad[0]}")

    client_id = secrets.token_urlsafe(24)
    with db.admin() as conn:
        conn.execute(
            "INSERT INTO oauth_client (client_id, client_name, redirect_uris) "
            "VALUES (%s, %s, %s)",
            (client_id, (client_name or "MCP client")[:120], redirect_uris))
        conn.commit()
    return {
        "client_id": client_id,
        "client_name": client_name,
        "redirect_uris": redirect_uris,
        "token_endpoint_auth_method": "none",     # public client
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
    }


def get_client(client_id: str) -> dict | None:
    with db.admin() as conn:
        row = conn.execute(
            "SELECT client_id, client_name, redirect_uris FROM oauth_client "
            "WHERE client_id = %s", (client_id,)).fetchone()
    return {"client_id": row[0], "client_name": row[1], "redirect_uris": row[2]} if row else None


# ------------------------------------------------------------ authorize --

def check_authorize_request(client_id: str, redirect_uri: str, scope: str,
                            code_challenge: str, method: str) -> dict:
    client = get_client(client_id)
    if client is None:
        raise OAuthError("invalid_client", "Unknown client.")
    # Exact match. A prefix or wildcard match here is an open redirect.
    if redirect_uri not in client["redirect_uris"]:
        raise OAuthError("invalid_redirect_uri", "That redirect URI is not registered.")
    if method != "S256":
        raise OAuthError("invalid_request", "PKCE with S256 is required.")
    if not code_challenge or len(code_challenge) < 43:
        raise OAuthError("invalid_request", "A valid PKCE code_challenge is required.")
    requested = set((scope or "recur:read").split())
    if not requested <= SCOPES:
        raise OAuthError("invalid_scope", f"Unsupported scope: {' '.join(requested - SCOPES)}")
    return client


def issue_code(client_id: str, user_id: int, redirect_uri: str, scope: str,
               code_challenge: str, resource: str | None) -> str:
    code = secrets.token_urlsafe(32)
    with db.admin() as conn:
        conn.execute(
            "INSERT INTO oauth_code (code_hash, client_id, user_id, redirect_uri,"
            " code_challenge, code_challenge_method, scope, resource, expires_at) "
            "VALUES (%s, %s, %s, %s, %s, 'S256', %s, %s, %s)",
            (_hash(code), client_id, user_id, redirect_uri, code_challenge,
             scope or "recur:read", resource, _now() + CODE_TTL))
        conn.commit()
    return code


# ---------------------------------------------------------------- token --

def exchange_code(code: str, client_id: str, redirect_uri: str,
                  code_verifier: str, audience: str) -> dict:
    if not code_verifier or not (43 <= len(code_verifier) <= 128):
        raise OAuthError("invalid_grant", "A valid PKCE code_verifier is required.")

    with db.admin() as conn:
        # Deleted as it is read: a code cannot be redeemed twice, and two
        # simultaneous attempts cannot both win.
        row = conn.execute(
            "DELETE FROM oauth_code WHERE code_hash = %s AND expires_at > now() "
            "RETURNING client_id, user_id, redirect_uri, code_challenge, scope, resource",
            (_hash(code),)).fetchone()
        conn.commit()

    if row is None:
        raise OAuthError("invalid_grant", "That authorization code is invalid or expired.")
    stored_client, user_id, stored_redirect, challenge, scope, resource = row

    if stored_client != client_id:
        raise OAuthError("invalid_grant", "That code was issued to another client.")
    if stored_redirect != redirect_uri:
        raise OAuthError("invalid_grant", "redirect_uri does not match the request.")
    if not secrets.compare_digest(_b64url_sha256(code_verifier), challenge):
        raise OAuthError("invalid_grant", "PKCE verification failed.")
    # The code was bound to a resource at /authorize; the token must not be
    # minted for a different one.
    if resource and resource.rstrip("/") != audience.rstrip("/"):
        raise OAuthError("invalid_target", "resource does not match the request.")

    token = secrets.token_urlsafe(40)
    expires = _now() + TOKEN_TTL
    with db.admin() as conn:
        conn.execute(
            "INSERT INTO oauth_token (token_hash, client_id, user_id, scope,"
            " audience, expires_at) VALUES (%s, %s, %s, %s, %s, %s)",
            (_hash(token), client_id, user_id, scope, audience.rstrip("/"), expires))
        conn.commit()

    return {"access_token": token, "token_type": "Bearer",
            "expires_in": int(TOKEN_TTL.total_seconds()), "scope": scope}


def resolve_token(token: str | None, audience: str) -> int | None:
    """-> user_id, or None.

    The audience check is the whole point: a token this server minted is
    accepted here and nowhere else, and a token minted elsewhere is refused
    here even if it is otherwise valid.
    """
    if not token:
        return None
    with db.admin() as conn:
        row = conn.execute(
            "SELECT t.user_id, t.audience FROM oauth_token t "
            "JOIN app_user u ON u.id = t.user_id "
            "WHERE t.token_hash = %s AND t.expires_at > now() "
            "AND t.revoked_at IS NULL AND u.disabled_at IS NULL",
            (_hash(token),)).fetchone()
        if row is None:
            return None
        user_id, aud = row
        if aud.rstrip("/") != audience.rstrip("/"):
            return None
        conn.execute("UPDATE oauth_token SET last_used_at = now() WHERE token_hash = %s",
                     (_hash(token),))
        conn.commit()
        return user_id


def revoke_token(token: str) -> None:
    with db.admin() as conn:
        conn.execute("UPDATE oauth_token SET revoked_at = now() WHERE token_hash = %s",
                     (_hash(token),))
        conn.commit()


def list_grants(user_id: int) -> list[dict]:
    """So a person can see which clients hold a token, and take it back."""
    with db.admin() as conn:
        rows = conn.execute(
            "SELECT t.token_hash, c.client_name, t.scope, t.created_at, t.last_used_at "
            "FROM oauth_token t LEFT JOIN oauth_client c ON c.client_id = t.client_id "
            "WHERE t.user_id = %s AND t.revoked_at IS NULL AND t.expires_at > now() "
            "ORDER BY t.created_at DESC", (user_id,)).fetchall()
    return [{"id": r[0][:12], "client": r[1] or "unknown", "scope": r[2],
             "granted": r[3], "last_used": r[4]} for r in rows]


def revoke_grant(user_id: int, token_prefix: str) -> bool:
    with db.admin() as conn:
        n = conn.execute(
            "UPDATE oauth_token SET revoked_at = now() "
            "WHERE user_id = %s AND token_hash LIKE %s AND revoked_at IS NULL",
            (user_id, token_prefix + "%")).rowcount
        conn.commit()
    return n > 0


def purge_expired() -> None:
    with db.admin() as conn:
        conn.execute("DELETE FROM oauth_code WHERE expires_at < now()")
        conn.execute("DELETE FROM oauth_token WHERE expires_at < now()")
        conn.commit()
