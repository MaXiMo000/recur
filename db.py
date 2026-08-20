"""Connections, and the tenant context that row-level security keys on.

Two ways in, and the distinction is the security boundary:

    with db.tenant(user_id) as conn:     # RLS active, sees only that user
    with db.admin() as conn:             # no tenant set, sees no tenant rows

`admin()` is not a backdoor. With `recur.user_id` unset, every RLS policy
evaluates NULL and the tenant tables return nothing at all -- it exists for
app_user/session/email_token, which are not tenant-scoped. Reaching for it to
read someone's transactions simply does not work.
"""

from __future__ import annotations

import os
import pathlib
from contextlib import contextmanager
from urllib.parse import quote, urlsplit, urlunsplit

import psycopg
from psycopg_pool import ConnectionPool

_SCHEMA = pathlib.Path(__file__).with_name("schema.sql")

def _normalize(dsn: str) -> str:
    # Render hands out postgres:// URLs; psycopg wants postgresql://.
    return ("postgresql://" + dsn[len("postgres://"):]
            if dsn.startswith("postgres://") else dsn)


# The owner/migration DSN. Privileged, used only to create the schema and the
# application role. The running application never uses it.
OWNER_DSN = _normalize(
    os.environ.get("DATABASE_URL")
    or os.environ.get("RECUR_DSN", "postgresql://recur:recur@127.0.0.1:5433/recur")
)

APP_ROLE = "recur_app"
APP_PASSWORD = os.environ.get("RECUR_APP_PASSWORD", "recur_app_local_dev")


def app_dsn() -> str:
    """The owner DSN with the credentials swapped for the unprivileged role.
    Everything the application does goes through this, because RLS does not
    apply to the superuser the owner DSN usually points at."""
    if os.environ.get("RECUR_APP_DSN"):
        return _normalize(os.environ["RECUR_APP_DSN"])
    u = urlsplit(OWNER_DSN)
    host = u.hostname or "127.0.0.1"
    netloc = f"{APP_ROLE}:{quote(APP_PASSWORD)}@{host}"
    if u.port:
        netloc += f":{u.port}"
    return urlunsplit((u.scheme, netloc, u.path, u.query, u.fragment))


DSN = app_dsn()


def _reset(conn: psycopg.Connection) -> None:
    """Runs when a connection goes back to the pool. Without this, a pooled
    connection would carry one tenant's id into the next request that borrows
    it -- which is the multi-tenant bug that leaks everything."""
    conn.execute("RESET recur.user_id")
    conn.commit()


# A psycopg pool cannot be reopened once closed, so the pool is rebuilt rather
# than reused. Anything that restarts -- a test suite, a worker that recycles,
# a reload -- would otherwise die on the second start.
_pool: ConnectionPool | None = None


def _new_pool() -> ConnectionPool:
    return ConnectionPool(
        DSN,
        min_size=1,
        max_size=int(os.environ.get("RECUR_POOL_MAX", "10")),
        reset=_reset,
        open=False,
        kwargs={"application_name": "recur"},
    )


def open_pool() -> None:
    global _pool
    if _pool is None:
        _pool = _new_pool()
        _pool.open()


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def _get() -> ConnectionPool:
    if _pool is None:
        open_pool()
    assert _pool is not None
    return _pool


@contextmanager
def tenant(user_id: int):
    """A connection that can see exactly one user's rows.

    set_config's third argument is `false` (session scope, not transaction
    scope) because the pipeline commits several times inside one unit of work
    and a transaction-scoped setting would vanish at the first commit, leaving
    every subsequent statement invisible to itself. The pool's reset callback is
    what makes session scope safe.
    """
    if not isinstance(user_id, int) or user_id <= 0:
        raise ValueError("tenant() needs a real user id")
    with _get().connection() as conn:
        conn.execute("SELECT set_config('recur.user_id', %s, false)", (str(user_id),))
        yield conn


@contextmanager
def admin():
    """No tenant. Use for identity tables only; tenant tables return zero rows."""
    with _get().connection() as conn:
        yield conn


def apply_schema() -> None:
    """Fresh-database bootstrap, run as the owner. Creates the schema and the
    unprivileged application role that RLS actually binds."""
    from psycopg import sql

    with psycopg.connect(OWNER_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(_SCHEMA.read_text())
            role = sql.Identifier(APP_ROLE)
            cur.execute(
                sql.SQL("""
                DO $role$ BEGIN
                    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = {name}) THEN
                        CREATE ROLE {role} LOGIN;
                    END IF;
                END $role$;
                """).format(name=sql.Literal(APP_ROLE), role=role))
            # NOSUPERUSER NOBYPASSRLS is the line that makes every policy real.
            cur.execute(sql.SQL(
                "ALTER ROLE {role} WITH LOGIN NOSUPERUSER NOBYPASSRLS "
                "NOCREATEDB NOCREATEROLE PASSWORD {pw}"
            ).format(role=role, pw=sql.Literal(APP_PASSWORD)))
            for stmt in (
                "GRANT USAGE ON SCHEMA public TO {role}",
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}",
                "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}",
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {role}",
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                "GRANT USAGE, SELECT ON SEQUENCES TO {role}",
            ):
                cur.execute(sql.SQL(stmt).format(role=role))
        conn.commit()
