#!/usr/bin/env python3
"""Tiers 1-2 of the merchant resolution ladder, plus the human review queue.

    python resolve.py            resolve everything unresolved, print ladder stats
    python resolve.py --review   work the pending queue
    python resolve.py --groups   show what got grouped, to eyeball correctness

Tier 1  exact    scrubbed string already has an alias           O(1), free
Tier 2  fuzzy    RapidFuzz token_set_ratio vs known merchants   sub-ms, free
        queue    borderline, ambiguous or suspect -> a human decides
        new      nothing resembles it -> its own merchant

Tiers 3 (pgvector) and 4 (LLM) are deliberately absent. Build them when the
measured residue says they're needed, not before.

Every resolution, whatever tier produced it, writes a merchant_alias row. Tier 1
grows, the expensive tiers shrink. That ratio is the number worth charting.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter

from rapidfuzz import fuzz, process

import db

# ponytail: tuned against sample.csv only. Re-measure on a real statement.
FUZZY_ACCEPT = 90      # token_set_ratio we'll commit to without asking
FUZZY_BORDERLINE = 72  # below ACCEPT, above this -> ask a human
AMBIGUOUS_MARGIN = 4   # top two this close -> the score isn't evidence, ask
MIN_FUZZY_LEN = 4      # ratios are meaningless on 2-3 char strings

# token_set_ratio is blind to abbreviations ('AMZN' vs 'AMAZON' -> 61) and to
# missing spaces ('YOUTUBEPREMIUM' vs 'YOUTUBE PREMIUM' -> 65). partial_ratio
# catches both (91, 95). But it is permissive on short strings, so it is only
# ever allowed to raise a question, never to answer one.
PARTIAL_SUSPECT = 85

# Missing spaces defeat every token-based scorer at once: 'HBOMAX' vs
# 'WARNERMEDIA HBO MAX' scores 48 on token_set and 83 on partial -- under both
# thresholds, so it splits silently. Despaced, one contains the other.
MIN_CONTAINMENT = 5    # shorter than this and containment is coincidence


# --------------------------------------------------------------------------- #
# pure logic (no DB -- this is the part with tests)
# --------------------------------------------------------------------------- #

def rank(needle: str, haystack: list[str], scorer=fuzz.token_set_ratio,
         limit: int = 5) -> list[tuple[str, float]]:
    """Best matches for `needle`, highest score first."""
    if len(needle) < MIN_FUZZY_LEN or not haystack:
        return []
    hits = process.extract(needle, haystack, scorer=scorer, limit=limit)
    return [(name, score) for name, score, _ in hits]


def classify(needle: str, known: list[str]) -> tuple[str, list[tuple[str, float]]]:
    """-> ('match'|'ambiguous'|'borderline'|'suspect'|'unknown', candidates)

    Only 'match' is acted on automatically. Everything between confident and
    clearly-new goes to a human, because the failure this guards against is
    silent: two descriptors for one subscription become two subscriptions, and
    nothing in the output ever says so.
    """
    hits = rank(needle, known)
    if hits:
        top, top_score = hits[0]
        if len(hits) > 1 and top_score - hits[1][1] <= AMBIGUOUS_MARGIN \
                and hits[1][1] >= FUZZY_BORDERLINE:
            return "ambiguous", hits
        if top_score >= FUZZY_ACCEPT:
            return "match", hits
        if top_score >= FUZZY_BORDERLINE:
            return "borderline", hits

    # token_set said no. Ask the second signal whether it's an abbreviation or a
    # spacing artifact before accepting that this is a brand-new merchant.
    partials = rank(needle, known, scorer=fuzz.partial_ratio, limit=3)
    if partials and partials[0][1] >= PARTIAL_SUSPECT:
        return "suspect", partials

    held = contains(needle, known)
    if held:
        return "suspect", [(k, fuzz.partial_ratio(needle, k)) for k in held[:3]]

    return "unknown", hits


def contains(needle: str, known: list[str]) -> list[str]:
    """Known names that swallow `needle` (or are swallowed by it) once spaces
    are removed. Deterministic, and blind to how the merchant chose to space
    its own name."""
    n = needle.replace(" ", "")
    if len(n) < MIN_CONTAINMENT:
        return []
    out = []
    for k in known:
        ks = k.replace(" ", "")
        if len(ks) >= MIN_CONTAINMENT and (n in ks or ks in n):
            out.append(k)
    return out


# --------------------------------------------------------------------------- #
# DB glue
# --------------------------------------------------------------------------- #

def _merchant_id(cur, user_id: int, name: str) -> int:
    cur.execute(
        "INSERT INTO merchant (user_id, canonical_name) VALUES (%s, %s) "
        "ON CONFLICT (user_id, canonical_name) DO UPDATE "
        "SET canonical_name = EXCLUDED.canonical_name RETURNING id",
        (user_id, name),
    )
    return cur.fetchone()[0]


def _link(cur, user_id: int, scrubbed: str, merchant_id: int, tier: str,
          score: float | None) -> None:
    """Attach a scrubbed string to a merchant and record the alias, so this
    string never costs anything again."""
    cur.execute(
        "INSERT INTO merchant_alias (user_id, scrubbed_pattern, merchant_id,"
        " resolved_by, confidence) VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (user_id, scrubbed_pattern) DO NOTHING",
        (user_id, scrubbed, merchant_id, tier,
         None if score is None else round(score / 100, 3)),
    )
    cur.execute(
        "UPDATE raw_transaction SET merchant_id = %s WHERE scrubbed = %s AND merchant_id IS NULL",
        (merchant_id, scrubbed),
    )


def _enqueue(cur, user_id: int, scrubbed: str, n: int,
             hits: list[tuple[str, float]], reason: str) -> None:
    cur.execute(
        "INSERT INTO resolution_queue (user_id, scrubbed, txn_count, candidates,"
        " top_score, reason) VALUES (%s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (user_id, scrubbed) DO UPDATE SET txn_count = EXCLUDED.txn_count, "
        "candidates = EXCLUDED.candidates, top_score = EXCLUDED.top_score",
        (user_id, scrubbed, n, json.dumps([{"name": h, "score": s} for h, s in hits]),
         hits[0][1] if hits else None, reason),
    )


def resolve_all(conn, user_id: int) -> Counter:
    """Most frequent descriptor first, and every merchant created joins the
    known set immediately -- so the dominant spelling of a merchant anchors it,
    and the rarer variants are then compared against it instead of quietly
    becoming merchants of their own."""
    stats: Counter = Counter()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT scrubbed, count(*) FROM raw_transaction WHERE merchant_id IS NULL "
            "GROUP BY scrubbed ORDER BY count(*) DESC, scrubbed"
        )
        pending = cur.fetchall()
        if not pending:
            return stats

        cur.execute("SELECT scrubbed_pattern, merchant_id FROM merchant_alias")
        aliases = dict(cur.fetchall())
        cur.execute("SELECT canonical_name, id FROM merchant")
        merchants = dict(cur.fetchall())
        cur.execute("SELECT scrubbed FROM resolution_queue WHERE status = 'pending'")
        queued = {r[0] for r in cur.fetchall()}

        for s, n in pending:
            if s in aliases:                                   # tier 1
                _link(cur, user_id, s, aliases[s], "exact", None)
                stats["exact"] += n
                continue
            if s in queued:                                    # a human owns it
                stats["queued"] += n
                continue

            verdict, hits = classify(s, list(merchants))       # tier 2
            if verdict == "match":
                _link(cur, user_id, s, merchants[hits[0][0]], "fuzzy", hits[0][1])
                stats["fuzzy"] += n
            elif verdict in ("ambiguous", "borderline", "suspect"):
                _enqueue(cur, user_id, s, n, hits, verdict)
                queued.add(s)
                stats["queued"] += n
            else:
                mid = _merchant_id(cur, user_id, s)
                merchants[s] = mid
                _link(cur, user_id, s, mid, "exact", None)
                stats["new"] += n

        conn.commit()
    return stats


def print_stats(conn, stats: Counter) -> None:
    total = sum(stats.values())
    if not total:
        print("nothing to resolve")
    else:
        print(f"\nresolved {total} transactions this run:")
        for tier in ("exact", "fuzzy", "new", "queued"):
            if stats[tier]:
                print(f"  {tier:<8} {stats[tier]:>6}  {stats[tier] / total:>6.1%}")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FILTER (WHERE merchant_id IS NOT NULL), count(*) "
            "FROM raw_transaction"
        )
        done, all_ = cur.fetchone()
        cur.execute("SELECT count(*) FROM merchant")
        n_merchants = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM resolution_queue WHERE status = 'pending'")
        n_queue = cur.fetchone()[0]

    if not all_:
        print("\nno transactions loaded  ->  python ingest.py sample.csv --account demo")
        return
    print(f"\n{done}/{all_} transactions resolved without a model "
          f"({done / all_:.1%}) into {n_merchants} merchants")
    print(f"{n_queue} awaiting human review"
          + ("  ->  python resolve.py --review" if n_queue else ""))


def print_groups(conn) -> None:
    """Eyeball check: did the ladder group things a human would group?"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT m.canonical_name, array_agg(DISTINCT t.scrubbed), count(*) "
            "FROM raw_transaction t JOIN merchant m ON m.id = t.merchant_id "
            "GROUP BY m.canonical_name HAVING count(DISTINCT t.scrubbed) > 1 "
            "ORDER BY count(*) DESC"
        )
        rows = cur.fetchall()
    if not rows:
        print("no merchant has more than one descriptor variant yet")
        return
    print("\nmerchants with multiple descriptor variants (the tier-2 wins):\n")
    for name, variants, n in rows:
        print(f"  {name}  ({n} txns)")
        for v in sorted(variants):
            print(f"      {v}")


