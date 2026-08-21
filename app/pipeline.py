"""One tenant's statement, start to finish: parse, resolve, detect.

Runs synchronously. A 5,000-row statement completes in well under a second, so
a job queue would be infrastructure bought against a problem nobody has yet --
the caps below are what keep that true. If real statements ever push past the
request timeout, this is the function to move behind a worker, and nothing
calling it has to change.
"""

from __future__ import annotations

import io

from app import db
from app.core import detect
from app.core import ingest
from app.core import resolve

# Caps exist because this endpoint is reachable by anyone who can register.
MAX_BYTES = 8 * 1024 * 1024
MAX_ROWS = 200_000


def run(user_id: int, raw: bytes, account: str, *, dayfirst: bool = False,
        flip_sign: bool | None = None, currency: str = "USD",
        source: str = "upload") -> dict:
    """-> a summary of what changed. Raises ValueError on anything the user
    can fix themselves, so the API can hand the message straight back."""
    if not raw:
        raise ValueError("That file is empty.")
    if len(raw) > MAX_BYTES:
        raise ValueError(f"That file is larger than {MAX_BYTES // (1024*1024)} MB.")

    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        # Windows exports from European banks are routinely cp1252.
        try:
            text = raw.decode("cp1252")
        except UnicodeDecodeError:
            raise ValueError("That file isn't text this can read (try UTF-8 CSV).")

    if "\x00" in text[:4096]:
        raise ValueError("That looks like a binary file, not a CSV.")

    with db.tenant(user_id) as conn:
        loaded = ingest.load(conn, user_id, io.StringIO(text), account,
                             dayfirst=dayfirst, flip_sign=flip_sign,
                             currency=currency, source=source, max_rows=MAX_ROWS)
        tiers = resolve.resolve_all(conn, user_id)
        found = detect.detect_all(conn, user_id)

        with conn.cursor() as cur:
            pending = cur.execute(
                "SELECT count(*) FROM resolution_queue WHERE status = 'pending'"
            ).fetchone()[0]

    return {
        "rows_read": loaded["read"],
        "rows_new": loaded["inserted"],
        "rows_duplicate": loaded["duplicates"],
        "sign_flipped": loaded["flip_sign"],
        "resolved": {k: v for k, v in tiers.items()},
        "subscriptions_found": found,
        "awaiting_review": pending,
    }


def rerun(user_id: int) -> dict:
    """Re-resolve and re-detect without new data -- what to call after someone
    answers a review-queue item, since one answer can merge two merchants and
    change every total downstream."""
    with db.tenant(user_id) as conn:
        tiers = resolve.resolve_all(conn, user_id)
        found = detect.detect_all(conn, user_id)
        with conn.cursor() as cur:
            pending = cur.execute(
                "SELECT count(*) FROM resolution_queue WHERE status = 'pending'"
            ).fetchone()[0]
    return {"resolved": dict(tiers), "subscriptions_found": found,
            "awaiting_review": pending}
