"""The MCP tools, as plain functions taking a user id.

One implementation, three callers: the stdio server (bound to a local user),
the remote HTTP endpoint (bound to the user an OAuth token names), and the
tests. A tool cannot be called without a user id, so there is no shape of this
code that reads "everyone's" data.

TOOL DESCRIPTIONS ARE STATIC. Never an f-string, never interpolated with a
merchant name. A descriptor comes off a CSV that a merchant wrote; a tool
description is read by the model as instructions. That boundary is asserted in
test_mcp.py rather than left as a comment.
"""

from __future__ import annotations

from datetime import date, timedelta

from app import db
from app.core.detect import CADENCES, USAGE_CV

PERIOD = dict((c[0], c[1]) for c in CADENCES)


def _rows(uid: int, sql: str, params: tuple = ()) -> list[dict]:
    with db.tenant(uid) as conn:
        cur = conn.execute(sql, params)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _as_of(uid: int) -> date:
    with db.tenant(uid) as conn:
        return conn.execute(
            "SELECT max(posted_date) FROM raw_transaction").fetchone()[0] or date.today()


def _subs(uid: int) -> list[dict]:
    rows = _rows(uid,
        "SELECT s.id, m.canonical_name AS merchant, s.cadence, s.current_amount_cents,"
        "       s.amount_cv, s.charge_count, s.confidence, s.status, s.last_seen,"
        "       s.next_due FROM subscription s JOIN merchant m ON m.id = s.merchant_id "
        "ORDER BY s.current_amount_cents * (365.25 / s.period_days) DESC")
    for r in rows:
        r["annual_cents"] = round(
            r["current_amount_cents"] * 365.25 / PERIOD[r["cadence"]])
        r["usage_based"] = float(r["amount_cv"]) > USAGE_CV
    return rows


def list_subscriptions(uid: int, status: str = "active",
                       min_annual_dollars: float = 0) -> list[dict]:
    out = []
    for r in _subs(uid):
        if status != "all" and r["status"] != status:
            continue
        if r["annual_cents"] < min_annual_dollars * 100:
            continue
        out.append({
            "merchant": r["merchant"], "cadence": r["cadence"],
            "amount": round(r["current_amount_cents"] / 100, 2),
            "annual": round(r["annual_cents"] / 100, 2),
            "charges_seen": r["charge_count"],
            "confidence": float(r["confidence"]),
            "usage_based": r["usage_based"], "status": r["status"],
            "next_due": str(r["next_due"]),
        })
    return out


def spending_summary(uid: int) -> dict:
    rows = _subs(uid)
    active = [r for r in rows if r["status"] == "active"]
    annual = sum(r["annual_cents"] for r in active)
    changes = _rows(uid,
        "SELECT p.new_amount_cents - p.old_amount_cents AS d, s.period_days "
        "FROM price_change p JOIN subscription s ON s.id = p.subscription_id")
    pending = _rows(uid, "SELECT count(*) AS n FROM resolution_queue "
                         "WHERE status = 'pending'")[0]["n"]
    return {
        "statement_through": str(_as_of(uid)),
        "annual": round(annual / 100, 2),
        "monthly": round(annual / 1200, 2),
        "active_subscriptions": len(active),
        "inactive_subscriptions": len(rows) - len(active),
        "annual_cost_added_by_price_rises": round(
            sum(c["d"] * 365.25 / float(c["period_days"]) for c in changes) / 100, 2),
        # Reported because an unworked queue makes every figure above too low.
        "descriptors_awaiting_review": pending,
    }


def upcoming_charges(uid: int, days: int = 30) -> dict:
    days = max(1, min(int(days), 365))
    rows = _rows(uid,
        "SELECT m.canonical_name AS merchant, s.next_due, s.current_amount_cents "
        "FROM subscription s JOIN merchant m ON m.id = s.merchant_id "
        "WHERE s.status = 'active' AND s.next_due <= %s ORDER BY s.next_due",
        (_as_of(uid) + timedelta(days=days),))
    items = [{"date": str(r["next_due"]), "merchant": r["merchant"],
              "amount": round(r["current_amount_cents"] / 100, 2)} for r in rows]
    return {"days": days, "charges": items,
            "total": round(sum(i["amount"] for i in items), 2)}


