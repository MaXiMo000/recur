"""Run: python test_scrub.py

The scrub is the one piece of week-1 logic that can be silently wrong, so it's
the one piece with a check. Real descriptors, hand-checked expectations.
"""

from app.core.scrub import scrub

CASES = [
    # processor prefixes
    ("SP * NETFLIX.COM 866-579-7172", "NETFLIX"),
    ("NETFLIX.COM", "NETFLIX"),
    ("Netflix 1 800 associates", "NETFLIX ASSOCIATES"),
    ("SQ *BLUE BOTTLE COFFEE", "BLUE BOTTLE COFFEE"),
    ("TST* BLUE BOTTLE 0091 OAKLAND CA", "BLUE BOTTLE OAKLAND"),
    ("PAYPAL *SPOTIFY USA", "SPOTIFY"),
    ("POS DEBIT SQ *PHILZ COFFEE", "PHILZ COFFEE"),
    ("CHECKCARD 0803 ADOBE SYSTEMS 408-536-6000 CA", "ADOBE SYSTEMS"),
    ("PURCHASE AUTHORIZED ON 08/03 AMZN MKTP US*2K4LM SEATTLE WA",
     "AMZN MKTP US SEATTLE"),
    # trailing junk
    ("SPOTIFY USA #4429", "SPOTIFY"),
    ("GITHUB.COM REF#88213", "GITHUB"),
    ("CLAUDE.AI SUBSCRIPTION 000123456789", "CLAUDE AI SUBSCRIPTION"),
    # leading numbers are part of the name, not store numbers
    ("7 ELEVEN 33812 FREMONT CA", "7 ELEVEN FREMONT"),
    ("24 HOUR FITNESS 0421", "24 HOUR FITNESS"),
    # a bare state code is a merchant, not a state
    ("CA", "CA"),
    # idempotent: scrubbing a scrubbed string changes nothing
    ("AT&T *PAYMENT 800-288-2020 TX", "AT T PAYMENT"),
    # Non-Latin merchant names must survive. An [A-Z0-9] filter deletes these
    # outright; an isalnum() filter shreds Devanagari, because a vowel sign is
    # a combining mark and isalnum() is False for it.
    ("\u30e1\u30eb\u30ab\u30ea", "\u30e1\u30eb\u30ab\u30ea"),
    ("\u0410\u043f\u0442\u0435\u043a\u0430 \u0420\u0438\u0433\u043b\u0430",
     "\u0410\u041f\u0422\u0415\u041a\u0410 \u0420\u0418\u0413\u041b\u0410"),
    ("\u0928\u0947\u091f\u092b\u094d\u0932\u093f\u0915\u094d\u0938",
     "\u0928\u0947\u091f\u092b\u094d\u0932\u093f\u0915\u094d\u0938"),
    ("\u652f\u4ed8\u5b9d-\u7f51\u6613\u4e91\u97f3\u4e50",
     "\u652f\u4ed8\u5b9d \u7f51\u6613\u4e91\u97f3\u4e50"),
]

# Half the world writes 1.234,56 for what the US writes as 1,234.56. Getting
# this wrong is silent and off by a factor of a thousand.
AMOUNTS = [
    ("$1,234.56", 123456), ("1.234,56", 123456), ("1 234,56", 123456),
    ("\u00a345.99", 4599), ("\u20b91,299.00", 129900), ("(45.00)", -4500),
    ("-1.234,56", -123456), ("1,50", 150), ("1,500", 150000), ("45.99", 4599),
    ("", None), ("--", None),
]

# Currency is not always hundredths. Yen has no fractional part, dinars have
# three digits, and reading either as hundredths is off by 100x or 10x.
AMOUNTS_BY_CURRENCY = [
    ("15.49", "USD", 1549),
    ("1,234.56", "USD", 123456),
    ("1.234", "USD", 123400),      # three digits after a dot: thousands, in USD
    ("1200", "JPY", 1200),         # 1200 yen, not 12.00
    ("1,200", "JPY", 1200),
    ("12.00", "JPY", 12),          # exporter-added decimals, not 1200 yen
    ("1,234,567", "JPY", 1234567),
    ("1.234", "KWD", 1234),        # three digits after a dot: a real fraction
    ("1.234,56", "EUR", 123456),
    ("(45.00)", "USD", -4500),
]


def main() -> None:
    from app.core.ingest import parse_amount
    failures = []
    for raw, want in AMOUNTS:
        got = parse_amount(raw)
        if got != want:
            failures.append(f"  amount {raw!r}\n    expected {want!r}\n    got      {got!r}")
    for raw, cur, want in AMOUNTS_BY_CURRENCY:
        got = parse_amount(raw, cur)
        if got != want:
            failures.append(
                f"  amount {raw!r} in {cur}\n    expected {want!r}\n    got      {got!r}")
    for raw, expected in CASES:
        got = scrub(raw)
        if got != expected:
            failures.append(f"  {raw!r}\n    expected {expected!r}\n    got      {got!r}")
        elif scrub(got) != got:
            failures.append(f"  not idempotent: {raw!r} -> {got!r} -> {scrub(got)!r}")

    if failures:
        print(f"FAIL ({len(failures)})")
        print("\n".join(failures))
        raise SystemExit(1)
    print(f"ok  ({len(CASES)} descriptors, {len(AMOUNTS)} amounts, "
          f"{len(AMOUNTS_BY_CURRENCY)} currency amounts)")


if __name__ == "__main__":
    main()
