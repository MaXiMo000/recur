"""Run: python test_detect.py

Periodicity is where a plausible-looking wrong answer is most dangerous: a
misread cadence silently multiplies or divides someone's annual total. These
are the cases that break naive gap arithmetic.
"""

from datetime import date, timedelta

from detect import (coefficient_of_variation, fit_cadence, next_occurrence,
                    price_changes, status_of)

FAILURES = []


def check(label, got, expected):
    if got != expected:
        FAILURES.append(f"  {label}\n    expected {expected!r}\n    got      {got!r}")


def monthly_dates(y, m, n, dom):
    import calendar
    out = []
    for i in range(n):
        mm = m + i
        yy, mm = y + (mm - 1) // 12, (mm - 1) % 12 + 1
        out.append(date(yy, mm, min(dom, calendar.monthrange(yy, mm)[1])))
    return out


def main() -> None:
    # ---- monthly on the 31st: gaps are 28..31 days, which is NOT 30.44 apart,
    # but the day-of-month is rock stable. This is the case a gap-only fit gets
    # wrong.
    d = monthly_dates(2025, 9, 12, 31)
    check("month-end series is monthly", fit_cadence(d)[0], "monthly")

    # ---- and the anchor survives the February clamp
    check("anchor day is the 31st, not February's 28", fit_cadence(d)[3], 31)

    # ---- a genuine 28th-of-month series must not be mistaken for month-end
    check("28th-of-month is monthly too", fit_cadence(monthly_dates(2025, 9, 12, 28))[0],
          "monthly")

    # ---- weekly / annual endpoints
    weekly = [date(2026, 1, 5) + timedelta(days=7 * i) for i in range(10)]
    check("weekly", fit_cadence(weekly)[0], "weekly")
    check("two points a year apart", fit_cadence([date(2025, 3, 4), date(2026, 3, 6)])[0],
          "annual")

    # ---- a failed payment leaves a ~2x gap. That's a missed charge, not a
    # different cadence and not the end of the series.
    skipped = monthly_dates(2025, 9, 12, 9)
    del skipped[3]
    check("skipped payment stays monthly", fit_cadence(skipped)[0], "monthly")

    # ---- random one-off purchases are not a subscription
    noise = [date(2025, 9, 2), date(2025, 9, 3), date(2025, 10, 19),
             date(2025, 11, 27), date(2026, 1, 3)]
    check("irregular purchases have no cadence", fit_cadence(noise), None)

    # ---- fewer than two points can't be a series
    check("single charge is not a subscription", fit_cadence([date(2026, 1, 1)]), None)

    # ---- price step: 7 months at 2499 then 5 at 2699
    pts = list(zip(monthly_dates(2025, 9, 12, 3), [2499] * 7 + [2699] * 5))
    changes = price_changes(pts)
    check("one price step found", len(changes), 1)
    check("step is old -> new", (changes[0][1], changes[0][2]), (2499, 2699))

    # ---- a single prorated charge is not a price change, and must not move
    # the baseline either
    pts = list(zip(monthly_dates(2025, 9, 6, 3), [999, 999, 450, 999, 999, 999]))
    check("unconfirmed one-off is ignored", price_changes(pts), [])

    # ---- usage-based amounts are not price changes
    check("steady amounts have low CV", coefficient_of_variation([1599] * 8) < 0.02, True)
    check("usage amounts have high CV",
          coefficient_of_variation([350, 6210, 1180, 4400, 900]) > 0.02, True)

    # ---- month-end clamping in the forecast: Jan 31 -> Feb 28, not Mar 3
    check("next monthly occurrence clamps to month length",
          next_occurrence(date(2026, 1, 31), "monthly", 30.44, 31), date(2026, 2, 28))

    # ---- status is measured against the newest data, not today
    check("recent charge is active",
          status_of(date(2026, 8, 3), date(2026, 8, 20), 30.44), "active")
    check("two months silent is lapsed",
          status_of(date(2026, 6, 15), date(2026, 8, 20), 30.44), "lapsed")
    check("four months silent is cancelled",
          status_of(date(2026, 3, 1), date(2026, 8, 20), 30.44), "cancelled")

    if FAILURES:
        print(f"FAIL ({len(FAILURES)})")
        print("\n".join(FAILURES))
        raise SystemExit(1)
    print("ok  (17 checks)")


if __name__ == "__main__":
    main()
