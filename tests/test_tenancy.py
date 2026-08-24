"""Run: python test_tenancy.py

The single most important test in this repo. Everything else being correct is
worth nothing if one user can read another's bank transactions.

These are deliberately *adversarial*: they don't check that a well-behaved query
is scoped, they check that a deliberately unscoped one -- no WHERE, an explicit
foreign id -- still returns nothing.

What RLS here does NOT defend against, measured rather than assumed: a
connection that can run arbitrary SQL as the app role can call set_config and
re-point `recur.user_id` at another tenant. That is pinned by
`check_forged_setting_is_a_known_limit` below so the boundary stays written
down instead of imagined. The controls that make it moot are the ones asserted
here -- tenant() refuses anything that is not a positive int, and every caller
parameterizes -- plus the role attributes and FORCE ROW LEVEL SECURITY that
schema.sql warns can be silently inert while pg_class still reports
relrowsecurity = true.
"""

import psycopg

from app import db

FAILURES = []
CHECKS = [0]

# The seven tables RLS is armed on in schema.sql. Listed here on purpose: if a
# table is added there and not here, the coverage gap is visible rather than
# implicit.
TENANT_TABLES = (
    "account", "merchant", "merchant_alias", "raw_transaction",
    "resolution_queue", "subscription", "price_change",
)


def check(label, got, expected):
    CHECKS[0] += 1
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

        # --- the controls themselves, not just their effects -----------------
        # schema.sql warns that connecting as a privileged user leaves every
        # policy inert "while pg_class still cheerfully reports
        # relrowsecurity = true". That is a configuration failure no isolation
        # test above would name -- they would just start failing, mysteriously.
        # These assert the controls are genuinely in force.

        with db.tenant(alice) as conn:
            role, is_super, bypasses = conn.execute(
                "SELECT current_user,"
                " (SELECT usesuper FROM pg_user WHERE usename = current_user),"
                " (SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user)"
            ).fetchone()
            owner = conn.execute(
                "SELECT pg_get_userbyid(relowner) FROM pg_class "
                "WHERE relname = 'raw_transaction'").fetchone()[0]
        check("the app connects as the unprivileged role", role, db.APP_ROLE)
        check("which is not a superuser -- nothing binds a superuser", is_super, False)
        check("and cannot bypass RLS", bypasses, False)
        check("and does not own the tables, which FORCE would otherwise have to catch",
              owner != db.APP_ROLE, True)

        with db.admin() as conn:
            armed = dict(conn.execute(
                "SELECT relname, relrowsecurity AND relforcerowsecurity "
                "FROM pg_class WHERE relname = ANY(%s)", (list(TENANT_TABLES),)
            ).fetchall())
            policed = {r[0] for r in conn.execute(
                "SELECT tablename FROM pg_policies "
                "WHERE policyname = 'tenant_isolation' AND tablename = ANY(%s)",
                (list(TENANT_TABLES),)).fetchall()}
        check("every tenant table has RLS enabled AND forced",
              sorted(t for t, ok in armed.items() if ok), sorted(TENANT_TABLES))
        check("every tenant table carries the isolation policy",
              sorted(policed), sorted(TENANT_TABLES))

        # --- the trust boundary that actually holds the line -----------------
        # RLS keys on a session setting the app role is allowed to write, so
        # the real boundary is that no attacker-controlled value reaches it.
        rejected = []
        for bad in ("1 OR 1=1", f"{bob}", 0, -1, None, 1.5, True):
            try:
                with db.tenant(bad):
                    pass
            except ValueError:
                rejected.append(bad)
            except Exception:  # noqa: BLE001 -- any other failure is still not a pass
                pass
        check("tenant() refuses everything that is not a positive int",
              rejected, ["1 OR 1=1", f"{bob}", 0, -1, None, 1.5, True])

        # --- a known limit, pinned so it stays known --------------------------
        # If someone later makes the setting transaction-scoped or moves the
        # tenant key somewhere the app role cannot write, this check fails and
        # forces the docstring above to be rewritten. That is the point.
        # A third user with no data of their own, so this does not depend on
        # whether an earlier check has erased someone.
        mallory = make_user("mallory@example.com")
        with db.tenant(mallory) as conn:
            own = conn.execute("SELECT count(*) FROM raw_transaction").fetchone()[0]
            conn.execute("SELECT set_config('recur.user_id', %s, false)", (str(alice),))
            pivoted = conn.execute(
                "SELECT count(*) FROM raw_transaction").fetchone()[0]
        check("a tenant with no data of their own sees none", own, 0)
        check("arbitrary SQL as the app role can still re-point the tenant key "
              "(documented limit, not a defence)", pivoted > 0, True)

        with db.admin() as conn:
            conn.execute("DELETE FROM app_user WHERE email LIKE '%@example.com'")
            conn.commit()
    finally:
        db.close_pool()

    if FAILURES:
        print(f"FAIL ({len(FAILURES)})")
        print("\n".join(FAILURES))
        raise SystemExit(1)
    # A floor, not a target. The count was hardcoded here, so deleting a check
    # left the suite printing the old number and looking unchanged.
    FLOOR = 18
    if CHECKS[0] < FLOOR:
        raise SystemExit(f"isolation checks shrank: {CHECKS[0]} < {FLOOR}. "
                         "An edit probably deleted one -- check git diff.")
    print(f"ok  ({CHECKS[0]} isolation checks)")


if __name__ == "__main__":
    main()