def review(conn, user_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, scrubbed, txn_count, candidates, reason FROM resolution_queue "
            "WHERE status = 'pending' ORDER BY txn_count DESC"
        )
        rows = cur.fetchall()
        if not rows:
            print("queue is empty")
            return

        for qid, scrubbed, n, candidates, reason in rows:
            print(f"\n  {scrubbed!r}  ({n} transactions, {reason})")
            for i, c in enumerate(candidates, 1):
                print(f"    {i}. {c['name']}  ({c['score']:.0f})")
            print("    n = new merchant, s = skip, q = quit")
            choice = input("  > ").strip().lower()

            if choice == "q":
                break
            if choice == "s":
                continue
            if choice == "n":
                mid = _merchant_id(cur, user_id, scrubbed)
            elif choice.isdigit() and 1 <= int(choice) <= len(candidates):
                mid = _merchant_id(cur, user_id, candidates[int(choice) - 1]["name"])
            else:
                print("    ?")
                continue

            # A human decision becomes a permanent rule, not a one-off answer.
            _link(cur, user_id, scrubbed, mid, "human", None)
            cur.execute(
                "UPDATE resolution_queue SET status = 'resolved' WHERE id = %s", (qid,)
            )
            conn.commit()
            print(f"    -> aliased; this string will never be asked about again")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--review", action="store_true", help="work the pending queue")
    ap.add_argument("--groups", action="store_true", help="show grouped variants")
    ap.add_argument("--user", type=int, required=True)
    args = ap.parse_args()

    db.open_pool()
    try:
        with db.tenant(args.user) as conn:
            if args.review:
                review(conn, args.user)
            elif args.groups:
                print_groups(conn)
            else:
                print_stats(conn, resolve_all(conn, args.user))
    finally:
        db.close_pool()


if __name__ == "__main__":
    main()
