#!/usr/bin/env python3
"""Read-only JSON API over what the pipeline already computed.

    .venv/bin/uvicorn api:app --reload --port 8000

Every endpoint here is a thin projection of `subscription` / `price_change`.
The logic lives in detect.py and resolve.py, and week 5's MCP server calls the
same functions this module does -- so REST and MCP can't drift apart, because
there is only one implementation to drift from.

Read-only on purpose: nothing here can mutate financial data. Ingest and
resolution are deliberate local commands, not something a web request can start.
"""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

import db
from detect import CADENCES, USAGE_CV

PERIOD = dict((c[0], c[1]) for c in CADENCES)

app = FastAPI(title="Recur", description="Recurring-charge truth engine")

# Local-first single-user tool: the only client is the Vite dev server.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    with db.connect(readonly=True) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _as_of() -> date:
    with db.connect(readonly=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT max(posted_date) FROM raw_transaction")
        return cur.fetchone()[0] or date.today()


@app.get("/api/summary")
def summary() -> dict:
    rows = _rows(
        "SELECT s.cadence, s.current_amount_cents, s.status FROM subscription s"
    )
    active = [r for r in rows if r["status"] == "active"]
    annual = sum(r["current_amount_cents"] * 365.25 / PERIOD[r["cadence"]]
                 for r in active)
    changes = _rows("SELECT new_amount_cents - old_amount_cents AS d,"
                    " s.period_days FROM price_change p"
                    " JOIN subscription s ON s.id = p.subscription_id")
    return {
        "as_of": _as_of(),
        "active_count": len(active),
        "inactive_count": len(rows) - len(active),
        "annual_cents": round(annual),
        "monthly_cents": round(annual / 12),
        "price_increase_annual_cents": round(
            sum(c["d"] * 365.25 / float(c["period_days"]) for c in changes)),
    }


@app.get("/api/subscriptions")
def subscriptions() -> list[dict]:
    rows = _rows(
        "SELECT s.id, m.canonical_name AS merchant, s.cadence, s.period_days,"
        "       s.current_amount_cents, s.amount_cv, s.charge_count, s.confidence,"
        "       s.status, s.first_seen, s.last_seen, s.next_due "
        "FROM subscription s JOIN merchant m ON m.id = s.merchant_id "
        "ORDER BY s.current_amount_cents * (365.25 / s.period_days) DESC"
    )
    for r in rows:
        r["annual_cents"] = round(
            r["current_amount_cents"] * 365.25 / PERIOD[r["cadence"]])
        r["usage_based"] = float(r["amount_cv"]) > USAGE_CV
    return rows


@app.get("/api/upcoming")
def upcoming(days: int = Query(30, ge=1, le=365)) -> list[dict]:
    return _rows(
        "SELECT m.canonical_name AS merchant, s.next_due, s.current_amount_cents,"
        "       s.cadence FROM subscription s JOIN merchant m ON m.id = s.merchant_id "
        "WHERE s.status = 'active' AND s.next_due <= %s ORDER BY s.next_due",
        (_as_of() + timedelta(days=days),),
    )


@app.get("/api/increases")
def increases() -> list[dict]:
    rows = _rows(
        "SELECT m.canonical_name AS merchant, p.effective_date, p.old_amount_cents,"
        "       p.new_amount_cents, p.pct_change, s.period_days "
        "FROM price_change p JOIN subscription s ON s.id = p.subscription_id "
        "JOIN merchant m ON m.id = s.merchant_id ORDER BY p.effective_date DESC"
    )
    for r in rows:
        r["annual_impact_cents"] = round(
            (r["new_amount_cents"] - r["old_amount_cents"])
            * 365.25 / float(r["period_days"]))
    return rows


@app.get("/api/history/{subscription_id}")
def history(subscription_id: int) -> list[dict]:
    return _rows(
        "SELECT t.posted_date, -t.amount_cents AS amount_cents "
        "FROM raw_transaction t JOIN subscription s ON s.merchant_id = t.merchant_id "
        "AND s.account_id = t.account_id "
        "WHERE s.id = %s AND t.amount_cents < 0 ORDER BY t.posted_date",
        (subscription_id,),
    )


@app.get("/api/review-queue")
def review_queue() -> list[dict]:
    """Surfaced so the dashboard can show what the ladder refused to guess on.
    Resolving happens in the CLI -- a wrong click here would corrupt the data
    that every other number is derived from."""
    return _rows(
        "SELECT scrubbed, txn_count, candidates, top_score, reason "
        "FROM resolution_queue WHERE status = 'pending' ORDER BY txn_count DESC"
    )
