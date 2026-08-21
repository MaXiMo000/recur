#!/usr/bin/env python3
"""Load a bank/card CSV export. No credentials, no API -- just a file you
downloaded yourself.

    python -m app.core.ingest ~/Downloads/chase.csv --account chase-sapphire
    python -m app.core.ingest ~/Downloads/amex.csv  --account amex --flip-sign

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

from app import db
from app.core.scrub import scrub

_DATE_FORMATS_US = ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%m-%d-%Y",
                    "%Y/%m/%d", "%d-%b-%Y", "%b %d, %Y", "%d %b %Y",
                    "%d.%m.%Y")
_DATE_FORMATS_INTL = ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%d-%m-%Y",
                      "%d.%m.%Y", "%d.%m.%y", "%Y/%m/%d", "%d-%b-%Y", "%d %b %Y")


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #

# Column headers, in the languages a bank export actually arrives in. A
# maintained list rather than a clever guess: getting the amount column wrong
# is not an inconvenience, it is wrong numbers presented confidently.
DATE_WORDS = ("date", "datum", "fecha", "data", "dato", "tarih", "päivä",
              "buchung", "valuta")
DESC_WORDS = ("description", "beschreibung", "verwendungszweck", "buchungstext",
              "merchant", "payee", "narrative", "details", "concepto",
              "descrizione", "omschrijving", "libellé", "libelle", "tekst",
              "opis", "name", "particulars")
AMOUNT_WORDS = ("amount", "betrag", "importe", "importo", "montant", "bedrag",
                "kwota", "belopp", "beløb", "summa", "value")
DEBIT_WORDS = ("debit", "withdrawal", "soll", "débito", "debito", "obciążenie",
               "uttag")
CREDIT_WORDS = ("credit", "deposit", "haben", "crédito", "credito", "uznanie",
                "insättning")


def pick_column(headers: list[str], *keywords: str, exclude: tuple = ()) -> str | None:
    """First header containing any keyword, in keyword priority order."""
    for kw in keywords:
        for h in headers:
            hl = h.lower()
            if kw in hl and not any(x in hl for x in exclude):
                return h
    return None


def parse_amount(raw: str) -> int | None:
    """'$1,234.56', '1.234,56', '1 234,56', '(45.00)' -> integer cents.

    Half the world writes 1.234,56 for what the US writes as 1,234.56. Stripping
    every non-digit except '.' turns the European form into 1.23456 -- off by a
    factor of a thousand, with no error and no warning. So the decimal separator
    is *detected*: whichever of '.' or ',' appears last, and only when 1-2 digits
    follow it (three digits after a separator is a thousands group, not a price).
    """
    s = (raw or "").strip()
    if not s:
        return None
    negative = (s.startswith("(") and s.endswith(")")) or "-" in s
    s = re.sub(r"[^\d.,\s]", "", s).strip()
    if not s:
        return None

    dot, comma = s.rfind("."), s.rfind(",")
    if dot > -1 and comma > -1:
        dec = "." if dot > comma else ","
    elif comma > -1:
        dec = "," if len(s) - comma - 1 in (1, 2) else None
    elif dot > -1:
        dec = "." if len(s) - dot - 1 in (1, 2) else None
    else:
        dec = None

    if dec == ",":
        s = s.replace(".", "").replace(" ", "").replace(",", ".")
    else:
        s = s.replace(",", "").replace(" ", "")
        if dec is None:
            s = s.replace(".", "")       # '1.234' is thousands, not 1.234

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


def read_rows(fh, dayfirst: bool, flip_sign: bool, verbose: bool = True):
    """Yield (posted_date, amount_cents, descriptor). Negative = money out.

    Takes an open text stream rather than a path, so an uploaded file can be
    parsed straight out of memory and never has to touch the server's disk.
    """
    if True:
        sample = fh.read(8192)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(fh, dialect=dialect)
        headers = [h for h in (reader.fieldnames or []) if h]
        if not headers:
            raise ValueError("No header row found in the CSV.")

        # "Post Date" beats "Transaction Date" -- posting is when it hit the card.
        date_col = pick_column(headers, "post date", "posted", *DATE_WORDS)
        desc_col = pick_column(headers, *DESC_WORDS)
        amt_col = pick_column(headers, *AMOUNT_WORDS,
                              exclude=("running", "balance", "saldo", "solde"))
        debit_col = pick_column(headers, *DEBIT_WORDS)
        credit_col = pick_column(headers, *CREDIT_WORDS)

        if not date_col or not desc_col:
            raise ValueError(
                f"Could not find date and description columns. Saw: {headers}")
        if not amt_col and not debit_col:
            raise ValueError(
                f"Could not find an amount or debit column. Saw: {headers}")

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


def looks_flipped(fh, dayfirst: bool) -> bool:
    """Amex-style files record charges as positive. If most rows are positive,
    the file almost certainly uses positive=charge."""
    signs = [c for _, c, _ in read_rows(fh, dayfirst, False, verbose=False)]
    fh.seek(0)
    if not signs:
        return False
    return sum(1 for c in signs if c > 0) / len(signs) > 0.7


# --------------------------------------------------------------------------- #
# load
# --------------------------------------------------------------------------- #

def load(conn, user_id: int, fh, account: str, dayfirst: bool = False,
         flip_sign: bool | None = None, source: str = "upload",
         currency: str = "USD", max_rows: int = 200_000) -> dict:
    """Parse a statement stream into one tenant's raw_transaction rows.

    `conn` must already be tenant-scoped (db.tenant), so RLS -- not this
    function -- is what guarantees the rows land against the right user.
    """
    if flip_sign is None:
        flip_sign = looks_flipped(fh, dayfirst)
    rows = list(read_rows(fh, dayfirst, flip_sign, verbose=False))
    if not rows:
        raise ValueError("No usable rows found in that file.")
    if len(rows) > max_rows:
        raise ValueError(f"That file has {len(rows):,} rows; the limit is {max_rows:,}.")

    # Two identical charges on the same day are real (two coffees), and
    # re-uploading the same statement must still be a no-op. An occurrence
    # index inside each duplicate group gives both behaviours from one hash.
    seen: Counter = Counter()
    records = []
    for when, cents, desc in rows:
        key = (account, when, cents, desc)
        n = seen[key]
        seen[key] += 1
        blob = f"{user_id}|{account}|{when}|{cents}|{desc}|{n}"
        records.append((when, cents, currency, desc, scrub(desc), source,
                        hashlib.sha256(blob.encode()).hexdigest()))

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO account (user_id, label, currency) VALUES (%s, %s, %s) "
            "ON CONFLICT (user_id, label) DO UPDATE SET label = EXCLUDED.label "
            "RETURNING id", (user_id, account, currency))
        account_id = cur.fetchone()[0]
        cur.executemany(
            "INSERT INTO raw_transaction "
            "(user_id, account_id, posted_date, amount_cents, currency,"
            " raw_descriptor, scrubbed, source_file, dedup_hash) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (user_id, dedup_hash) DO NOTHING",
            [(user_id, account_id, *r) for r in records])
        inserted = cur.rowcount
    conn.commit()
    return {"read": len(records), "inserted": inserted,
            "duplicates": len(records) - inserted, "account_id": account_id,
            "flip_sign": flip_sign}


def report(cur, user_id: int, account_id: int) -> None:
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
    ap.add_argument("--user", type=int, required=True, help="user id to load into")
    ap.add_argument("--flip-sign", action="store_true",
                    help="file records charges as positive (Amex style)")
    ap.add_argument("--dayfirst", action="store_true",
                    help="dates are DD/MM/YYYY rather than MM/DD/YYYY")
    args = ap.parse_args()

    from app import db
    db.apply_schema()
    db.open_pool()
    try:
        with db.tenant(args.user) as conn:
            with open(args.csv_path, newline="", encoding="utf-8-sig") as fh:
                r = load(conn, args.user, fh, args.account, args.dayfirst,
                         args.flip_sign or None,
                         source=os.path.basename(args.csv_path))
            print(f"{r['read']} rows read, {r['inserted']} new, "
                  f"{r['duplicates']} already present")
            report(conn.cursor(), args.user, r["account_id"])
    finally:
        db.close_pool()


if __name__ == "__main__":
    main()
