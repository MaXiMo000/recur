"""Currency handling, because getting this wrong produces confidently wrong money.

Two separate mistakes live here, and this module exists because both were made:

  1. **Not every currency has two decimal places.** ¥1200 is one thousand two
     hundred yen, not twelve. Multiplying by 100 the way you would for dollars
     inflates a Japanese statement by a factor of a hundred, silently.
  2. **Different currencies cannot be added together.** A USD subscription and
     a JPY one summed into a single "annual total" is not an approximation, it
     is a meaningless number presented as a fact.

So amounts are stored in each currency's own minor unit, and every total is
reported per currency. There is no conversion: doing FX would need a rate
source, a rate date per transaction, and an answer to "which day's rate" that
nobody agrees on. Reporting each currency separately is correct and needs none
of that.
"""

from __future__ import annotations

# ISO 4217 currencies with no minor unit. The amount as written IS the integer.
ZERO_DECIMAL = frozenset({
    "BIF", "CLP", "DJF", "GNF", "JPY", "KMF", "KRW", "MGA", "PYG",
    "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF",
})

# Three minor digits rather than two.
THREE_DECIMAL = frozenset({"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"})


def minor_units(currency: str) -> int:
    """How many digits after the decimal point this currency actually has."""
    c = (currency or "USD").upper()
    if c in ZERO_DECIMAL:
        return 0
    if c in THREE_DECIMAL:
        return 3
    return 2


def to_minor(amount: float, currency: str) -> int:
    """A decimal amount as written on a statement -> that currency's integer
    minor unit. 15.49 USD -> 1549. 1200 JPY -> 1200. 1.234 KWD -> 1234."""
    return round(amount * (10 ** minor_units(currency)))


def format(units: int, currency: str) -> str:
    """For CLI output. The frontend uses Intl.NumberFormat, which knows this
    per-currency rule already."""
    digits = minor_units(currency)
    value = units / (10 ** digits) if digits else units
    return f"{value:,.{digits}f} {currency.upper()}"
