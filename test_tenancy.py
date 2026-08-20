"""Run: python test_tenancy.py

The single most important test in this repo. Everything else being correct is
worth nothing if one user can read another's bank transactions.

These are deliberately *adversarial*: they don't check that a well-behaved query
is scoped, they check that a deliberately unscoped one -- no WHERE, an explicit
foreign id, a forged setting -- still returns nothing.
"""

import psycopg

import db

FAILURES = []


def check(label, got, expected):
    if got != expected:
        FAILURES.append(f"  {label}\n    expected {expected!r}\n    got      {got!r}")


def make_user(email: str) -> int:
    with db.admin() as conn:
        row = conn.execute(
            "INSERT INTO app_user (email, password_hash) VALUES (%s, 'x') "
            "ON CONFLICT (email) DO UPDATE SET email = EXCLUDED.email RETURNING id",
            (email,),
        ).fetchone()
        conn.commit()
        return row[0]


def seed(user_id: int, label: str, merchant: str) -> None:
    with db.tenant(user_id) as conn:
        acct = conn.execute(
            "INSERT INTO account (user_id, label) VALUES (%s, %s) "
            "ON CONFLICT (user_id, label) DO UPDATE SET label = EXCLUDED.label "
            "RETURNING id", (user_id, label)).fetchone()[0]
        merch = conn.execute(
            "INSERT INTO merchant (user_id, canonical_name) VALUES (%s, %s) "
            "ON CONFLICT (user_id, canonical_name) DO UPDATE "
            "SET canonical_name = EXCLUDED.canonical_name RETURNING id",
            (user_id, merchant)).fetchone()[0]
        conn.execute(
            "INSERT INTO raw_transaction (user_id, account_id, posted_date,"
            " amount_cents, raw_descriptor, scrubbed, merchant_id, dedup_hash) "
            "VALUES (%s,%s,'2026-01-05',-1599,%s,%s,%s,%s) "
            "ON CONFLICT (user_id, dedup_hash) DO NOTHING",
            (user_id, acct, merchant, merchant, merch, f"hash-{user_id}"))
        conn.commit()


def main() -> None:
    # Schema and the application role must exist before the pool, which
    # connects as that role, is opened.
    db.apply_schema()
    db.open_pool()
    try:
        alice = make_user("alice@example.com")
        bob = make_user("bob@example.com")
        seed(alice, "alice-chase", "ALICE SECRET THERAPY")
        seed(bob, "bob-amex", "BOB GAMBLING SITE")

        # --- an unscoped SELECT returns only your own rows
        with db.tenant(alice) as conn:
            rows = conn.execute("SELECT scrubbed FROM raw_transaction").fetchall()
        check("unscoped SELECT is still tenant-scoped",
              [r[0] for r in rows], ["ALICE SECRET THERAPY"])

        # --- naming the other tenant explicitly returns nothing
        with db.tenant(alice) as conn:
            rows = conn.execute(
                "SELECT scrubbed FROM raw_transaction WHERE user_id = %s", (bob,)
            ).fetchall()
        check("asking for another user's id by number returns nothing", rows, [])

        # --- a join can't be used to walk out of the tenant either
        with db.tenant(alice) as conn:
            rows = conn.execute(
                "SELECT m.canonical_name FROM merchant m "
                "JOIN raw_transaction t ON t.merchant_id = m.id"
            ).fetchall()
        check("joins stay inside the tenant",
              [r[0] for r in rows], ["ALICE SECRET THERAPY"])

        # --- writing a row branded as another user is rejected by WITH CHECK
        wrote_as_bob = False
        with db.tenant(alice) as conn:
            try:
                conn.execute(
                    "INSERT INTO merchant (user_id, canonical_name) VALUES (%s, %s)",
                    (bob, "PLANTED BY ALICE"))
                conn.commit()
                wrote_as_bob = True
            except psycopg.errors.Error:
                conn.rollback()
        check("cannot insert a row owned by another user", wrote_as_bob, False)

        # --- and cannot reassign one of your own rows to someone else
        moved = False
        with db.tenant(alice) as conn:
            try:
                conn.execute("UPDATE merchant SET user_id = %s", (bob,))
                conn.commit()
                moved = True
            except psycopg.errors.Error:
                conn.rollback()
        check("cannot hand a row to another user", moved, False)

        # --- deleting another tenant's data affects zero rows
        with db.tenant(alice) as conn:
            n = conn.execute("DELETE FROM raw_transaction WHERE user_id = %s",
                             (bob,)).rowcount
            conn.rollback()
        check("cannot delete another user's transactions", n, 0)

        # --- with no tenant set, the tenant tables are empty, not open
        with db.admin() as conn:
            n = conn.execute("SELECT count(*) FROM raw_transaction").fetchone()[0]
        check("no tenant set means no rows, not all rows", n, 0)

        # --- the pool must not carry a tenant into the next borrower
        with db.tenant(bob) as conn:
            conn.execute("SELECT 1")
        with db.admin() as conn:
            leaked = conn.execute(
                "SELECT current_setting('recur.user_id', true)").fetchone()[0]
        check("pool reset clears the tenant between checkouts", leaked in (None, ""), True)

        # --- deleting the user removes the data (GDPR erasure, by cascade)
        with db.admin() as conn:
            conn.execute("DELETE FROM app_user WHERE id = %s", (bob,))
            conn.commit()
        with db.tenant(bob) as conn:
            n = conn.execute("SELECT count(*) FROM raw_transaction").fetchone()[0]
        check("deleting a user erases their transactions", n, 0)

        with db.admin() as conn:
            conn.execute("DELETE FROM app_user WHERE email LIKE '%@example.com'")
            conn.commit()
    finally:
        db.close_pool()

    if FAILURES:
        print(f"FAIL ({len(FAILURES)})")
        print("\n".join(FAILURES))
        raise SystemExit(1)
    print("ok  (9 isolation checks)")


if __name__ == "__main__":
    main()
