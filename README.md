# Recur

Find out what you're actually paying for. Upload a bank CSV, get the truth about
your recurring charges: what renews, what quietly went up, what hits next week.

**No bank credentials. No Plaid. No OAuth.** You download a CSV yourself and the
data stays on your machine. This system has nowhere to put a bank password.

---

## Status: complete (5 of 5)

| Week | Scope | |
|---|---|---|
| 1 | CSV ingest, schema, dedup, tier-0 descriptor scrub | **done** |
| 2 | Merchant resolution: tiers 1-2 + human review queue | **done** |
| 3 | Periodicity detection, price-change detection, forecast + benchmark | **done** |
| 4 | FastAPI read-only API + React dashboard | **done** |
| 5 | MCP server (stdio), end-to-end script | **done** |

Full design: [`../recur-spec.md`](../recur-spec.md)

## Run it

```bash
docker compose up -d
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run_all.sh                     # ingest -> resolve -> detect, on sample.csv
.venv/bin/python resolve.py --review   # answer anything the ladder wouldn't guess
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

```bash
.venv/bin/python detect.py                # find recurring series
.venv/bin/python detect.py --increases    # what quietly went up
.venv/bin/python detect.py --upcoming 30  # what hits the card next
.venv/bin/python benchmark.py             # score against known ground truth
```

Tests: `for t in test_*.py; do .venv/bin/python $t; done`

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
make_sample.py  regenerates sample.csv from REAL descriptor formats and real
             2026 subscription prices; sources cited in its docstring
sample.csv   669 rows, 26 known merchants, with a price step, a skipped
             payment, an annual series and same-day duplicates
```

## Week 2: the merchant resolution ladder

```
tier 1  exact    scrubbed string already has an alias        O(1), free
tier 2  fuzzy    token_set_ratio vs known merchants          sub-ms, free
        queue    borderline / ambiguous / suspect            a human decides
        new      nothing resembles it                        its own merchant
```

On `sample.csv`: **669/669 transactions resolved into 26 merchants with no model
call** — 26 is the ground truth exactly — with 3 items routed to review and
**zero silent splits**.

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

## Test data: real shapes, synthetic statement

