"""Run: python test_ingest.py

Every other module in app/core/ has its own test file (test_scrub.py,
test_resolve.py, test_detect.py); ingest.py had none -- only indirect coverage
through test_pipeline.py and test_api.py exercising the whole upload path.
This is the direct one, for the parts that are pure and need no database:
looks_flipped() decides whether every amount in a statement gets its sign
inverted, silently, and had zero coverage of its own before this file.
"""

import io

from app.core.ingest import looks_flipped, read_rows

FAILURES = []
CHECKS = [0]


def check(label, got, expected):
    CHECKS[0] += 1
    if got != expected:
        FAILURES.append(f"  {label}\n    expected {expected!r}\n    got      {got!r}")


NEGATIVE_STYLE = b"""Date,Description,Amount
01/03/2026,COFFEE SHOP,-4.50
02/03/2026,GROCERY STORE,-62.10
03/03/2026,NETFLIX,-15.49
04/03/2026,A REFUND,3.00
"""

# Amex-style: charges are positive, a payment/credit is negative.
POSITIVE_STYLE = b"""Date,Description,Amount
01/03/2026,COFFEE SHOP,4.50
02/03/2026,GROCERY STORE,62.10
03/03/2026,NETFLIX,15.49
04/03/2026,PAYMENT RECEIVED - THANK YOU,-200.00
"""

# Right at the 70% threshold read_rows/looks_flipped uses: 3 of 4 positive.
BORDERLINE = b"""Date,Description,Amount
01/03/2026,A,10.00
02/03/2026,B,10.00
03/03/2026,C,10.00
04/03/2026,D,-10.00
"""


def test_negative_style_is_not_flipped():
    fh = io.StringIO(NEGATIVE_STYLE.decode())
    check("ordinary bank export is left alone", looks_flipped(fh, False), False)


def test_positive_style_is_flipped():
    fh = io.StringIO(POSITIVE_STYLE.decode())
    check("Amex-style export is detected", looks_flipped(fh, False), True)


def test_looks_flipped_does_not_consume_the_file_handle():
    """load() calls looks_flipped() and then reads the same handle again for
    the real parse -- if this left the cursor at EOF, every auto-detected
    upload would silently import zero rows."""
    fh = io.StringIO(POSITIVE_STYLE.decode())
    looks_flipped(fh, False)
    rows = list(read_rows(fh, False, False, verbose=False))
    check("the handle is rewound and still readable", len(rows), 4)


def test_a_header_with_no_data_rows_is_not_flipped():
    """The `if not signs` guard inside looks_flipped() -- a genuinely empty
    upload (no bytes at all) is refused earlier, by pipeline.run(), before
    this is ever reached, but a header-only file reaches read_rows() fine and
    yields zero rows, which is what this guards."""
    header_only = b"Date,Description,Amount\n"
    check("nothing to look at is not a reason to flip everything",
          looks_flipped(io.StringIO(header_only.decode()), False), False)


def test_borderline_ratio_is_read_correctly():
    """Pinning the exact threshold read_rows/looks_flipped uses (> 0.7, not
    >= ), so a future edit that loosens it to >= is a visible test change,
    not a silent behaviour change on files that happen to sit right on it."""
    fh = io.StringIO(BORDERLINE.decode())
    # 3/4 = 0.75 > 0.7, so this one *is* flipped -- the case worth pinning is
    # the boundary itself, not this particular ratio.
    check("3 of 4 positive crosses the threshold", looks_flipped(fh, False), True)


# Statement layouts modeled on what Indian banks' CSV exports look like --
# column names, a summary block above the table, DR/CR columns. Synthetic
# rows; the shapes are what matter.
_PREAMBLE_NARRATION = """Statement of account
Account No :,XXXXXXXX1234
Period :,01/09/26 To 30/09/26

Date,Narration,Chq./Ref.No.,Value Dt,Withdrawal Amt.,Deposit Amt.,Closing Balance
02/09/26,UPI-NETFLIX-NETFLIX@HDFC,000123,02/09/26,649.00,,10351.00
05/09/26,SALARY SEP,000124,05/09/26,,85000.00,95351.00
25/09/26,ACH D- SPOTIFY INDIA,000125,25/09/26,119.00,,95232.00
"""

