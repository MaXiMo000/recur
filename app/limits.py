"""Attempt counting, shared by both routers.

This lived in `api.py`, which `mcp_http` cannot import — `api` imports
`mcp_http` to mount it, so the arrow only points one way. The result was that
every unauthenticated endpoint in `api.py` was rate limited and none of the
OAuth ones were, including `/oauth/register`, which is unauthenticated *by
design* and writes a row per call.

Not `auth.py`: that module is deliberately free of the web layer, and the only
thing it would gain here is an HTTPException import.
"""

from __future__ import annotations

from typing import Protocol

from fastapi import HTTPException

from app import config
from app import db


class _HasClient(Protocol):
    headers: object
    client: object


def client_ip(request: _HasClient) -> str:
    # Render terminates TLS and forwards; the leftmost entry is the client.
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() if fwd
            else (request.client.host if request.client else "unknown"))


def rate_limit(kind: str, key: str) -> None:
    """Counted in Postgres rather than in memory, because an in-process counter
    resets on every deploy and is per-instance -- two instances would double
    every limit, and a restart would clear a brute-force in progress."""
    limit, window = config.LIMITS[kind]
    with db.admin() as conn:
        n = conn.execute(
            "SELECT count(*) FROM auth_attempt WHERE key = %s AND kind = %s "
            "AND at > now() - make_interval(secs => %s)",
            (key, kind, window)).fetchone()[0]
        if n >= limit:
            conn.commit()
            raise HTTPException(429, "Too many attempts. Try again later.")
        conn.execute("INSERT INTO auth_attempt (key, kind) VALUES (%s, %s)",
                     (key, kind))
        conn.commit()


def clear_attempts(kind: str, key: str) -> None:
    with db.admin() as conn:
        conn.execute("DELETE FROM auth_attempt WHERE key = %s AND kind = %s",
                     (key, kind))
        conn.commit()
