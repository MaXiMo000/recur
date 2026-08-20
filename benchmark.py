#!/usr/bin/env python3
"""Score the detector against sample.csv's known ground truth.

    python benchmark.py

sample.csv is generated, so what every merchant *should* be is known exactly.
That makes precision and recall real numbers rather than an impression, and it
makes a regression visible the moment a threshold is tuned badly.

Ground truth is stated here rather than imported from make_sample.py on purpose:
if the generator and the scorer share a definition, the scorer can only ever
agree with it.
"""

from __future__ import annotations

import re
import sys

import db

# canonical-name pattern -> expected cadence
RECURRING = {
    r"^NETFLIX":        "monthly",
    r"^APPLE BILL":     "monthly",
    r"^HBOMAX":         "monthly",
    r"AMAZON PRIME":    "monthly",
    r"^GOOGLE HULU":    "monthly",
    r"^SUPER$":         "monthly",
    r"^ZXC SITE":       "monthly",
    r"^CLAUDE AI":      "monthly",
    r"AWS":             "monthly",
    r"^NAMECHEAP":      "annual",
}


def expected(name: str) -> str | None:
    for pat, cadence in RECURRING.items():
        if re.search(pat, name):
            return cadence
    return None


def main() -> None:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT canonical_name FROM merchant")
        all_merchants = [r[0] for r in cur.fetchall()]
        cur.execute(
            "SELECT m.canonical_name, s.cadence FROM subscription s "
            "JOIN merchant m ON m.id = s.merchant_id"
        )
        detected = dict(cur.fetchall())

    if not all_merchants:
        sys.exit("no data -- run ingest.py, resolve.py and detect.py first")

    tp = [m for m in all_merchants if expected(m) and m in detected]
    fp = [m for m in all_merchants if not expected(m) and m in detected]
    fn = [m for m in all_merchants if expected(m) and m not in detected]
    tn = [m for m in all_merchants if not expected(m) and m not in detected]
    wrong = [(m, expected(m), detected[m]) for m in tp if detected[m] != expected(m)]

    precision = len(tp) / (len(tp) + len(fp)) if tp or fp else 0.0
    recall = len(tp) / (len(tp) + len(fn)) if tp or fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0

    print(f"\n  {len(all_merchants)} merchants, {len(RECURRING)} truly recurring\n")
    print(f"  true positives   {len(tp):>3}   correctly called recurring")
    print(f"  true negatives   {len(tn):>3}   correctly left alone")
    print(f"  false positives  {len(fp):>3}   one-off spending called a subscription")
    print(f"  false negatives  {len(fn):>3}   real subscription missed")
    print(f"\n  precision  {precision:.3f}")
    print(f"  recall     {recall:.3f}")
    print(f"  F1         {f1:.3f}")
    print(f"  cadence    {(len(tp) - len(wrong))}/{len(tp)} correct")

    for m in fp:
        print(f"\n  FALSE POSITIVE  {m}  -> {detected[m]}")
    for m in fn:
        print(f"\n  MISSED          {m}  (expected {expected(m)})")
    for m, exp, got in wrong:
        print(f"\n  WRONG CADENCE   {m}  expected {exp}, got {got}")

    if fp or fn or wrong:
        raise SystemExit(1)
    print()


if __name__ == "__main__":
    main()
