"""Run: python test_scrub.py

The scrub is the one piece of week-1 logic that can be silently wrong, so it's
the one piece with a check. Real descriptors, hand-checked expectations.
"""

from scrub import scrub

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
]


def main() -> None:
    failures = []
    for raw, expected in CASES:
        got = scrub(raw)
        if got != expected:
            failures.append(f"  {raw!r}\n    expected {expected!r}\n    got      {got!r}")
        elif scrub(got) != got:
            failures.append(f"  not idempotent: {raw!r} -> {got!r} -> {scrub(got)!r}")

    if failures:
        print(f"FAIL ({len(failures)}/{len(CASES)})")
        print("\n".join(failures))
        raise SystemExit(1)
    print(f"ok  ({len(CASES)} cases)")


if __name__ == "__main__":
    main()