def price_increases(uid: int) -> list[dict]:
    rows = _rows(uid,
        "SELECT m.canonical_name AS merchant, p.effective_date, p.old_amount_cents,"
        "       p.new_amount_cents, p.pct_change, s.period_days "
        "FROM price_change p JOIN subscription s ON s.id = p.subscription_id "
        "JOIN merchant m ON m.id = s.merchant_id ORDER BY p.effective_date DESC")
    return [{
        "merchant": r["merchant"], "effective_date": str(r["effective_date"]),
        "old_amount": round(r["old_amount_cents"] / 100, 2),
        "new_amount": round(r["new_amount_cents"] / 100, 2),
        "pct_change": float(r["pct_change"]),
        "annual_impact": round((r["new_amount_cents"] - r["old_amount_cents"])
                               * 365.25 / float(r["period_days"]) / 100, 2),
    } for r in rows]


def find_forgotten(uid: int) -> list[dict]:
    out = []
    for r in _subs(uid):
        if r["cadence"] in ("annual", "semiannual") and r["status"] == "active":
            out.append({"merchant": r["merchant"], "reason": "annual renewal ahead",
                        "next_due": str(r["next_due"]),
                        "amount": round(r["current_amount_cents"] / 100, 2)})
        elif r["status"] == "lapsed":
            out.append({"merchant": r["merchant"],
                        "reason": "no charge for over a cycle -- cancelled, or a failed card",
                        "last_seen": str(r["last_seen"]),
                        "amount": round(r["current_amount_cents"] / 100, 2)})
    return out


def data_quality(uid: int) -> dict:
    stats = _rows(uid,
        "SELECT count(*) FILTER (WHERE merchant_id IS NOT NULL) AS resolved,"
        "       count(*) AS total FROM raw_transaction")[0]
    merchants = _rows(uid, "SELECT count(*) AS n FROM merchant")[0]["n"]
    pending = _rows(uid, "SELECT scrubbed FROM resolution_queue WHERE status = 'pending'")
    total = stats["total"]
    return {
        "transactions": total,
        "resolved_to_a_merchant": stats["resolved"],
        "resolved_pct": round(stats["resolved"] / total * 100, 1) if total else 0.0,
        "merchants": merchants,
        "awaiting_human_review": len(pending),
        "queued_descriptors": [p["scrubbed"] for p in pending],
    }


# name -> (function, description, json-schema properties, required)
TOOLS = {
    "list_subscriptions": (
        list_subscriptions,
        "List recurring charges with amount, cadence, annual cost and confidence.",
        {"status": {"type": "string", "enum": ["active", "lapsed", "cancelled", "all"],
                    "default": "active"},
         "min_annual_dollars": {"type": "number", "default": 0,
                                "description": "Only charges costing at least this per year."}},
        []),
    "spending_summary": (
        spending_summary,
        "Total recurring spend per year and per month, how many subscriptions are "
        "active, and how much annual cost price increases have added.",
        {}, []),
    "upcoming_charges": (
        upcoming_charges,
        "Charges expected in the next N days, with the total.",
        {"days": {"type": "integer", "minimum": 1, "maximum": 365, "default": 30}}, []),
    "price_increases": (
        price_increases,
        "Subscriptions whose price changed, with old and new amount, the percentage, "
        "and what it adds per year. A change is only reported once a later charge "
        "confirms it, so prorated one-offs and promo months are excluded.",
        {}, []),
    "find_forgotten": (
        find_forgotten,
        "Subscriptions most likely to be forgotten: annual renewals coming up, and "
        "anything lapsed rather than cancelled outright.",
        {}, []),
    "data_quality": (
        data_quality,
        "How much of the data resolved automatically and what is still waiting on a "
        "human decision. Worth checking before trusting a total: queued descriptors "
        "are not yet counted against any merchant.",
        {}, []),
}


def schema() -> list[dict]:
    return [{
        "name": name,
        "description": desc,
        "inputSchema": {"type": "object", "properties": props, "required": req},
    } for name, (_, desc, props, req) in TOOLS.items()]


def call(uid: int, name: str, arguments: dict | None):
    if name not in TOOLS:
        raise KeyError(name)
    fn = TOOLS[name][0]
    allowed = set(TOOLS[name][2])
    kwargs = {k: v for k, v in (arguments or {}).items() if k in allowed}
    return fn(uid, **kwargs)
