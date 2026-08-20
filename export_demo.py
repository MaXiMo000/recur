#!/usr/bin/env python3
"""Freeze the API's responses into a static JSON file for the public demo.

    python export_demo.py

The deployed demo has no backend and no database. It is this one file plus the
built React app on a CDN, which means the public build has no endpoint to
attack, no credential to leak and nothing to rate-limit.

REFUSES TO RUN ON REAL DATA. The demo is generated from sample.csv, whose
merchants are known; anything else is presumed to be a real statement and is
rejected rather than published. Shipping your own transactions to a CDN because
a build script was too obliging is not a mistake worth being able to make.
"""

from __future__ import annotations

import json
import pathlib
import sys

import api

OUT = pathlib.Path(__file__).with_name("web") / "public" / "demo.json"

# Every merchant sample.csv can produce. If the database holds anything else,
# it isn't the sample.
SAMPLE_MERCHANTS = {
    "NETFLIX", "APPLE BILL", "HBOMAX", "AMAZON PRIME", "GOOGLE HULU", "SUPER",
    "ZXC SITE ACCESS", "NAMECHEAP ORDER", "CLAUDE AI SUBSCRIPTION",
    "AWS EMEA AWS AMAZON", "AMZN MKTP US", "COSTCO WHSE MIAMI", "PUBLIX MIAMI",
    "BLUE BOTTLE COFFEE", "PHILZ COFFEE", "TARTINE BAKERY", "CONVENIENT STORE",
    "7 ELEVEN FREMONT", "SAFEWAY FREMONT", "CHEVRON UNION CITY",
    "IN N OUT BURGER", "TARGET T UNION CITY", "VENMO PAYMENT", "ETSY INC",
    "SHELL OIL", "TRADER JOE S FREMONT", "WARNERMEDIA HBO MAX", "HBO MAX",
    "AMAZON WEB SERVICES AWS AMAZON", "AMZN PRIME MEMBERSHIP",
}


def main() -> None:
    subs = api.subscriptions()
    if not subs:
        sys.exit("no subscriptions -- run ./run_all.sh first")

    unknown = {r["merchant"] for r in subs} - SAMPLE_MERCHANTS
    if unknown:
        sys.exit(
            "REFUSING to export: the database contains merchants that are not in\n"
            "sample.csv, so this looks like a real statement:\n"
            + "".join(f"    {m}\n" for m in sorted(unknown))
            + "\nRebuild the demo database first:\n"
              "    ./run_all.sh sample.csv demo\n"
        )

    payload = {
        "generated_from": "sample.csv (synthetic; see make_sample.py)",
        "summary": api.summary(),
        "subscriptions": subs,
        "upcoming": api.upcoming(days=30),
        "increases": api.increases(),
        "history": {str(r["id"]): api.history(r["id"]) for r in subs},
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=1, default=str))
    kb = OUT.stat().st_size / 1024
    print(f"wrote {OUT.relative_to(pathlib.Path.cwd())}  ({kb:.0f} KB, "
          f"{len(subs)} subscriptions, synthetic data only)")


if __name__ == "__main__":
    main()
