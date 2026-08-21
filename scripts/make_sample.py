#!/usr/bin/env python3
"""Generate sample.csv: a synthetic statement built from REAL descriptor shapes.

    python scripts/make_sample.py > sample.csv

No real person's transactions are used. What is real is the *format* of the
descriptors and the subscription prices, both taken from public sources:

  Descriptor anatomy, processor prefixes, MCC suffix, store numbers, the
  25-char processor limit, and the aggregator `[name]*[sub-merchant]` form:
    https://en.wikipedia.org/wiki/Billing_descriptor
    https://www.chargelookupnow.com/articles/merchant-descriptors-explained
    https://paylosophy.com/ach-credit-card-transaction-descriptors/
    https://stripe.com/resources/more/billing-descriptors

  2026 subscription prices, including Netflix's end-of-March 2026 increase
  across all tiers (modelled below as a real price-change event):
    https://www.tomsguide.com/entertainment/streaming/what-streaming-costs-in-2026-the-price-of-netflix-disney-plus-max-and-more

The point is not realism for its own sake. Descriptors that were invented to
be parseable prove nothing; these were collected because they are awkward.
"""

from __future__ import annotations

import csv
import random
import sys
from datetime import date, timedelta

random.seed(20260820)

rows: list[tuple[date, str, float]] = []


def add(d: date, desc: str, amt: float) -> None:
    rows.append((d, desc, round(amt, 2)))


def monthly(desc, amt, start: tuple[int, int], n: int, dom: int) -> None:
    """desc/amt may be callables of the month index, for drifting descriptors
    and mid-series price changes."""
    y, m = start
    for i in range(n):
        mm = m + i
        yy, mm = y + (mm - 1) // 12, (mm - 1) % 12 + 1
        last = [31, 29 if yy % 4 == 0 else 28, 31, 30, 31, 30,
                31, 31, 30, 31, 30, 31][mm - 1]
        add(date(yy, mm, min(dom, last)),
            desc(i) if callable(desc) else desc,
            amt(i) if callable(amt) else amt)


# --------------------------------------------------------------------------- #
# recurring: real prices, real descriptor shapes
# --------------------------------------------------------------------------- #

# Netflix Premium: $26.99 after the end-of-March-2026 increase. Series starts
# 2025-09 at the prior $24.99, so the step falls inside the window.
monthly(lambda i: random.choice([
    "NETFLIX.COM 866-579-7172",
    "NETFLIX.COM",
    "NETFLIX 866-579-7172 LOS GATOS CA",
]), lambda i: 24.99 if i < 7 else 26.99, (2025, 9), 12, 3)

# Disney+ ad-free $16.99. Apple's descriptor form is APPLE.COM/BILL -- billed
# through the App Store, so the merchant name never appears at all.
monthly(lambda i: random.choice(["APPLE.COM/BILL", "APPLE.COM/BILL 866-712-7753"]),
        16.99, (2025, 9), 12, 11)

# Max ad-free $16.99, via an aggregator: [aggregator]*[sub-merchant]
monthly(lambda i: random.choice([
    "BT*HBO MAX 877-871-4204", "HBOMAX.COM", "WARNERMEDIA*HBO MAX",
]), 16.99, (2025, 9), 12, 19)

# Amazon Prime $14.99/mo. Three shapes, one merchant. (AMZN Mktp US* is
# marketplace shopping, a *different* merchant -- it lives in NOISE below.)
monthly(lambda i: random.choice([
    "AMAZON PRIME*MW4XY2", "Amazon Prime", "AMZN Prime Membership",
]), 14.99, (2025, 9), 12, 7)

# Hulu ad-supported $9.99, billed through Google Play
monthly(lambda i: random.choice([
    "GOOGLE*HULU", "GOOGLE *Hulu g.co/helppay#", "HULU 888-265-6650",
]), 9.99, (2025, 9), 12, 25)

# Super.com premium -- the two shapes documented on chargelookupnow, which are
# the same merchant and share almost no characters.
monthly(lambda i: random.choice([
    "SUPER+ *1833-773-8471", "BT*SUPER+1-833-773-8471 SAN FRANCISCO CA",
]), 8.99, (2025, 9), 12, 14)

# Dynamic descriptor, Wikipedia's own example form: [ABC]* [service] [phone]
monthly("ZXC* Site Access 800-123-4567", 12.00, (2025, 9), 12, 28)

# Annual, 14 months apart -> only two points, must not be called monthly
add(date(2025, 9, 14), "NAMECHEAP.COM*ORDER 000198234", 38.88)
add(date(2026, 9, 13), "NAMECHEAP.COM*ORDER 000221907", 44.88)

# Usage-based recurring: high amount variance, still a real series
monthly(lambda i: random.choice([
    "AMAZON WEB SERVICES AWS.AMAZON.CO", "AWS EMEA aws.amazon.co",
]), lambda i: random.uniform(3.5, 71.0), (2025, 9), 12, 3)

# Monthly with a failed payment in month 4 -> a ~2x gap, not a broken series
monthly("CLAUDE.AI SUBSCRIPTION", 20.00, (2025, 9), 12, 9)
rows = [r for r in rows
        if not (r[1] == "CLAUDE.AI SUBSCRIPTION" and (r[0].year, r[0].month) == (2025, 12))]

# --------------------------------------------------------------------------- #
# non-recurring noise, using documented store-number / location forms
# --------------------------------------------------------------------------- #

NOISE = [
    "AMZN Mktp US*2K4LM9DX3",
    "COSTCO WHSE #1229 MIAMI FL",
    "PUBLIX #1397 MIAMI FL",
    "SQ*BLUE BOTTLE COFFEE*5699",
    "SQ *PHILZ COFFEE",
    "TST* TARTINE BAKERY 0091",
    "PSP*convenient store NY",
    "7-ELEVEN 33812 FREMONT CA",
    "SAFEWAY #1842 FREMONT CA",
    "CHEVRON 0345678 UNION CITY CA",
    "IN N OUT BURGER 234",
    "TARGET T-2213 UNION CITY CA",
    "VENMO*PAYMENT",
    "PAYPAL *ETSY INC 4029357733",
    "SHELL OIL 57444216508",
    "TRADER JOE'S #123 FREMONT CA",
]

d = date(2025, 9, 1)
while d < date(2026, 9, 1):
    for _ in range(random.randint(0, 3)):
        add(d, random.choice(NOISE), random.uniform(4, 95))
    d += timedelta(days=1)

# Two genuine same-day identical charges -> the dedup occurrence index
add(date(2026, 3, 12), "SQ *PHILZ COFFEE", 6.25)
add(date(2026, 3, 12), "SQ *PHILZ COFFEE", 6.25)

# --------------------------------------------------------------------------- #

w = csv.writer(sys.stdout)
w.writerow(["Transaction Date", "Post Date", "Description", "Category", "Type", "Amount", "Memo"])
for d_, desc, amt in sorted(rows):
    w.writerow([d_.strftime("%m/%d/%Y"), d_.strftime("%m/%d/%Y"),
                desc, "", "Sale", f"-{amt:.2f}", ""])
print(f"{len(rows)} rows", file=sys.stderr)