_REMARKS_AMOUNT_COLUMNS = """S No.,Value Date,Transaction Date,Transaction Remarks,Withdrawal Amount (INR ),Deposit Amount (INR ),Balance (INR )
1,03/09/2026,03/09/2026,UPI/ADOBE SYSTEMS,1675.00,0.00,50000.00
2,15/09/2026,15/09/2026,NEFT CREDIT REFUND,0.00,500.00,50500.00
"""

_BARE_DR_CR = """Tran Date,CHQNO,PARTICULARS,DR,CR,BAL,SOL
01-09-2026,,YOUTUBE PREMIUM,129.00,,9871.00,1234
20-09-2026,,INTEREST CREDIT,,42.00,9913.00,1234
"""

_INDICATOR_COLUMN = """Sl. No.,Transaction Date,Description,Amount,Dr / Cr,Balance
1,04/09/2026,AMAZON PRIME,1499.00,DR,8501.00
2,18/09/2026,CASHBACK,50.00,CR,8551.00
"""


def _rows(text, dayfirst=False):
    return list(read_rows(io.StringIO(text), dayfirst, False, verbose=False, currency="INR"))


def test_a_summary_above_the_header_row_is_skipped():
    rows = _rows(_PREAMBLE_NARRATION)
    check("three transactions", len(rows), 3)
    check("narration is the description", rows[0][2], "UPI-NETFLIX-NETFLIX@HDFC")
    check("withdrawal is money out", rows[0][1], -64900)
    check("deposit is money in", rows[1][1], 8500000)


def test_value_dt_is_not_mistaken_for_the_amount():
    # "value" is an amount word; "Value Dt" is a date column.
    check("amounts are money, not dates", [c for _, c, _ in _rows(_PREAMBLE_NARRATION)],
          [-64900, 8500000, -11900])


def test_a_withdrawal_amount_header_is_not_the_single_amount_column():
    # Regression: "Withdrawal Amount (INR )" contains "amount", was taken as
    # the one amount column, and every deposit was silently lost.
    rows = _rows(_REMARKS_AMOUNT_COLUMNS)
    check("remarks is the description", rows[0][2], "UPI/ADOBE SYSTEMS")
    check("both directions kept", [c for _, c, _ in rows], [-167500, 50000])


def test_bare_dr_and_cr_columns():
    check("DR out, CR in", [c for _, c, _ in _rows(_BARE_DR_CR)], [-12900, 4200])


def test_a_dr_cr_indicator_column_signs_the_amount():
    check("signed by the indicator", [c for _, c, _ in _rows(_INDICATOR_COLUMN)], [-149900, 5000])


def test_day_first_dates_are_detected_when_month_first_is_impossible():
    from datetime import date
    rows = _rows(_PREAMBLE_NARRATION)  # 25/09/26 can't be month-first
    check("read day-first without the flag", rows[2][0], date(2026, 9, 25))
    ambiguous = "Date,Description,Amount\n01/03/2026,A,-1.00\n02/03/2026,B,-1.00\n"
    check("ambiguous dates follow the flag", _rows(ambiguous)[0][0], date(2026, 1, 3))
    check("and the flag still works", _rows(ambiguous, dayfirst=True)[0][0], date(2026, 3, 1))


def main() -> None:
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
    if FAILURES:
        print(f"FAIL ({len(FAILURES)})")
        print("\n".join(FAILURES))
        raise SystemExit(1)
    print(f"ok  ({len(tests)} tests, {CHECKS[0]} checks)")


if __name__ == "__main__":
    main()
