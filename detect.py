#!/usr/bin/env python3
"""Week 3: which merchants are actually recurring, at what cadence, and what
changed. Signal processing, not prompting -- no model is involved anywhere here.

    python detect.py             find subscriptions, print what was found
    python detect.py --upcoming  what hits the card next
    python detect.py --increases price changes, newest first

Charges are not neatly spaced. The four things that break a naive gap check:

  1. Monthly is not 30 days. A subscription anchored on the 31st posts on
     Feb 28. Judged on day-gaps that looks like drift; judged on day-of-month
     it is perfectly stable. So monthly is scored both ways and keeps the
     better fit.
  2. One refund or double charge destroys a mean. Everything here uses the
     median and median absolute deviation.
  3. Failed payments leave a ~2x gap. That is a missed charge, not the end of
     the series, so gaps are divided by the nearest whole number of periods.
  4. Some recurring charges have no fixed amount (AWS, utilities). Those are
     still subscriptions; they just can't have price-change detection run on
     them, so they're separated by coefficient of variation.
"""

from __future__ import annotations

import argparse
import calendar
from datetime import date, timedelta
from statistics import median

import db

# (name, period in days, tolerated median deviation in days)
CADENCES = [
    ("weekly",       7.0,   1.0),
    ("biweekly",    14.0,   2.0),
    ("semimonthly", 15.2,   2.0),
    ("monthly",     30.44,  3.0),
    ("quarterly",   91.3,   5.0),
    ("semiannual", 182.6,   8.0),
    ("annual",     365.25, 12.0),
]

MIN_CHARGES = 2        # annual subs only ever have 2 points in a year of data,
                       # and they're the ones worth catching -- so 2 is allowed
                       # and paid for in the confidence score instead.
MAX_SKIPPED = 3        # a gap larger than this many periods ends the series
PRICE_TOL = 0.02       # amount moves under 2% are noise, not a price change
USAGE_CV = 0.02        # above this the amount isn't fixed -> usage-based
DOM_TOL = 3            # days either side of the anchor that still count as 'on' it


# --------------------------------------------------------------------------- #
# pure logic
# --------------------------------------------------------------------------- #

def _mad(values: list[float]) -> float:
    m = median(values)
    return median([abs(v - m) for v in values])


def gap_deviation(dates: list[date], period: float) -> float | None:
    """Median deviation of the gaps from whole multiples of `period`, so a
    missed payment reads as one wide gap rather than a broken series.

    Two things this must refuse, both of which a naive version gets wrong:
    a gap that is nowhere near *any* multiple of the period, and a cadence that
    only fits by assuming most of the charges were missed -- which is how
    'biweekly' swallows a monthly series, every 30-day gap being two skipped
    fortnights.
    """
    devs, skips = [], 0
    for a, b in zip(dates, dates[1:]):
        gap = (b - a).days
        k = max(1, round(gap / period))
        if k > MAX_SKIPPED:
            return None
        if k > 1:
            skips += 1
        # Distance to the nearest multiple, NOT gap/k -- dividing lets a wildly
        # wrong gap look close after it's been split up.
        devs.append(abs(gap - k * period))
    if not devs or skips * 2 > len(devs):
        return None
    # median, NOT median-absolute-deviation. MAD would measure how *consistent*
    # the error is rather than how small: five random purchases are all roughly
    # 325 days away from being annual, which is consistent, and would score
    # beautifully. The typical error itself is what has to be small.
    return median(devs)


def dom_deviation(dates: list[date]) -> tuple[float, int]:
    """Median deviation of the day-of-month, and the anchor day.

    Computed twice: raw, and with month-end days folded together so that
    Jan 31 / Feb 28 / Mar 31 read as one anchor rather than three. The better
    of the two wins, so a genuine 28th-of-the-month series isn't punished for
    February.
    """
    raw = [d.day for d in dates]
    folded = [32 if d.day == calendar.monthrange(d.year, d.month)[1] else d.day
              for d in dates]
    best = min(_score_doms(raw), _score_doms(folded))
    return best[0], min(best[1], 31)


def _score_doms(doms: list[int]) -> tuple[float, int]:
    """MAD alone is too forgiving here. With five dates, three of them landing
    together drags the median deviation down to nothing even when the other two
    are weeks away -- which is how a handful of unrelated purchases starts
    looking like a monthly subscription. So a real majority has to actually sit
    on the anchor.
    """
    anchor = int(median(doms))
    near = sum(1 for d in doms if abs(d - anchor) <= DOM_TOL)
    if near * 3 < len(doms) * 2:
        return float("inf"), anchor
    return _mad(doms), anchor


def fit_cadence(dates: list[date]) -> tuple[str, float, float, int] | None:
    """-> (cadence, period_days, normalized_deviation, anchor_day) or None.

    Normalized deviation is deviation/tolerance, so cadences with different
    tolerances compete fairly. Lower is better.
    """
    dates = sorted(dates)
    if len(dates) < MIN_CHARGES:
        return None

    best = None
    for name, period, tol in CADENCES:
        dev = gap_deviation(dates, period)
        if dev is None:
            continue
        score = dev / tol
        anchor = None

        if name == "monthly":
            # Judge monthly on where in the month it lands, not on day-gaps.
            dom_dev, anchor = dom_deviation(dates)
            score = min(score, dom_dev / tol)

        if score <= 1.0 and (best is None or score < best[2]):
            best = (name, period, score, anchor or median([d.day for d in dates]))

    if best is None:
        return None
    return best[0], best[1], best[2], int(best[3])


