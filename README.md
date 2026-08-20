# Recur

Find out what you're actually paying for. Upload a bank CSV, get the truth about
your recurring charges: what renews, what quietly went up, what hits next week.

**No bank credentials. No Plaid. No OAuth.** You download a CSV yourself and the
data stays on your machine. This system has nowhere to put a bank password.

---

## Status: week 2 of 5

| Week | Scope | |
|---|---|---|
| 1 | CSV ingest, schema, dedup, tier-0 descriptor scrub | **done** |
| 2 | Merchant resolution: tiers 1-2 + human review queue | **done** |
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

```bash
.venv/bin/python resolve.py            # run the ladder, print stats
.venv/bin/python resolve.py --review   # work the review queue
.venv/bin/python resolve.py --groups   # see what got grouped
```

Tests: `.venv/bin/python test_scrub.py && .venv/bin/python test_resolve.py`

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

## Week 2: the merchant resolution ladder

```
tier 1  exact    scrubbed string already has an alias        O(1), free
tier 2  fuzzy    token_set_ratio vs known merchants          sub-ms, free
        queue    borderline / ambiguous / suspect            a human decides
        new      nothing resembles it                        its own merchant
```

On `sample.csv`: **644/644 transactions resolved into 17 merchants with no model
call**, 2 items routed to review.

### Tiers 3 and 4 are not built, on purpose

The spec called for pgvector and an LLM tier. Tiers 1-2 cleared the field on the
data available, so building them now would be maintaining code for nothing. They
go in when a real statement produces residue that fuzzy matching can't close.

### The bug that justifies the whole design

First run auto-created 19 merchants. Correct answer was 17. Two pairs had split:

```
AMZN PRIME MEMBERSHIP  vs  AMAZON PRIME             token_set_ratio  61
GOOGLE YOUTUBEPREMIUM  vs  GOOGLE YOUTUBE PREMIUM   token_set_ratio  65
```

`token_set_ratio` is blind to abbreviations and to missing spaces. Both scored
below the borderline band, so both were classified "brand new merchant" and
created silently — you'd have been shown two separate $14.99 Amazon Prime
subscriptions with nothing indicating anything went wrong.

`partial_ratio` scores those same pairs 91 and 95. It's also permissive on short
strings, so it is wired in as a **second opinion that can only raise a question,
never answer one**: a high partial score with a low token_set score routes to the
review queue. Both pairs now reach a human, who resolves them once — and the
resolution is written back as an alias, so the string is never asked about again.

The general rule the ladder enforces: *only high confidence acts automatically;
the band between confident and clearly-new belongs to a person.* The failure mode
being defended against is not being wrong — it's being wrong quietly.
