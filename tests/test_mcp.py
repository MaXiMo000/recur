"""Run: python test_mcp.py

The MCP tools, and the one property a reader cannot verify by eye: that no tool
description carries content out of the database. A merchant descriptor is
attacker-controlled text; a tool description is read by the model as
instructions.
"""

from app import auth
from app import db
from app import mcp_tools
from app import pipeline

FAILURES = []

CSV = b"""Date,Description,Amount
09/03/2025,SP * NETFLIX.COM 866-579,-15.49
10/03/2025,NETFLIX.COM,-15.49
11/03/2025,NETFLIX.COM,-15.49
12/03/2025,NETFLIX.COM,-17.99
01/03/2026,NETFLIX.COM,-17.99
02/03/2026,NETFLIX.COM,-17.99
"""


def check(label, got, expected):
    if got != expected:
        FAILURES.append(f"  {label}\n    expected {expected!r}\n    got      {got!r}")


def main() -> None:
    db.apply_schema()
    db.open_pool()
    try:
        with db.admin() as conn:
            conn.execute("DELETE FROM app_user WHERE email LIKE %s", ("%@example.com",))
            conn.commit()
        uid, tok = auth.register("tools@example.com", "a-perfectly-fine-password")
        auth.consume_email_token(tok, "verify")
        pipeline.run(uid, CSV, "card")

        schema = mcp_tools.schema()
        check("every tool is described", len(schema), len(mcp_tools.TOOLS))
        check("each has an input schema",
              all("inputSchema" in t for t in schema), True)

        # The injection boundary: no DB content may reach a description.
        merchants = {r["merchant"] for r in mcp_tools.list_subscriptions(uid, "all")}
        check("merchants exist to leak", len(merchants) > 0, True)
        leaked = [t["name"] for t in schema
                  if any(m in t["description"] for m in merchants)]
        check("no database content in any tool description", leaked, [])

        s = mcp_tools.spending_summary(uid)
        check("summary reports the active subscription", s["active_subscriptions"], 1)
        check("summary reports the price rise",
              s["annual_cost_added_by_price_rises"] > 0, True)

        subs = mcp_tools.list_subscriptions(uid)
        check("netflix is listed at its current price", subs[0]["amount"], 17.99)
        check("filtering by annual cost works",
              mcp_tools.list_subscriptions(uid, min_annual_dollars=99999), [])

        up = mcp_tools.upcoming_charges(uid, 400)
        check("days is clamped to a year", up["days"], 365)
        check("upcoming total matches its rows",
              up["total"], round(sum(c["amount"] for c in up["charges"]), 2))

        inc = mcp_tools.price_increases(uid)
        check("the increase is reported", (inc[0]["old_amount"], inc[0]["new_amount"]),
              (15.49, 17.99))

        q = mcp_tools.data_quality(uid)
        check("data quality counts every transaction", q["transactions"], 6)

        # An unknown tool is refused rather than silently doing nothing.
        try:
            mcp_tools.call(uid, "definitely_not_a_tool", {})
            FAILURES.append("  unknown tool: expected KeyError")
        except KeyError:
            pass

        # Unexpected arguments are dropped, not forwarded into the function.
        r = mcp_tools.call(uid, "spending_summary", {"user_id": 99999, "evil": True})
        check("stray arguments cannot re-point a tool at another user",
              r["active_subscriptions"], 1)

        with db.admin() as conn:
            conn.execute("DELETE FROM app_user WHERE email LIKE %s", ("%@example.com",))
            conn.commit()
    finally:
        db.close_pool()

    if FAILURES:
        print(f"FAIL ({len(FAILURES)})")
        print("\n".join(FAILURES))
        raise SystemExit(1)
    print("ok  (13 mcp tool checks)")


if __name__ == "__main__":
    main()
