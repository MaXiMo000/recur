"""Tier 0 of the merchant resolution ladder: deterministic descriptor scrubbing.

    'SP * NETFLIX.COM 866-579-7172'    -> 'NETFLIX'
    'TST* BLUE BOTTLE 0091 OAKLAND CA' -> 'BLUE BOTTLE OAKLAND'

The leftover city is deliberate -- see the note at the bottom of this file.

Pure and cheap. Every piece of noise removed here is noise the fuzzy, vector and
LLM tiers would otherwise pay to ignore. Nothing in this file makes a network
call or a guess -- if it can't be done with a rule, it belongs in a later tier.
"""

from __future__ import annotations

import re

# Card-network and payment-processor junk that prefixes the real merchant name.
# Applied repeatedly, because they stack: "POS DEBIT SQ *BLUE BOTTLE".
_PREFIX = re.compile(
    r"""^(?:
        sp|sq|tst|pp|paypal|py|dd|ec|in|wl|ig      # processor initials before a '*'
      | bt|psp|iso                                 # Braintree, aggregators
      )\s*\*+\s*
    | ^(?:
        pos\s+(?:debit|purchase|pur)
      | debit\s+card\s+(?:purchase|pur)
      | credit\s+card\s+purchase
      | checkcard
      | visa\s+dda\s+pur
      | ach\s+(?:debit|credit|web|pmt)
      | recurring\s+(?:payment|debit|card\s+purchase)
      | preauthorized\s+(?:debit|payment)
      | purchase\s+authorized\s+on\s+\d{1,2}/\d{1,2}
      | paypal\s+(?:inst\s+)?xfer
      | web\s+id:?\s*\d+
      | ext\s+trnsfr
      )\s+
    """,
    re.I | re.X,
)

# Trailing junk, stripped repeatedly from the right.
_SUFFIX = [
    re.compile(r"\s+\d{3}[-.\s]?\d{3}[-.\s]?\d{4}$"),   # 866-579-7172
    re.compile(r"\s+\d{8,}$"),                          # order / auth ids
    re.compile(r"\s+#\s*\w+$"),                         # #1234, #A17
    re.compile(r"\s+\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?$"),  # embedded dates
    re.compile(r"\s+(?:usa?|can?)$", re.I),             # country
    re.compile(r"\s+x{2,}\d+$", re.I),                  # XX1234 card refs
    re.compile(r"\s+(?:ref|auth|inv|id|trn)[:#]?\s*\w+$", re.I),
]

# Stripped only as a *trailing token* -- "CA" after a city is a state, but
# "CA" alone could be a merchant, so a bare one-token descriptor is left alone.
_STATES = frozenset(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS "
    "MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV "
    "WI WY DC PR VI GU".split()
)

_TLD = re.compile(r"\.(?:com|net|org|co|io|app|us|gov|edu)\b", re.I)


def scrub(descriptor: str) -> str:
    """Normalize a bank descriptor to a bare merchant string. Idempotent."""
    s = descriptor.upper().strip()

    # Processor prefixes stack; peel until stable.
    for _ in range(5):
        s, n = _PREFIX.subn("", s, count=1)
        if not n:
            break
        s = s.strip()

    s = _TLD.sub("", s)

    # Trailing junk also stacks: "... 0091 OAKLAND CA 866-579-7172".
    for _ in range(6):
        before = s
        for pat in _SUFFIX:
            s = pat.sub("", s).strip()
        s = _strip_state(s)
        if s == before:
            break

    # Anything left that isn't a letter, digit or space is a separator.
    s = re.sub(r"[^A-Z0-9 ]+", " ", s)
    tokens = s.split()

    # Store numbers hide mid-string ("BLUE BOTTLE 0091 OAKLAND"). Any pure-numeric
    # token is one -- except a short leading one, which is part of the name
    # ("7 ELEVEN", "24 HOUR FITNESS", "76 GAS"). A *long* leading number is a
    # date or card fragment the prefix rules didn't eat ("CHECKCARD 0803 ADOBE").
    tokens = [
        t for i, t in enumerate(tokens)
        if not t.isdigit() or (i == 0 and len(t) <= 2)
    ]

    # Order/auth ids ('AMZN MKTP US*2K4LM9DX3') are unique per transaction, so
    # left in place every charge becomes its own merchant. Never the first
    # token, so names that legitimately mix digits and letters ('1800FLOWERS')
    # survive.
    tokens = [t for i, t in enumerate(tokens) if i == 0 or not _is_id_token(t)]

    return " ".join(tokens)


def _is_id_token(t: str) -> bool:
    return len(t) >= 5 and any(c.isdigit() for c in t) and any(c.isalpha() for c in t)


def _strip_state(s: str) -> str:
    """Drop a trailing US state code, but never reduce a descriptor to nothing."""
    parts = s.rsplit(maxsplit=1)
    if len(parts) == 2 and parts[1] in _STATES:
        return parts[0].strip()
    return s


# ponytail: no city gazetteer. A leftover city token ("BLUE BOTTLE OAKLAND")
# is harmless -- RapidFuzz token_set_ratio in tier 2 ignores extra tokens.
# Add one only if the tier-2 miss rate says it matters.
