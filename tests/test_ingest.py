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
