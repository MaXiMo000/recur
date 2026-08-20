"""Run: python test_mcp.py

Exercises every MCP tool against the live database, and asserts the two design
rules that a reader can't verify by eye:

  - no tool description carries content out of the database (the prompt
    injection boundary -- a merchant name is attacker-controlled text, a tool
    description is read by the model as instructions)
  - the numbers the MCP server reports match what the REST API reports, since
    the whole claim is that they share one implementation
"""

import asyncio
import json

import api
from mcp_server import mcp

EXPECTED = {"list_subscriptions", "spending_summary", "upcoming_charges",
            "price_increases", "find_forgotten", "data_quality"}
FAILURES = []


def check(label, got, expected):
    if got != expected:
        FAILURES.append(f"  {label}\n    expected {expected!r}\n    got      {got!r}")


async def call(name, args=None):
    """A tool returning a list comes back as one content block PER ITEM, not as
    a single block holding a list. Reading only block 0 silently gives you the
    first row and calls it the answer."""
    res = await mcp.call_tool(name, args or {})
    return [json.loads(b.text) for b in res.content]


async def call_one(name, args=None):
    rows = await call(name, args)
    return rows[0] if rows else {}


async def main():
    tools = await mcp.list_tools()
    check("every tool is registered", {t.name for t in tools}, EXPECTED)

    # Descriptions must be static. If a merchant name ever reaches one, a CSV
    # can write instructions the model will read.
    merchants = {r["merchant"] for r in api.subscriptions()}
    leaked = [t.name for t in tools
              if any(m in (t.description or "") for m in merchants)]
    check("no DB content in any tool description", leaked, [])

    s = await call_one("spending_summary")
    check("summary matches the REST API",
          s["annual"], round(api.summary()["annual_cents"] / 100, 2))
    check("summary counts active subs", s["active_subscriptions"],
          len([r for r in api.subscriptions() if r["status"] == "active"]))

    subs = await call("list_subscriptions", {"status": "all"})
    check("list matches the REST API", len(subs), len(api.subscriptions()))

    big = await call("list_subscriptions", {"min_annual_dollars": 200})
    check("min_annual_dollars filters", all(r["annual"] >= 200 for r in big), True)

    up = await call_one("upcoming_charges", {"days": 14})
    check("upcoming total is the sum of its charges",
          up["total"], round(sum(c["amount"] for c in up["charges"]), 2))

    # days is clamped, not trusted
    wild = await call_one("upcoming_charges", {"days": 99999})
    check("out-of-range days doesn't error", isinstance(wild["charges"], list), True)

    inc = await call("price_increases")
    check("increases match the REST API", len(inc), len(api.increases()))

    await call("find_forgotten")
    q = await call_one("data_quality")
    check("data_quality reports every transaction",
          q["resolved_to_a_merchant"] <= q["transactions"], True)

    if FAILURES:
        print(f"FAIL ({len(FAILURES)})")
        print("\n".join(FAILURES))
        raise SystemExit(1)
    print(f"ok  (10 checks, {len(tools)} tools)")


if __name__ == "__main__":
    asyncio.run(main())