def current_amount(amounts: list[int]) -> int:
    """What the merchant charges *now*. The median of the whole series is wrong
    the moment a price changes -- it keeps reporting the old price and
    understates the annual total. The last few charges, median'd so a single
    prorated final charge can't set the headline number."""
    return int(median(amounts[-3:]))


def coefficient_of_variation(amounts: list[int]) -> float:
    """Spread relative to size, using the median so one odd charge can't
    manufacture variance that isn't there."""
    m = median(amounts)
    if not m:
        return 0.0
    return _mad([float(a) for a in amounts]) / abs(m)


def confidence(n: int, norm_dev: float, cv: float) -> float:
    """More charges, tighter fit and steadier amounts all raise it. Two charges
    can never look certain no matter how neatly they line up."""
    count = min(1.0, (n - 1) / 4)
    fit = max(0.0, 1.0 - norm_dev)
    steady = max(0.0, 1.0 - min(cv, 1.0))
    return round(0.45 * count + 0.40 * fit + 0.15 * steady, 3)


def next_occurrence(last: date, cadence: str, period: float, anchor: int) -> date:
    """Monthly walks the calendar and clamps to the month's length; everything
    else is a straight period away."""
    if cadence in ("monthly", "semimonthly"):
        y, m = (last.year + (last.month == 12)), (last.month % 12 + 1)
        return date(y, m, min(anchor, calendar.monthrange(y, m)[1]))
    return last + timedelta(days=round(period))


def forecast(last: date, as_of: date, cadence: str, period: float, anchor: int) -> date:
    """The next charge that hasn't happened yet. One period past the last charge
    can still be in the past -- a series that missed a cycle, or a statement
    whose other accounts run later -- and a 'next charge' in the past is not a
    forecast. Roll forward until it's genuinely ahead."""
    nxt = next_occurrence(last, cadence, period, anchor)
    guard = 0
    while nxt <= as_of and guard < 64:
        nxt = next_occurrence(nxt, cadence, period, anchor)
        guard += 1
    return nxt


def price_changes(points: list[tuple[date, int]]) -> list[tuple[date, int, int]]:
    """Step changes in a series of (date, cents). -> [(effective, old, new)]

    A change must *persist*: the charge after it has to agree. That single
    condition is what keeps prorated charges, promo months and partial refunds
    out of the results, and it's why the most recent charge can never trigger
    one on its own.
    """
    if len(points) < 3:
        return []
    amounts = [a for _, a in points]
    out, run = [], [amounts[0]]

    for i in range(1, len(amounts)):
        base = median(run)
        a = amounts[i]
        if base and abs(a - base) / base > PRICE_TOL:
            confirmed = i + 1 < len(amounts) and abs(amounts[i + 1] - a) / a <= PRICE_TOL
            if confirmed:
                out.append((points[i][0], int(base), a))
                run = [a]
            # unconfirmed -> a one-off. Deliberately does not move the baseline.
        else:
            run.append(a)
    return out


def status_of(last_seen: date, as_of: date, period: float) -> str:
    """`as_of` is the newest transaction in the data, not today -- a statement
    exported in March shouldn't mark everything cancelled in June."""
    elapsed = (as_of - last_seen).days
    if elapsed > 2.5 * period:
        return "cancelled"
    if elapsed > 1.5 * period:
        return "lapsed"
    return "active"


# --------------------------------------------------------------------------- #
# DB glue
# --------------------------------------------------------------------------- #

def detect_all(conn) -> int:
    found = 0
    with conn.cursor() as cur:
        cur.execute("SELECT max(posted_date) FROM raw_transaction")
        as_of = cur.fetchone()[0]
        if as_of is None:
            print("no transactions loaded")
            return 0

        cur.execute(
            "SELECT merchant_id, account_id, array_agg(posted_date ORDER BY posted_date), "
            "       array_agg(-amount_cents ORDER BY posted_date) "
            "FROM raw_transaction WHERE merchant_id IS NOT NULL AND amount_cents < 0 "
            "GROUP BY merchant_id, account_id HAVING count(*) >= %s",
            (MIN_CHARGES,),
        )
        groups = cur.fetchall()

        cur.execute("TRUNCATE subscription RESTART IDENTITY CASCADE")

        for merchant_id, account_id, dates, amounts in groups:
            fit = fit_cadence(dates)
            if fit is None:
                continue
            cadence, period, norm_dev, anchor = fit
            cv = coefficient_of_variation(amounts)

            cur.execute(
                "INSERT INTO subscription (merchant_id, account_id, cadence, period_days,"
                " anchor_day, current_amount_cents, amount_cv, charge_count, first_seen,"
                " last_seen, next_due, status, confidence) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (merchant_id, account_id, cadence, round(period, 2), anchor,
                 current_amount(amounts), round(cv, 3), len(dates), dates[0], dates[-1],
                 forecast(dates[-1], as_of, cadence, period, anchor),
                 status_of(dates[-1], as_of, period),
                 confidence(len(dates), norm_dev, cv)),
            )
            sub_id = cur.fetchone()[0]
            found += 1

            # A merchant whose amount moves every month has no "price" to change.
            if cv <= USAGE_CV:
                for eff, old, new in price_changes(list(zip(dates, amounts))):
                    cur.execute(
                        "INSERT INTO price_change (subscription_id, effective_date,"
                        " old_amount_cents, new_amount_cents, pct_change) "
                        "VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                        (sub_id, eff, old, new, round((new - old) / old * 100, 2)),
                    )
        conn.commit()
    return found