No real person's transactions are used. What is real is the *descriptor formats*
and the *prices*, both from public sources cited in `make_sample.py`:
[Wikipedia](https://en.wikipedia.org/wiki/Billing_descriptor),
[Stripe](https://stripe.com/resources/more/billing-descriptors),
[Paylosophy](https://paylosophy.com/ach-credit-card-transaction-descriptors/),
[Charge Lookup](https://www.chargelookupnow.com/articles/merchant-descriptors-explained),
[Tom's Guide](https://www.tomsguide.com/entertainment/streaming/what-streaming-costs-in-2026-the-price-of-netflix-disney-plus-max-and-more).

Descriptors invented to be parseable prove nothing. These were collected because
they are awkward:

```
SUPER+ *1833-773-8471                        ┐ one merchant, sharing
BT*SUPER+1-833-773-8471 SAN FRANCISCO CA     ┘ almost no characters

HBOMAX.COM            ┐
BT*HBO MAX 877-871-4204   ├─ one merchant, three spellings
WARNERMEDIA*HBO MAX   ┘

AMZN Mktp US*2K4LM9DX3    unique order id per charge
APPLE.COM/BILL            merchant name never appears at all
SQ*BLUE BOTTLE COFFEE*5699    trailing MCC code
```

Running these found four defects the invented data never would:

1. **Order ids fragment merchants.** `2K4LM9DX3` is unique per transaction, so
   every charge became its own merchant. Now stripped: a non-leading token
   mixing letters and digits is an id, never a name (`1800FLOWERS` survives
   because it's leading).
2. **`BT*` and `PSP*` prefixes weren't known**, so Braintree and aggregator
   traffic split off. Added to the list — and it stays a *list*, not a
   `^[A-Z]{2,6}\*` regex, because the public spec shows the same syntax means
   "aggregator" in `PSP*convenient store` and "merchant" in `ZXC* Site Access`.
   Generalizing would silently destroy merchant identity.
3. **Missing spaces defeat every token scorer at once.** `HBOMAX` vs
   `WARNERMEDIA HBO MAX` scores 48 on `token_set_ratio` and 83 on
   `partial_ratio` — under both thresholds, so it split with no warning.
   Fixed with despaced containment: 1 flag across 351 merchant pairs, and it
   was the true positive.
4. **`ingest.py` never applied the schema** — only `resolve.py` did. Invisible
   until the tables were dropped.

### Where tier 2 actually stops

`AMAZON WEB SERVICES` vs `AWS EMEA` scores 80. No string metric groups those
without inventing false matches elsewhere — an acronym has no character overlap
with what it abbreviates. That is a real ceiling, and it is the honest argument
for tiers 3 and 4: an embedding or a model knows AWS *is* Amazon Web Services,
where `token_set_ratio` never can. Until then the queue catches it, which is the
correct failure — a question, not a wrong answer.

## Week 3: periodicity detection

No model is involved anywhere in this file. Four things break a naive gap check,
and all four are in `sample.csv` on purpose:

**Monthly is not 30 days.** A subscription anchored on the 31st posts on Feb 28.
On day-gaps that reads as drift; on day-of-month it is perfectly stable. Monthly
is scored both ways and keeps the better fit, with month-end days folded
together so Jan 31 / Feb 28 / Mar 31 are one anchor rather than three.

**A failed payment is not the end of a series.** Gaps are matched to the nearest
whole multiple of the period, so one missing charge reads as a wide gap. But a
cadence that only fits by assuming *most* charges were missed is rejected —
otherwise "biweekly" swallows every monthly series, each 30-day gap being two
skipped fortnights. That bug was live until the tests caught it.

**Robust ≠ accurate.** The fit metric is the *median of the deviations*, not the
median absolute deviation of them. MAD measures how consistent the error is
rather than how small: five random purchases are all roughly 325 days short of a
year, which is beautifully consistent, and scored as `annual` until this was
fixed. Day-of-month keeps MAD, but adds a majority condition — two thirds of the
charges have to actually sit on the anchor, because three dates clustering out
of five drags the median deviation to nothing while two sit weeks away.

**Not every recurring charge has a price.** AWS varies every month. Coefficient
of variation separates fixed subscriptions from usage-based ones, and
price-change detection is only run on the fixed ones.

### Price changes must persist

A step is only reported if the *following* charge agrees with it. That one
condition is what keeps prorated charges, promo months and partial refunds out
of the results — and it means the most recent charge can never trigger one on
its own. On the sample it finds Netflix's real end-of-March-2026 increase:
`24.99 -> 26.99, +8.0%, +24.00/yr`.

### Benchmark

```
26 merchants, 10 truly recurring
precision 1.000   recall 1.000   F1 1.000   cadence 10/10
```

**This is synthetic data, so treat it as a regression guard, not as evidence of
real-world accuracy.** Its value is that tuning a threshold badly now fails
loudly instead of silently. `benchmark.py` states ground truth independently of
`make_sample.py` on purpose — a scorer that imports the generator's definitions
can only ever agree with them.

### Bugs this phase found

- `biweekly` matched every monthly series (skip-divisor let short cadences
  absorb long gaps)
- `annual` matched random one-off purchases (MAD measured consistency, not size)
- "current amount" was the median of the whole series, so after a price rise
  Netflix still reported the old price and the annual total was understated
- `next_due` could land in the past, so the forecast listed charges that had
  already happened

## Week 4: API and dashboard

```bash
.venv/bin/uvicorn api:app --port 8000     # JSON API
npm --prefix web run dev                  # dashboard on :5173
```

`api.py` is a thin projection of what the pipeline already computed — and it is
**read-only by design**. Nothing reachable over HTTP can mutate financial data;
ingest and resolution stay deliberate local commands. Week 5's MCP server calls
the same functions, so REST and MCP cannot drift apart: there is only one
implementation to drift from.

### Chart decisions

The annual total is a **stat tile, not a chart** — one number needs no axes.
Subscriptions are a table with inline magnitude bars rather than a bar chart,
because the names are as important as the amounts.

Price history is a **step line**: prices hold flat and then jump, so
interpolating between points would draw a gradual rise that never happened. One
series, so no legend — the row heading names it. No chart library; it's ~40
lines of inline SVG, which also keeps full control of the mark specs (2px
stroke, ≥8px hit targets, 2px surface ring on the points).

Colours come from a validated palette — one categorical slot, checked with the
data-viz validator (lightness band, chroma floor, contrast vs surface: all pass).
Light and dark are both defined explicitly rather than auto-flipped. Price
changes carry an **arrow and a signed percentage**, never colour alone.

## Week 5: MCP server

```bash
.venv/bin/python mcp_server.py     # stdio
```

Register it with Claude Code:

```json
{
  "mcpServers": {
    "recur": {
      "command": "/absolute/path/to/recur/.venv/bin/python",
      "args": ["/absolute/path/to/recur/mcp_server.py"]
    }
  }
}
```

Six tools: `list_subscriptions`, `spending_summary`, `upcoming_charges`,
`price_increases`, `find_forgotten`, `data_quality`. Each calls the same
function `api.py` calls — one implementation, two transports, nothing to keep
in sync.

### Security posture

**stdio only.** No port, no token, nothing to leave unauthenticated. 41% of
public MCP servers ship with no auth at all; the cheapest way not to be one is
to have no network surface.

**Tool descriptions are static string literals** — never f-strings, never
interpolated with a merchant name. A descriptor comes off a CSV that a merchant
wrote; a tool description is read by the model *as instructions*. That's the
injection boundary, and `test_mcp.py` asserts it by checking that no live
merchant name appears in any tool description.

**Every tool is read-only.** Ingest and merchant resolution stay deliberate
local commands; no conversation can trigger a write to financial data.

### A number that's wrong has to say so

Run from a cold database, `detect.py` reports **9 subscriptions and $1,361/yr**.
After the review queue is worked it reports **10 and $2,097/yr** — a 54%
difference, caused entirely by three descriptors nobody had resolved yet.

So the totals now carry a warning whenever the queue is non-empty, and
`data_quality` exposes the same thing over MCP. The queue was already the right
design; what was missing was that an unworked queue silently understated every
figure on screen and nothing said so.

---

## Everything, verified

```
test_scrub.py      16 cases
test_resolve.py     9 checks
test_detect.py     17 checks
test_mcp.py        10 checks, 6 tools
benchmark.py       precision 1.000  recall 1.000  F1 1.000  cadence 10/10
```

```
669 transactions -> 26 merchants (ground truth: 26) -> 10 subscriptions
$2,097.31/year  ·  Netflix +8.0% found  ·  0 silent merchant splits
```

Benchmark numbers are against generated data, so they are a regression guard,
not a claim about field accuracy. The real test is your own statement.

## Security

The threat this design removes rather than mitigates: **there is no credential
to steal.** No Plaid, no bank login, no OAuth token, no API key. Nothing in the
repo needs a `.env`, because there is no secret to put in one. The only setting
is `RECUR_DSN`, and it has a working default.

| | |
|---|---|
| Postgres | bound to `127.0.0.1` only |
| API / MCP connections | `SET default_transaction_read_only = on` |
| SQL | fully parameterized, no string-built queries |
| MCP transport | stdio — no port, no token, no network surface |
| MCP tool descriptions | static literals, never DB content |
| Outbound network calls | none — no HTTP client or model SDK is imported |

### Two things found by testing rather than reading

**The database was on the LAN.** `ports: "5433:5432"` in Compose binds `0.0.0.0`,
not localhost. Connecting from this machine's own LAN address read all 669
transactions using the password that is printed in `docker-compose.yml`. On any
shared network — a café, an office, an apartment building — that is every
transaction you own, readable by strangers. Now `127.0.0.1:5433:5432`, verified
by a connection attempt from the LAN address being refused.

**"Read-only" was only a docstring.** `api.py` and `mcp_server.py` are now
opened with `default_transaction_read_only`, so Postgres itself rejects
`DELETE`, `UPDATE` and `DROP` on those connections. A property the database
guarantees, not one a reviewer has to take on trust.

### What is still true

Anyone with your macOS login can read the database; it is unencrypted at rest
beyond FileVault. The Postgres password is in `docker-compose.yml`, which is
acceptable *only* while the binding stays on loopback. If you ever expose the
port, that password becomes the entire defence — change it first.

### It does not use any AI credit

No model is called anywhere in the pipeline. The resolution ladder is regex and
RapidFuzz; periodicity detection is arithmetic. The MCP server ships **zero**
model calls — it only answers questions about your database. Asking Claude
"what am I paying for?" spends your Claude usage on *that conversation*, exactly
as any other message would; the server itself adds nothing.

## Which banks does this work with?

Not all of them, and the honest list matters more than the claim.

**Handled:** US, UK, EU, Indian and Australian CSV exports; `,`/`;`/tab
delimiters; separate debit/credit columns; positive-means-charge files (Amex);
`1,234.56` and `1.234,56` and `1 234,56`; `DD/MM` via `--dayfirst`; dotted
German dates; non-Latin merchant names (Japanese, Cyrillic, Devanagari, CJK,
Arabic).

**Two bugs found by testing non-US formats:**

1. `1.234,56` parsed as **$1.23** — off by a factor of a thousand, silently. The
   decimal separator is now detected (last separator wins, and only when 1–2
   digits follow it, since three digits after a separator is a thousands group).
2. `メルカリ`, `Аптека`, `नेटफ्लिक्स`, `支付宝` all scrubbed to the **empty
   string** — every non-Latin merchant name was deleted and its charges belonged
   to nobody. Word characters are now tested by Unicode category. Combining
   marks count: `str.isalnum()` is `False` for a Devanagari vowel sign, so an
   alnum test shreds `नेटफ्लिक्स` into five meaningless fragments.

**Still US-centric, and not fixed:**

- One currency per database, defaulting to USD. Multi-currency accounts and FX
  conversion are not handled at all.
- `08/03/2026` is read as March 8 unless you pass `--dayfirst`. There is no
  auto-detection, and guessing wrong is silent.
- The processor-prefix list (`SQ*`, `TST*`, `BT*`, `PSP*`) is US/EU. Indian
  `UPI/`, `IMPS/`, `NEFT/` and German `SEPA-ELV LASTSCHRIFT` prefixes are left
  in place — harmless, since fuzzy matching ignores extra tokens, but untuned.
- The trailing-state-code rule only knows US states.
