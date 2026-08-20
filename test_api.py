"""Run: python test_api.py

Drives the HTTP surface the way a client (or an attacker) would: no session, a
forged session, one user reaching for another's rows by id, rate limits, and
the enumeration endpoints.
"""

import io

from fastapi.testclient import TestClient

import api
import auth
import config
import db

FAILURES = []
PW = "a-perfectly-fine-password"

CSV_A = b"""Date,Description,Amount
09/03/2025,SP * NETFLIX.COM 866-579,-15.49
10/03/2025,NETFLIX.COM,-15.49
11/03/2025,NETFLIX.COM,-15.49
12/03/2025,NETFLIX.COM,-17.99
01/03/2026,NETFLIX.COM,-17.99
02/03/2026,NETFLIX.COM,-17.99
"""
CSV_B = b"""Date,Description,Amount
09/07/2025,BOB CONFIDENTIAL LLC,-99.00
10/07/2025,BOB CONFIDENTIAL LLC,-99.00
11/07/2025,BOB CONFIDENTIAL LLC,-99.00
"""


def check(label, got, expected):
    if got != expected:
        FAILURES.append(f"  {label}\n    expected {expected!r}\n    got      {got!r}")


def signup(c: TestClient, email: str) -> None:
    r = c.post("/api/auth/register", json={"email": email, "password": PW})
    check(f"register {email}", r.status_code, 202)
    with db.admin() as conn:
        uid = conn.execute("SELECT id FROM app_user WHERE email = %s",
                           (email,)).fetchone()[0]
        conn.execute("UPDATE app_user SET email_verified_at = now() WHERE id = %s",
                     (uid,))
        conn.commit()


def main() -> None:
    db.apply_schema()
    with TestClient(api.app) as anon:
        with db.admin() as conn:
            conn.execute("DELETE FROM app_user WHERE email LIKE %s", ("%@example.com",))
            conn.execute("DELETE FROM auth_attempt")
            conn.commit()

        # --- everything is closed to an anonymous caller
        for path in ("/api/summary", "/api/subscriptions", "/api/upcoming",
                     "/api/increases", "/api/review-queue", "/api/me",
                     "/api/export", "/api/history/1"):
            check(f"anonymous {path} is 401", anon.get(path).status_code, 401)
        check("anonymous upload is 401",
              anon.post("/api/upload",
                        files={"file": ("x.csv", io.BytesIO(CSV_A), "text/csv")}
                        ).status_code, 401)

        # --- a forged cookie is not a session
        anon.cookies.set(config.COOKIE_NAME, "totally-made-up-token")
        check("forged session cookie is 401", anon.get("/api/me").status_code, 401)
        anon.cookies.clear()

        signup(anon, "alice@example.com")
        signup(anon, "bob@example.com")

    with TestClient(api.app) as a, TestClient(api.app) as b:
        check("login", a.post("/api/auth/login",
              json={"email": "alice@example.com", "password": PW}).status_code, 200)
        check("wrong password is 401", a.post("/api/auth/login",
              json={"email": "alice@example.com", "password": "nope-nope-nope"}
              ).status_code, 401)
        b.post("/api/auth/login", json={"email": "bob@example.com", "password": PW})

        ra = a.post("/api/upload", files={"file": ("a.csv", io.BytesIO(CSV_A), "text/csv")},
                    data={"account": "alice-card"})
        check("alice upload ok", ra.status_code, 200)
        rb = b.post("/api/upload", files={"file": ("b.csv", io.BytesIO(CSV_B), "text/csv")},
                    data={"account": "bob-card"})
        check("bob upload ok", rb.status_code, 200)

        subs_a = a.get("/api/subscriptions").json()
        subs_b = b.get("/api/subscriptions").json()
        check("alice sees only her merchants",
              [s["merchant"] for s in subs_a], ["NETFLIX"])
        check("bob sees only his merchants",
              [s["merchant"] for s in subs_b], ["BOB CONFIDENTIAL LLC"])

        # --- the attack: alice asks for bob's subscription by its real id
        bob_sub_id = subs_b[0]["id"]
        r = a.get(f"/api/history/{bob_sub_id}")
        check("alice reading bob's subscription id returns nothing",
              (r.status_code, r.json()), (200, []))

        # --- and cannot answer a queue item that isn't hers
        r = a.post("/api/review-queue/resolve",
                   json={"queue_id": 999999, "merchant": "X"})
        check("resolving someone else's queue item is 404", r.status_code, 404)

        # --- export returns her data and only hers
        exp = a.get("/api/export").json()
        names = [m["canonical_name"] for m in exp["merchant"]]
        check("export is scoped to the caller", names, ["NETFLIX"])

        # --- the price rise came through the API
        inc = a.get("/api/increases").json()
        check("price increase visible over HTTP",
              [(i["old_amount_cents"], i["new_amount_cents"]) for i in inc],
              [(1549, 1799)])

        # --- rubbish upload gives a usable message, not a stack trace
        r = a.post("/api/upload",
                   files={"file": ("x.csv", io.BytesIO(b"\x00\x01binary"), "text/csv")})
        check("binary upload is a 400", r.status_code, 400)
        check("400 body carries a message", "detail" in r.json(), True)
        r = a.post("/api/upload",
                   files={"file": ("x.exe", io.BytesIO(CSV_A), "application/octet-stream")})
        check("non-CSV extension refused", r.status_code, 400)

        # --- logout really ends it
        a.post("/api/auth/logout")
        check("after logout the session is gone", a.get("/api/me").status_code, 401)

    # --- rate limiting on login
    with db.admin() as conn:
        conn.execute("DELETE FROM auth_attempt")
        conn.commit()
    with TestClient(api.app) as c:
        codes = [c.post("/api/auth/login",
                        json={"email": "bob@example.com", "password": "wrong-one-here"}
                        ).status_code for _ in range(12)]
        check("login rate limit eventually returns 429", 429 in codes, True)

    # --- forgot-password must not reveal which addresses exist
    with db.admin() as conn:
        conn.execute("DELETE FROM auth_attempt")
        conn.commit()
    with TestClient(api.app) as c:
        r1 = c.post("/api/auth/forgot", json={"email": "bob@example.com"})
        r2 = c.post("/api/auth/forgot", json={"email": "ghost@example.com"})
        check("forgot-password answers identically for unknown addresses",
              (r1.status_code, r1.json()), (r2.status_code, r2.json()))

    # --- erasure through the API
    with TestClient(api.app) as c:
        c.post("/api/auth/login", json={"email": "bob@example.com", "password": PW})
        check("delete account", c.delete("/api/me").status_code, 200)
        with db.admin() as conn:
            n = conn.execute("SELECT count(*) FROM app_user WHERE email = %s",
                             ("bob@example.com",)).fetchone()[0]
        check("account is gone", n, 0)

    with db.admin() as conn:
        conn.execute("DELETE FROM app_user WHERE email LIKE %s", ("%@example.com",))
        conn.execute("DELETE FROM auth_attempt")
        conn.commit()

    if FAILURES:
        print(f"FAIL ({len(FAILURES)})")
        print("\n".join(FAILURES))
        raise SystemExit(1)
    print("ok  (28 api checks)")


if __name__ == "__main__":
    main()