def report(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT m.canonical_name, s.cadence, s.current_amount_cents, s.amount_cv,"
            "       s.charge_count, s.confidence, s.status, s.next_due "
            "FROM subscription s JOIN merchant m ON m.id = s.merchant_id "
            "ORDER BY s.current_amount_cents * (365.25 / s.period_days) DESC"
        )
        rows = cur.fetchall()
        cur.execute(
            "SELECT sum(s.current_amount_cents * (365.25 / s.period_days)) "
            "FROM subscription s WHERE s.status = 'active'"
        )
        annual = cur.fetchone()[0] or 0

    if not rows:
        print("no recurring charges found")
        return

    print(f"\n  {'merchant':<26} {'cadence':<12} {'amount':>9} {'/yr':>9} "
          f"{'n':>3} {'conf':>5}  next")
    for name, cad, cents, cv, n, conf, status, nxt in rows:
        tag = "" if status == "active" else f"  [{status}]"
        usage = " ~" if cv > USAGE_CV else ""
        print(f"  {name[:26]:<26} {cad:<12} {cents/100:>8,.2f}{usage} "
              f"{cents/100 * 365.25 / dict((c[0], c[1]) for c in CADENCES)[cad]:>8,.0f} "
              f"{n:>3} {conf:>5.2f}  {nxt}{tag}")
    print(f"\n  {'':<26} {'':<12} {'ACTIVE TOTAL':>9} {annual/100:>8,.0f} /year")
    print("  ~ = usage-based (amount varies), so no price tracking")

    # An unworked queue makes every total above too low, and nothing else on
    # screen would say so: those charges exist, they just aren't attached to a
    # merchant yet. A number that is wrong because a question is unanswered has
    # to admit it.
    with conn.cursor() as cur:
        cur.execute("SELECT count(*), coalesce(sum(txn_count), 0) FROM resolution_queue"
                    " WHERE status = 'pending'")
        n_queue, n_txn = cur.fetchone()
    if n_queue:
        print(f"\n  ⚠ {n_queue} descriptors ({n_txn} charges) are still in the review"
              f" queue, so these\n    totals are UNDERSTATED."
              f"  ->  python resolve.py --review")


def upcoming(conn, days: int = 30) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT max(posted_date) FROM raw_transaction")
        as_of = cur.fetchone()[0]
        cur.execute(
            "SELECT m.canonical_name, s.next_due, s.current_amount_cents "
            "FROM subscription s JOIN merchant m ON m.id = s.merchant_id "
            "WHERE s.status = 'active' AND s.next_due <= %s ORDER BY s.next_due",
            (as_of + timedelta(days=days),),
        )
        rows = cur.fetchall()
    print(f"\nnext {days} days after {as_of}:\n")
    total = 0
    for name, due, cents in rows:
        total += cents
        print(f"  {due}  {name[:30]:<30} {cents/100:>8,.2f}")
    print(f"\n  {'':<42} {total/100:>8,.2f} total")


def increases(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT m.canonical_name, p.effective_date, p.old_amount_cents,"
            "       p.new_amount_cents, p.pct_change, s.period_days "
            "FROM price_change p JOIN subscription s ON s.id = p.subscription_id "
            "JOIN merchant m ON m.id = s.merchant_id ORDER BY p.effective_date DESC"
        )
        rows = cur.fetchall()
    if not rows:
        print("no price changes detected")
        return
    print("\nprice changes:\n")
    for name, eff, old, new, pct, period in rows:
        extra = (new - old) / 100 * (365.25 / float(period))
        print(f"  {eff}  {name[:26]:<26} {old/100:>7,.2f} -> {new/100:>7,.2f}  "
              f"{pct:>+6.1f}%   {extra:>+7,.2f}/yr")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--upcoming", nargs="?", type=int, const=30, metavar="DAYS")
    ap.add_argument("--increases", action="store_true")
    args = ap.parse_args()

    with db.connect() as conn:
        if args.upcoming:
            upcoming(conn, args.upcoming)
        elif args.increases:
            increases(conn)
        else:
            n = detect_all(conn)
            print(f"{n} recurring series detected")
            report(conn)


if __name__ == "__main__":
    main()
