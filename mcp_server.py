#!/usr/bin/env python3
"""MCP server: ask Claude about your own subscriptions.

    .venv/bin/python mcp_server.py

stdio only. The whole point of this project is that it holds no credential, so
the front door is a local process on a pipe -- there is no port, no token, and
nothing to leave unauthenticated. (41% of public MCP servers ship with no auth
at all; the cheapest way to not be one of them is to have no network surface.)

Every tool below calls the same functions api.py calls. One implementation,
two transports.

Two rules this file follows deliberately:

  1. **Tool descriptions are static string literals.** Never an f-string, never
     interpolated with a merchant name or anything else out of the database.
     A descriptor comes off a CSV that a merchant wrote; a tool description is
     read by the model as instructions. Keeping DB content out of this file's
     docstrings is the injection boundary.
  2. **Read-only.** No tool mutates anything. Ingest and merchant resolution are
     deliberate local commands, not something a conversation can trigger.
"""

from __future__ import annotations

from mcp.server import MCPServer

import api

mcp = MCPServer("recur")


@mcp.tool()
def list_subscriptions(status: str = "active", min_annual_dollars: float = 0) -> list[dict]:
    """List recurring charges with amount, cadence, annual cost and confidence.

    Args:
        status: "active", "lapsed", "cancelled", or "all".
        min_annual_dollars: only return subscriptions costing at least this much
            per year. Useful for "what are my big ones".
    """
    rows = api.subscriptions()
    out = []
    for r in rows:
        if status != "all" and r["status"] != status:
            continue
        if r["annual_cents"] < min_annual_dollars * 100:
            continue
        out.append({
            "merchant": r["merchant"],
            "cadence": r["cadence"],
            "amount": round(r["current_amount_cents"] / 100, 2),
            "annual": round(r["annual_cents"] / 100, 2),
            "charges_seen": r["charge_count"],
            "confidence": float(r["confidence"]),
            "usage_based": r["usage_based"],
            "status": r["status"],
            "next_due": str(r["next_due"]),
        })
    return out


@mcp.tool()
def spending_summary() -> dict:
    """Total recurring spend per year and per month, how many subscriptions are
    active, and how much annual cost has been added by price increases."""
    s = api.summary()
    return {
        "statement_through": str(s["as_of"]),
        "annual": round(s["annual_cents"] / 100, 2),
        "monthly": round(s["monthly_cents"] / 100, 2),
        "active_subscriptions": s["active_count"],
        "inactive_subscriptions": s["inactive_count"],
        "annual_cost_added_by_price_rises": round(
            s["price_increase_annual_cents"] / 100, 2),
    }


@mcp.tool()
def upcoming_charges(days: int = 30) -> dict:
    """Charges expected in the next N days, with the total.

    Args:
        days: how far ahead to look, 1-365.
    """
    rows = api.upcoming(days=max(1, min(days, 365)))
    items = [{"date": str(r["next_due"]), "merchant": r["merchant"],
              "amount": round(r["current_amount_cents"] / 100, 2)} for r in rows]
    return {"days": days, "charges": items,
            "total": round(sum(i["amount"] for i in items), 2)}


@mcp.tool()
def price_increases() -> list[dict]:
    """Subscriptions whose price changed, with the old and new amount, the
    percentage, and what it adds per year.

    A change is only reported once a later charge confirms it, so one-off
    prorated charges and promo months are not listed.
    """
    return [{
        "merchant": r["merchant"],
        "effective_date": str(r["effective_date"]),
        "old_amount": round(r["old_amount_cents"] / 100, 2),
        "new_amount": round(r["new_amount_cents"] / 100, 2),
        "pct_change": float(r["pct_change"]),
        "annual_impact": round(r["annual_impact_cents"] / 100, 2),
    } for r in api.increases()]


@mcp.tool()
def find_forgotten() -> list[dict]:
    """Subscriptions most likely to be forgotten: annual renewals coming up, and
    anything that has lapsed rather than being cancelled outright.

    Annual charges are the ones people miss, because eleven months pass between
    the signup and the renewal.
    """
    out = []
    for r in api.subscriptions():
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


@mcp.tool()
def data_quality() -> dict:
    """How much of the data was resolved automatically and what is still waiting
    on a human decision.

    Worth checking before trusting a total: descriptors sitting in the review
    queue are not yet counted against any merchant.
    """
    import db
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FILTER (WHERE merchant_id IS NOT NULL), count(*) "
                    "FROM raw_transaction")
        done, total = cur.fetchone()
        cur.execute("SELECT count(*) FROM merchant")
        merchants = cur.fetchone()[0]
    pending = api.review_queue()
    return {
        "transactions": total,
        "resolved_to_a_merchant": done,
        "resolved_pct": round(done / total * 100, 1) if total else 0.0,
        "merchants": merchants,
        "awaiting_human_review": len(pending),
        "queued_descriptors": [p["scrubbed"] for p in pending],
    }


if __name__ == "__main__":
    mcp.run()  # stdio is the default, and the only transport this ships with
