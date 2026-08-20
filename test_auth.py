"""Run: python test_auth.py

Checks the properties that are easy to claim and easy to get wrong: that
password hashes are never reversible, that sessions can actually be revoked,
that email tokens are single-use, and that a failed login says the same thing
whoever asks.
"""

import time

import auth
import db

FAILURES = []
PW = "correct-horse-battery-staple"


def check(label, got, expected):
    if got != expected:
        FAILURES.append(f"  {label}\n    expected {expected!r}\n    got      {got!r}")


def raises(label, fn, *a, **k):
    try:
        fn(*a, **k)
    except auth.AuthError:
        return
    FAILURES.append(f"  {label}\n    expected AuthError, none raised")


def main() -> None:
    db.apply_schema()
    db.open_pool()
    try:
        with db.admin() as conn:
            conn.execute("DELETE FROM app_user WHERE email LIKE %s", ("%@example.test",))
            conn.commit()

        uid, verify_token = auth.register("carol@example.test", PW)

        # --- the stored hash must not contain or reveal the password
        with db.admin() as conn:
            stored = conn.execute(
                "SELECT password_hash FROM app_user WHERE id = %s", (uid,)).fetchone()[0]
        check("hash is argon2id", stored.startswith("$argon2id$"), True)
        check("password does not appear in the hash", PW in stored, False)

        # --- unverified accounts cannot sign in
        raises("unverified account is refused", auth.authenticate,
               "carol@example.test", PW)

        auth.consume_email_token(verify_token, "verify")
        check("verification succeeds", isinstance(
            auth.authenticate("carol@example.test", PW), int), True)

        # --- verification tokens are single-use
        raises("a verification token cannot be replayed",
               auth.consume_email_token, verify_token, "verify")

        # --- wrong password and unknown user must be indistinguishable
        msgs = set()
        for email, pw in [("carol@example.test", "wrong-password-here"),
                          ("nobody@example.test", PW)]:
            try:
                auth.authenticate(email, pw)
            except auth.AuthError as e:
                msgs.add(str(e))
        check("wrong password and unknown email give one message", len(msgs), 1)

        # --- and take comparable time, so timing isn't an enumeration oracle
        def timed(email, pw):
            t = time.perf_counter()
            try:
                auth.authenticate(email, pw)
            except auth.AuthError:
                pass
            return time.perf_counter() - t
        known = min(timed("carol@example.test", "wrong-password-here") for _ in range(3))
        unknown = min(timed("nobody@example.test", PW) for _ in range(3))
        ratio = max(known, unknown) / max(min(known, unknown), 1e-9)
        check(f"unknown-email timing is not a tell (ratio {ratio:.2f})", ratio < 3.0, True)

        # --- registering an existing address must not disclose that it exists
        uid2, _ = auth.register("carol@example.test", PW)
        check("re-registering returns the same user, no error", uid2, uid)

        # --- sessions: only the hash is stored, and it resolves
        token, _ = auth.start_session(uid, "pytest")
        with db.admin() as conn:
            n = conn.execute("SELECT count(*) FROM session WHERE token_hash = %s",
                             (token,)).fetchone()[0]
        check("the raw token is not what's stored", n, 0)
        check("session resolves to its user", auth.resolve_session(token), uid)
        check("a garbage token resolves to nobody",
              auth.resolve_session("not-a-real-token"), None)
        check("None resolves to nobody", auth.resolve_session(None), None)

        # --- revocation actually revokes (this is why not JWTs)
        auth.end_session(token)
        check("ended session stops resolving", auth.resolve_session(token), None)

        # --- changing a password logs every session out
        t1, _ = auth.start_session(uid)
        t2, _ = auth.start_session(uid)
        auth.set_password(uid, "another-perfectly-fine-password")
        check("password change kills session 1", auth.resolve_session(t1), None)
        check("password change kills session 2", auth.resolve_session(t2), None)

        # --- short passwords are refused
        raises("short password refused", auth.validate_password, "short")

        # --- disabling an account invalidates live sessions immediately
        auth.set_password(uid, PW)
        live, _ = auth.start_session(uid)
        with db.admin() as conn:
            conn.execute("UPDATE app_user SET disabled_at = now() WHERE id = %s", (uid,))
            conn.commit()
        check("disabled account's session stops resolving",
              auth.resolve_session(live), None)

        # --- erasure removes the user and everything cascading from them
        auth.delete_user(uid)
        with db.admin() as conn:
            n = conn.execute("SELECT count(*) FROM app_user WHERE id = %s",
                             (uid,)).fetchone()[0]
            s = conn.execute("SELECT count(*) FROM session WHERE user_id = %s",
                             (uid,)).fetchone()[0]
        check("user deleted", n, 0)
        check("their sessions went with them", s, 0)
    finally:
        db.close_pool()

    if FAILURES:
        print(f"FAIL ({len(FAILURES)})")
        print("\n".join(FAILURES))
        raise SystemExit(1)
    print("ok  (18 auth checks)")


if __name__ == "__main__":
    main()
