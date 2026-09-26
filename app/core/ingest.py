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
from app.core.money import minor_units, to_minor
from app.core.scrub import scrub

_DATE_FORMATS_US = ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%m-%d-%Y",
                    "%Y/%m/%d", "%d-%b-%Y", "%d-%b-%y", "%b %d, %Y", "%d %b %Y",
                    "%d %b %y", "%d.%m.%Y")
_DATE_FORMATS_INTL = ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%d-%m-%Y", "%d-%m-%y",
                      "%d.%m.%Y", "%d.%m.%y", "%Y/%m/%d", "%d-%b-%Y", "%d-%b-%y",
                      "%d %b %Y", "%d %b %y", "%d/%b/%Y")


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #

# Column headers, in the languages a bank export actually arrives in. A
# maintained list rather than a clever guess: getting the amount column wrong
# is not an inconvenience, it is wrong numbers presented confidently.
DATE_WORDS = ("date", "datum", "fecha", "data", "dato", "tarih", "päivä",
              "buchung", "valuta")
DESC_WORDS = ("description", "beschreibung", "verwendungszweck", "buchungstext",
              "merchant", "payee", "narration", "narrative", "remarks", "details", "concepto",
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


def parse_amount(raw: str, currency: str = "USD") -> int | None:
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

    # How many trailing digits can be a fraction depends on the currency: three
    # digits after the separator is a thousands group in dollars but a genuine
    # fraction in dinars, and in yen there is no fractional part at all. Getting
    # these two rules to interact correctly is the whole point of this block.
    places = minor_units(currency)
    fractional = range(1, places + 1)

    # Zero-decimal currencies still show up written as "12.00" by exporters
    # that format every amount the same way. Two trailing digits there is a
    # spurious fraction to discard, not a thousands group -- reading it as
    # thousands turns 12 yen into 1200.
    spurious = places == 0 and len(s) > 3 and s[-3] in ".," and s[-2:].isdigit()
    if spurious:
        s = s[:-3]

    dot, comma = s.rfind("."), s.rfind(",")
    if dot > -1 and comma > -1:
        dec = "." if dot > comma else ","
    elif comma > -1:
        dec = "," if len(s) - comma - 1 in fractional else None
    elif dot > -1:
        dec = "." if len(s) - dot - 1 in fractional else None
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
        # Scaled by the currency's own minor-unit count, not always by 100:
        # multiplying yen by 100 inflates a Japanese statement 100-fold.
        units = to_minor(float(s), currency)
    except ValueError:
        return None
    return -abs(units) if negative else units


def parse_date(raw: str, dayfirst: bool):
    s = (raw or "").strip()
    for fmt in (_DATE_FORMATS_INTL if dayfirst else _DATE_FORMATS_US):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


DEBIT_EXACT = ("dr", "dr.", "w/d")     # whole-header only: "dr" is inside "address"
CREDIT_EXACT = ("cr", "cr.")
INDICATOR_VALUES = {"dr", "cr", "d", "c", "debit", "credit"}
MONEY_EXCLUDE = ("running", "balance", "saldo", "solde", "bal")


def _exact(headers: list[str], names: tuple) -> str | None:
    for h in headers:
        if h.strip().lower() in names:
            return h
    return None


def _columns(headers: list[str]) -> dict:
    """Which header is which, or {} if these can't be a transaction table's
    headers. Pure: used both to find the header row and to read it."""
    # "Post Date" beats "Transaction Date" -- posting is when it hit the card.
    date_col = pick_column(headers, "post date", "posted", *DATE_WORDS)
    desc_col = pick_column(headers, *DESC_WORDS, exclude=("date",))
    debit_col = (_exact(headers, DEBIT_EXACT)
                 or pick_column(headers, *DEBIT_WORDS, exclude=MONEY_EXCLUDE))
    credit_col = (_exact(headers, CREDIT_EXACT)
                  or pick_column(headers, *CREDIT_WORDS, exclude=MONEY_EXCLUDE))
    # A "Withdrawal Amount (INR)" header contains "amount" too. Taken as the
    # one amount column, it silently dropped every deposit: debit and
    # credit headers are never the single amount column.
    # And "value" (an amount word in European exports) is also in HDFC's
    # "Value Dt" -- a date. Date-like headers are never the amount.
    amt_col = pick_column(headers, *AMOUNT_WORDS,
                          exclude=MONEY_EXCLUDE + DEBIT_WORDS + CREDIT_WORDS + ("date", "value dt"))
    if not date_col or not desc_col or not (amt_col or debit_col):
        return {}
    return {"date": date_col, "desc": desc_col, "amount": amt_col,
            "debit": debit_col, "credit": credit_col}


def _indicator_column(rows: list[dict], headers: list[str], skip: set) -> str | None:
    """A column whose every non-empty value is DR/CR (or D/C, Debit/Credit):
    the sign of an always-positive amount column, in exports that split
    them (common in Indian bank statements)."""
    for h in headers:
        if h in skip:
            continue
        values = {(r.get(h) or "").strip().lower().rstrip(".") for r in rows[:200]} - {""}
        if values and values <= INDICATOR_VALUES:
            return h
    return None


def _needs_dayfirst(values: list[str]) -> bool:
    """True only when month-first is impossible: some date doesn't parse
    that way ("25/09/2026") and every one parses day-first. When both
    readings work (01/03/2026), nothing here overrides the caller's flag."""
    values = [v for v in values if v and v.strip()]
    if not values:
        return False
    us_fails = any(parse_date(v, dayfirst=False) is None for v in values)
    intl_ok = all(parse_date(v, dayfirst=True) is not None for v in values)
    return us_fails and intl_ok


def read_rows(fh, dayfirst: bool, flip_sign: bool, verbose: bool = True,
              currency: str = "USD"):
    """Yield (posted_date, amount_cents, descriptor). Negative = money out.

    Takes an open text stream rather than a path, so an uploaded file can be
    parsed straight out of memory and never has to touch the server's disk.

    Real exports aren't always a clean table: many banks put an account
    summary above the header row. The header is the first of the opening
    lines that has a date column, a description column and an amount (or
    debit) column; everything above it is skipped.
    """
    text = fh.read()
    fh.seek(0)
    lines = text.splitlines()
    try:
        dialect = csv.Sniffer().sniff("\n".join(lines[:60])[:16384], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel

    start, cols, headers = None, {}, []
    for i, row in enumerate(csv.reader(lines[:60], dialect)):
        candidate = [h.strip() for h in row if h and h.strip()]
        cols = _columns(candidate)
        if cols:
            start, headers = i, candidate
            break
    if start is None:
        first = next(csv.reader(lines[:1], dialect), [])
        if not [h for h in first if h and h.strip()]:
            raise ValueError("No header row found in the CSV.")
        raise ValueError(
            "Could not find date, description and amount columns in the first "
            f"lines of the file. Saw: {[h.strip() for h in first if h]}")

    reader = csv.DictReader(lines[start:], dialect=dialect)
    reader.fieldnames = [(h or "").strip() for h in (reader.fieldnames or [])]
    rows = list(reader)

    indicator = None
    if cols["amount"] is None and cols["debit"] and not cols["credit"]:
        # One money column named like a debit ("Debit/Credit") plus a DR/CR
        # column: the money column is the amount, the other gives the sign.
        indicator = _indicator_column(rows, headers, {cols["date"], cols["desc"], cols["debit"]})
        if indicator:
            cols["amount"], cols["debit"] = cols["debit"], None
    elif cols["amount"]:
        indicator = _indicator_column(rows, headers, {cols["date"], cols["desc"], cols["amount"]})

    if not dayfirst and _needs_dayfirst([r.get(cols["date"], "") for r in rows[:500]]):
        dayfirst = True

    if verbose:
        money = cols["amount"] or f"{cols['debit']}/{cols['credit']}"
        print(f"columns -> date={cols['date']!r} desc={cols['desc']!r} amount={money!r}"
              + (f" sign={indicator!r}" if indicator else "")
              + (" (day-first dates)" if dayfirst else ""))

    skipped = 0
    for row in rows:
        when = parse_date(row.get(cols["date"], ""), dayfirst)
        desc = (row.get(cols["desc"]) or "").strip()

        if cols["amount"]:
            cents = parse_amount(row.get(cols["amount"], ""), currency)
            if cents is not None and indicator:
                mark = (row.get(indicator) or "").strip().lower().rstrip(".")
                cents = -abs(cents) if mark in ("dr", "d", "debit") else abs(cents)
            elif cents is not None and flip_sign:
                cents = -cents
        else:
            debit = parse_amount(row.get(cols["debit"], ""), currency)
            credit = parse_amount(row.get(cols["credit"], ""), currency) if cols["credit"] else None
            cents = -abs(debit) if debit else (abs(credit) if credit else None)

        if when is None or cents is None or not desc:
            skipped += 1
            continue
        yield when, cents, desc

    if skipped and verbose:
        print(f"skipped {skipped} unparseable rows (blank/summary lines)")


def looks_flipped(fh, dayfirst: bool, currency: str = "USD") -> bool:
    """Amex-style files record charges as positive. If most rows are positive,
    the file almost certainly uses positive=charge."""
    signs = [c for _, c, _ in read_rows(fh, dayfirst, False, verbose=False,
                                        currency=currency)]
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
        flip_sign = looks_flipped(fh, dayfirst, currency)
    rows = list(read_rows(fh, dayfirst, flip_sign, verbose=False, currency=currency))
    if not rows:
        raise ValueError("No usable rows found in that file.")
    if len(rows) > max_rows:
        raise ValueError(f"That file has {len(rows):,} rows; the limit is {max_rows:,}.")

    # Two identical charges on the same day are real (two coffees), and
    # re-uploading the same statement must still be a no-op. An occurrence
    # index inside each duplicate group gives both behaviours from one hash.
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO account (user_id, label, currency) VALUES (%s, %s, %s) "
            "ON CONFLICT (user_id, label) DO UPDATE SET label = EXCLUDED.label "
            "RETURNING id", (user_id, account, currency))
        account_id = cur.fetchone()[0]

        # Keyed on account_id, not the label. Hashing the label meant renaming
        # an account made every transaction in it look new, and a second upload
        # duplicated the entire history.
        seen: Counter = Counter()
        records = []
        for when, cents, desc in rows:
            key = (account_id, when, cents, desc)
            n = seen[key]
            seen[key] += 1
            blob = f"{user_id}|{account_id}|{when}|{cents}|{desc}|{n}"
            records.append((when, cents, currency, desc, scrub(desc), source,
                            hashlib.sha256(blob.encode()).hexdigest()))

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
