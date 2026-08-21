"""Run: python test_resolve.py

The ladder's pure logic. The DB glue is boring; these three functions are where
a wrong answer silently corrupts data, so this is what gets checked.
"""

from app.core.resolve import classify, contains

KNOWN = ["NETFLIX", "SPOTIFY", "AMAZON WEB SERVICES", "PHILZ COFFEE", "BLUE BOTTLE"]


def check(label, got, expected):
    if got != expected:
        return f"  {label}\n    expected {expected!r}\n    got      {got!r}"
    return None


CHECKS = 9


def main() -> None:
    f = []

    # ---- classify: commits when confident
    v, _ = classify("NETFLIX ASSOCIATES", KNOWN)
    f.append(check("subset name matches its merchant", v, "match"))

    # ---- classify: refuses when it can't tell them apart
    v, hits = classify("COFFEE", ["PHILZ COFFEE", "BLUE BOTTLE COFFEE"])
    f.append(check("two near-equal candidates are ambiguous, not a guess",
                   v, "ambiguous"))

    # ---- classify: a genuinely new merchant is not forced into an existing one
    v, _ = classify("TRADER JOES", KNOWN)
    f.append(check("unrelated string stays unknown", v, "unknown"))

    # ---- classify: too short to fuzzy-match on
    v, _ = classify("CA", KNOWN)
    f.append(check("2-char string is not fuzzy-matched", v, "unknown"))

    # ---- the two real misses found on sample data: token_set scores these 61
    # and 65, well under BORDERLINE. They must reach a human, not be silently
    # turned into separate merchants.
    v, _ = classify("AMZN PRIME MEMBERSHIP", ["AMAZON PRIME", "CHEVRON"])
    f.append(check("abbreviation is queued, not silently split", v, "suspect"))

    v, _ = classify("GOOGLE YOUTUBEPREMIUM", ["GOOGLE YOUTUBE PREMIUM", "CHEVRON"])
    f.append(check("missing space is queued, not silently split", v, "suspect"))

    # ---- but a genuinely new merchant must still auto-create, or the queue
    # fills with every coffee shop and nobody ever works it.
    v, _ = classify("TRADER JOES", ["NETFLIX", "SPOTIFY", "CHEVRON"])
    f.append(check("unrelated merchant still auto-creates", v, "unknown"))

    # ---- the missing-space class: defeats token_set (48) AND partial (83),
    # so containment on the despaced strings is the only thing that catches it.
    v, _ = classify("HBOMAX", ["WARNERMEDIA HBO MAX", "CHEVRON"])
    f.append(check("despaced containment is queued, not silently split",
                   v, "suspect"))

    f.append(check("containment needs real length, not a 3-char coincidence",
                   contains("AWS", ["AWS EMEA", "LAWSON"]), []))

    f = [x for x in f if x]
    if f:
        print(f"FAIL ({len(f)})")
        print("\n".join(f))
        raise SystemExit(1)
    print(f"ok  ({CHECKS} checks)")


if __name__ == "__main__":
    main()
