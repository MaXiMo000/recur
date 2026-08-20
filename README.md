# Recur

Find out what you're actually paying for. Upload a bank CSV, get the truth about
your recurring charges: what renews, what quietly went up, what hits next week.

**No bank credentials. No Plaid. No OAuth.** You download a CSV yourself and the
data stays on your machine. This system has nowhere to put a bank password.

---

## Status: week 1 of 5

| Week | Scope | |
|---|---|---|
| 1 | CSV ingest, schema, dedup, tier-0 descriptor scrub | **done** |
| 2 | Merchant resolution: fuzzy → vector → LLM → human review queue | |
| 3 | Periodicity detection, price-change detection, forecast + benchmark | |
| 4 | React dashboard | |
| 5 | MCP server, Docker packaging, demo | |

Full design: [`../recur-spec.md`](../recur-spec.md)

## Run it

```bash
docker compose up -d
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python ingest.py sample.csv --account demo
```

Then with your own statement:

```bash
.venv/bin/python ingest.py ~/Downloads/statement.csv --account chase
```

Add `--flip-sign` if your bank records charges as positive (Amex does), and
`--dayfirst` for DD/MM dates. Re-running the same file is a no-op.

Tests: `python3 test_scrub.py`

## What week 1 actually does

The interesting part is `scrub.py` — tier 0 of the merchant resolution ladder.
Bank descriptors are hostile:

```
SP * NETFLIX.COM 866-579-7172      ┐
NETFLIX.COM                        ├─→  NETFLIX
Netflix 1 800 associates           ┘
```

Pure regex, no model, no network call. Everything stripped here is noise the
fuzzy/vector/LLM tiers in week 2 would otherwise pay to ignore. Tier 0 alone
already collapses most descriptor drift for the same merchant.

Two things worth noting in the ingest:

- **Money is integer cents.** Never float.
- **Dedup uses an occurrence index.** `sha256(account|date|amount|descriptor|n)`
  where `n` is the row's position within its duplicate group. Re-uploading a
  statement inserts nothing; two genuine same-day identical charges both land.
  Most naive dedup schemes get exactly one of those two cases right.

## Layout

```
schema.sql   week-1 tables (account, merchant, merchant_alias, raw_transaction)
scrub.py     tier 0 — deterministic descriptor normalization
ingest.py    CSV dialect sniffing, sign/date normalization, dedup, load
test_scrub.py  assert-based checks against real descriptor shapes
sample.csv   608 synthetic rows with known recurring series, a price step,
             a skipped payment and same-day duplicates
```
