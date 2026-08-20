"""Connection helper. schema.sql is all CREATE ... IF NOT EXISTS, so applying it
on every connect is idempotent and there is nothing to remember to migrate.

ponytail: works only while schema changes are additive. The first destructive
ALTER is the signal to bring in Alembic.
"""

from __future__ import annotations

import os
import pathlib

import psycopg

DSN = os.environ.get("RECUR_DSN", "postgresql://recur:recur@localhost:5433/recur")
_SCHEMA = pathlib.Path(__file__).with_name("schema.sql")


def connect() -> psycopg.Connection:
    conn = psycopg.connect(DSN)
    with conn.cursor() as cur:
        cur.execute(_SCHEMA.read_text())
    conn.commit()
    return conn
