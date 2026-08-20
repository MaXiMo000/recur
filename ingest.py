#!/usr/bin/env python3
"""Load a bank/card CSV export. No credentials, no API -- just a file you
downloaded yourself.

    python ingest.py ~/Downloads/chase.csv --account chase-sapphire
    python ingest.py ~/Downloads/amex.csv  --account amex --flip-sign

Banks disagree on column names, date order and which sign means "money left".
Rather than a per-bank registry that rots, headers are matched by keyword and
the two genuinely ambiguous choices (sign, day-vs-month-first) get a flag.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
import sys
from collections import Counter
from datetime import datetime

import db
from scrub import scrub

_DATE_FORMATS_US = ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%m-%d-%Y",
                    "%Y/%m/%d", "%d-%b-%Y", "%b %d, %Y", "%d %b %Y")
_DATE_FORMATS_INTL = ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%d-%m-%Y",
                      "%Y/%m/%d", "%d-%b-%Y", "%d %b %Y")


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #

def pick_column(headers: list[str], *keywords: str, exclude: tuple = ()) -> str | None:
    """First header containing any keyword, in keyword priority order."""
    for kw in keywords:
        for h in headers:
            hl = h.lower()
            if kw in hl and not any(x in hl for x in exclude):
                return h
    return None


def parse_amount(raw: str) -> int | None:
    """'$1,234.56' / '(45.00)' -> integer cents. Parens mean negative."""
    s = (raw or "").strip()
    if not s:
        return None
    negative = s.startswith("(") and s.endswith(")")
    s = re.sub(r"[^\d.\-]", "", s)
    if not s or s in {"-", "."}:
        return None
    try:
        cents = round(float(s) * 100)
    except ValueError:
        return None
    return -abs(cents) if negative else cents


def parse_date(raw: str, dayfirst: bool):
    s = (raw or "").strip()
    for fmt in (_DATE_FORMATS_INTL if dayfirst else _DATE_FORMATS_US):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def read_rows(path: str, dayfirst: bool, flip_sign: bool, verbose: bool = True):
    """Yield (posted_date, amount_cents, descriptor). Negative = money out."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        sample = fh.read(8192)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(fh, dialect=dialect)
        headers = [h for h in (reader.fieldnames or []) if h]
        if not headers:
            sys.exit(f"{path}: no header row found")

        # "Post Date" beats "Transaction Date" -- posting is when it hit the card.
        date_col = pick_column(headers, "post date", "posted", "date")
        desc_col = pick_column(headers, "description", "merchant", "payee",
                               "narrative", "details", "name")
        amt_col = pick_column(headers, "amount", exclude=("running", "balance"))
        debit_col = pick_column(headers, "debit", "withdrawal")
        credit_col = pick_column(headers, "credit", "deposit")

        if not date_col or not desc_col:
            sys.exit(f"{path}: could not find date/description columns in {headers}")
        if not amt_col and not debit_col:
            sys.exit(f"{path}: could not find an amount or debit column in {headers}")

        if verbose:
            print(f"columns -> date={date_col!r} desc={desc_col!r} "
                  f"amount={amt_col or f'{debit_col}/{credit_col}'!r}")

        skipped = 0
        for row in reader:
            when = parse_date(row.get(date_col, ""), dayfirst)
            desc = (row.get(desc_col) or "").strip()

            if debit_col and not amt_col:
                debit = parse_amount(row.get(debit_col, ""))
                credit = parse_amount(row.get(credit_col, "")) if credit_col else None
                cents = -abs(debit) if debit else (abs(credit) if credit else None)
            else:
                cents = parse_amount(row.get(amt_col, ""))
                if cents is not None and flip_sign:
                    cents = -cents

            if when is None or cents is None or not desc:
                skipped += 1
                continue
            yield when, cents, desc

        if skipped and verbose:
            print(f"skipped {skipped} unparseable rows (blank/summary lines)")


def looks_flipped(path: str, dayfirst: bool) -> bool:
    """Amex-style files record charges as positive. If most rows are positive,
    the file almost certainly uses positive=charge."""
    signs = [c for _, c, _ in read_rows(path, dayfirst, False, verbose=False)]
    if not signs:
        return False
    return sum(1 for c in signs if c > 0) / len(signs) > 0.7


# --------------------------------------------------------------------------- #
# load
# --------------------------------------------------------------------------- #

def load(path: str, account: str, dayfirst: bool, flip_sign: bool) -> None:
    rows = list(read_rows(path, dayfirst, flip_sign))
    if not rows:
        sys.exit("nothing parsed")

    source = os.path.basename(path)

    # Two identical charges on the same day are real (two coffees), and
    # re-uploading the same statement must still be a no-op. An occurrence
    # index inside each duplicate group gives both behaviours from one hash.
    seen: Counter = Counter()
    records = []
    for when, cents, desc in rows:
        key = (account, when, cents, desc)
        n = seen[key]
        seen[key] += 1
        blob = f"{account}|{when}|{cents}|{desc}|{n}"
        records.append((
            when, cents, desc, scrub(desc), source,
            hashlib.sha256(blob.encode()).hexdigest(),
        ))

    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO account (label) VALUES (%s) "
            "ON CONFLICT (label) DO UPDATE SET label = EXCLUDED.label RETURNING id",
            (account,),
        )
        account_id = cur.fetchone()[0]

        cur.executemany(
            "INSERT INTO raw_transaction "
            "(account_id, posted_date, amount_cents, raw_descriptor, scrubbed,"
            " source_file, dedup_hash) "
            f"VALUES ({account_id}, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (dedup_hash) DO NOTHING",
            records,
        )
        inserted = cur.rowcount
        conn.commit()

        print(f"\n{len(records)} rows read, {inserted} new, "
              f"{len(records) - inserted} already present")
        report(cur, account_id)


def report(cur, account_id: int) -> None:
    cur.execute(
        "SELECT scrubbed, count(*), sum(amount_cents) "
        "FROM raw_transaction WHERE account_id = %s AND amount_cents < 0 "
        "GROUP BY scrubbed ORDER BY count(*) DESC, sum(amount_cents) LIMIT 15",
        (account_id,),
    )
    print("\nmost frequent merchants (candidates for recurring charges):\n")
    print(f"  {'merchant':<34} {'n':>4}  {'total':>12}")
    for name, n, total in cur.fetchall():
        print(f"  {name[:34]:<34} {n:>4}  {-total / 100:>11,.2f}")
    print("\nweek 2 turns these into canonical merchants; week 3 finds the cadence.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv_path")
    ap.add_argument("--account", required=True, help="label, e.g. chase-sapphire")
    ap.add_argument("--flip-sign", action="store_true",
                    help="file records charges as positive (Amex style)")
    ap.add_argument("--dayfirst", action="store_true",
                    help="dates are DD/MM/YYYY rather than MM/DD/YYYY")
    args = ap.parse_args()

    flip = args.flip_sign
    if not flip and looks_flipped(args.csv_path, args.dayfirst):
        print("note: most amounts are positive -- treating positive as a charge. "
              "Override with --flip-sign if that's wrong.")
        flip = True

    load(args.csv_path, args.account, args.dayfirst, flip)


if __name__ == "__main__":
    main()
