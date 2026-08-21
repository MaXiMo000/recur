"""Run: python test_pipeline.py

Two tenants run the full pipeline against different statements. Checks that the
results are correct per user AND that neither can see the other's -- because a
pipeline that is correct in isolation and leaks under concurrency is worse than
one that is obviously broken.
"""

from app import auth
from app import db
from app import pipeline

FAILURES = []

ALICE_CSV = b"""Date,Description,Amount
09/03/2025,SP * NETFLIX.COM 866-579-7172,-15.49
10/03/2025,NETFLIX.COM,-15.49
11/03/2025,NETFLIX.COM,-15.49
12/03/2025,SP * NETFLIX.COM 866-579-7172,-17.99
01/03/2026,NETFLIX.COM,-17.99
02/03/2026,NETFLIX.COM,-17.99
09/14/2025,ALICE THERAPY ASSOCIATES,-180.00
"""

# Deliberately a European export: dotted dates, comma decimals, semicolons.
BOB_CSV = """Datum;Beschreibung;Betrag
03.09.2025;SPOTIFY AB;-11,99
03.10.2025;SPOTIFY AB;-11,99
03.11.2025;SPOTIFY AB;-11,99
03.12.2025;SPOTIFY AB;-11,99
14.09.2025;BOB PRIVATE CLINIC;-250,00
""".encode()


def check(label, got, expected):
    if got != expected:
        FAILURES.append(f"  {label}\n    expected {expected!r}\n    got      {got!r}")


def main() -> None:
    db.apply_schema()
    db.open_pool()
    try:
        with db.admin() as conn:
            conn.execute("DELETE FROM app_user WHERE email LIKE %s", ("%@example.com",))
            conn.commit()

        alice, t1 = auth.register("a@example.com", "a-long-enough-password")
        auth.consume_email_token(t1, "verify")
        bob, t2 = auth.register("b@example.com", "a-long-enough-password")
        auth.consume_email_token(t2, "verify")

        ra = pipeline.run(alice, ALICE_CSV, "alice-card")
        rb = pipeline.run(bob, BOB_CSV, "bob-card", dayfirst=True, currency="EUR")

        check("alice's rows loaded", ra["rows_read"], 7)
        check("bob's rows loaded", rb["rows_read"], 5)
        check("alice has a subscription", ra["subscriptions_found"] >= 1, True)
        check("bob has a subscription", rb["subscriptions_found"] >= 1, True)

        # --- European amounts survived the round trip: 11,99 is not 1199 euros
        with db.tenant(bob) as conn:
            amt = conn.execute(
                "SELECT current_amount_cents FROM subscription s "
                "JOIN merchant m ON m.id = s.merchant_id "
                "WHERE m.canonical_name LIKE 'SPOTIFY%'").fetchone()[0]
        check("comma decimals parsed as 11.99", amt, 1199)

        # --- the price step in alice's data was found
        with db.tenant(alice) as conn:
            steps = conn.execute(
                "SELECT old_amount_cents, new_amount_cents FROM price_change").fetchall()
        check("netflix increase detected", steps, [(1549, 1799)])

        # --- neither tenant can see the other, through the pipeline's own tables
        with db.tenant(alice) as conn:
            names = [r[0] for r in conn.execute(
                "SELECT canonical_name FROM merchant").fetchall()]
        check("alice cannot see bob's clinic",
              any("CLINIC" in n or "SPOTIFY" in n for n in names), False)
        check("alice can see her own therapist",
              any("THERAPY" in n for n in names), True)

        with db.tenant(bob) as conn:
            names = [r[0] for r in conn.execute(
                "SELECT canonical_name FROM merchant").fetchall()]
        check("bob cannot see alice's therapist",
              any("THERAPY" in n or "NETFLIX" in n for n in names), False)

        # --- re-uploading the same file changes nothing
        again = pipeline.run(alice, ALICE_CSV, "alice-card")
        check("re-upload inserts nothing", again["rows_new"], 0)
        check("re-upload finds the same subscriptions",
              again["subscriptions_found"], ra["subscriptions_found"])

        # --- detect must not wipe the other tenant's subscriptions
        # (TRUNCATE would have; it ignores row-level security entirely)
        pipeline.rerun(alice)
        with db.tenant(bob) as conn:
            n = conn.execute("SELECT count(*) FROM subscription").fetchone()[0]
        check("alice re-running detection leaves bob's subscriptions alone",
              n >= 1, True)

        # --- rubbish input fails with a message a user can act on
        for bad, why in [(b"", "empty"), (b"\x00\x01\x02binary", "binary"),
                         (b"no,useful,columns\n1,2,3", "no date column")]:
            try:
                pipeline.run(alice, bad, "junk")
                FAILURES.append(f"  {why}: expected ValueError, none raised")
            except ValueError:
                pass
            except Exception as e:
                FAILURES.append(f"  {why}: expected ValueError, got {type(e).__name__}")

        # --- erasure really erases
        auth.delete_user(bob)
        with db.tenant(bob) as conn:
            n = conn.execute("SELECT count(*) FROM raw_transaction").fetchone()[0]
        check("deleting bob removes bob's transactions", n, 0)
        with db.tenant(alice) as conn:
            n = conn.execute("SELECT count(*) FROM raw_transaction").fetchone()[0]
        check("deleting bob leaves alice's transactions", n, 7)

        # --- currencies must never be added together, and renaming an
        # account must not duplicate its history.
        jpy = b"Date,Description,Amount\n" + b"".join(
            f"{m:02d}/09/2026,SPOTIFY JP,-1200\n".encode() for m in range(1, 7))
        pipeline.run(alice, jpy, "alice-jp", currency="JPY")
        with db.tenant(alice) as conn:
            amt, cur = conn.execute(
                "SELECT current_amount_cents, currency FROM subscription s "
                "JOIN merchant m ON m.id = s.merchant_id "
                "WHERE m.canonical_name LIKE 'SPOTIFY%'").fetchone()
        check("yen is stored as whole yen, not hundredths", (amt, cur), (1200, "JPY"))

        with db.tenant(alice) as conn:
            before = conn.execute("SELECT count(*) FROM raw_transaction").fetchone()[0]
            conn.execute("UPDATE account SET label = 'renamed' WHERE label = 'alice-jp'")
            conn.commit()
        pipeline.run(alice, jpy, "renamed", currency="JPY")
        with db.tenant(alice) as conn:
            after = conn.execute("SELECT count(*) FROM raw_transaction").fetchone()[0]
        check("renaming an account does not duplicate its transactions",
              after, before)

        with db.admin() as conn:
            conn.execute("DELETE FROM app_user WHERE email LIKE %s", ("%@example.com",))
            conn.commit()
    finally:
        db.close_pool()

    if FAILURES:
        print(f"FAIL ({len(FAILURES)})")
        print("\n".join(FAILURES))
        raise SystemExit(1)
    print("ok  (18 pipeline checks)")


if __name__ == "__main__":
    main()
