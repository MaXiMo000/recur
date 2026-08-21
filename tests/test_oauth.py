"""Run: python test_oauth.py

A complete authorization-code + PKCE flow, then the attacks it has to refuse:
a replayed code, a wrong verifier, a stolen code redeemed by another client, an
unregistered redirect URI, `plain` PKCE, and a token replayed at a different
audience.
"""

import base64
import hashlib
import secrets

from fastapi.testclient import TestClient

from app import api
from app import auth
from app import db
from app import oauth

FAILURES = []
PW = "a-perfectly-fine-password"
REDIRECT = "http://127.0.0.1:33418/callback"


def check(label, got, expected):
    if got != expected:
        FAILURES.append(f"  {label}\n    expected {expected!r}\n    got      {got!r}")


def challenge(verifier: str) -> str:
    d = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(d).decode().rstrip("=")


def main() -> None:
    db.apply_schema()
    with TestClient(api.app) as c:
        with db.admin() as conn:
            conn.execute("DELETE FROM app_user WHERE email LIKE %s", ("%@example.com",))
            conn.execute("DELETE FROM oauth_client")
            conn.execute("DELETE FROM auth_attempt")
            conn.commit()

        uid, tok = auth.register("mcp@example.com", PW)
        auth.consume_email_token(tok, "verify")

        # --- discovery documents an MCP client reads first
        prm = c.get("/.well-known/oauth-protected-resource").json()
        check("protected-resource names this server as the resource",
              prm["resource"].endswith("/mcp"), True)
        asm = c.get("/.well-known/oauth-authorization-server").json()
        check("only S256 is advertised", asm["code_challenge_methods_supported"], ["S256"])
        check("public clients only", asm["token_endpoint_auth_methods_supported"], ["none"])

        # --- dynamic registration
        reg = c.post("/oauth/register",
                     json={"client_name": "Claude", "redirect_uris": [REDIRECT]})
        check("registration returns 201", reg.status_code, 201)
        client_id = reg.json()["client_id"]

        check("plaintext http to a real host is refused",
              c.post("/oauth/register", json={"client_name": "x",
                     "redirect_uris": ["http://evil.example.com/cb"]}).status_code, 400)

        # --- an unauthenticated authorize sends the human to sign in
        v = secrets.token_urlsafe(48)
        q = {"client_id": client_id, "redirect_uri": REDIRECT, "response_type": "code",
             "scope": "recur:read", "state": "xyz", "code_challenge": challenge(v),
             "code_challenge_method": "S256"}
        r = c.get("/oauth/authorize", params=q, follow_redirects=False)
        check("anonymous authorize redirects to sign-in", r.status_code, 303)

        c.post("/api/auth/login", json={"email": "mcp@example.com", "password": PW})

        # --- 'plain' PKCE must be rejected outright
        r = c.get("/oauth/authorize", params={**q, "code_challenge_method": "plain"})
        check("plain PKCE is refused", r.status_code, 400)

        # --- an unregistered redirect URI is refused, and NOT redirected to
        r = c.get("/oauth/authorize",
                  params={**q, "redirect_uri": "https://attacker.example/cb"},
                  follow_redirects=False)
        check("unregistered redirect_uri is refused", r.status_code, 400)
        check("and is not redirected to", "location" in r.headers, False)

        # --- consent screen, then approval
        r = c.get("/oauth/authorize", params=q)
        check("consent screen names the client", "Claude" in r.text, True)
        r = c.post("/oauth/approve", params=q, follow_redirects=False)
        check("approval redirects to the client", r.status_code, 303)
        loc = r.headers["location"]
        check("state is returned verbatim", "state=xyz" in loc, True)
        code = loc.split("code=")[1].split("&")[0]

        # --- wrong verifier fails
        r = c.post("/oauth/token", data={"grant_type": "authorization_code", "code": code,
                   "redirect_uri": REDIRECT, "client_id": client_id,
                   "code_verifier": secrets.token_urlsafe(48)})
        check("a wrong code_verifier is rejected", r.status_code, 400)
        check("with invalid_grant", r.json()["error"], "invalid_grant")

        # --- and that consumed the code, so the right verifier now fails too
        r = c.post("/oauth/token", data={"grant_type": "authorization_code", "code": code,
                   "redirect_uri": REDIRECT, "client_id": client_id, "code_verifier": v})
        check("a code is single-use even after a failed attempt", r.status_code, 400)

        # --- a clean run
        v2 = secrets.token_urlsafe(48)
        q2 = {**q, "code_challenge": challenge(v2), "state": "abc"}
        loc = c.post("/oauth/approve", params=q2, follow_redirects=False).headers["location"]
        code2 = loc.split("code=")[1].split("&")[0]
        r = c.post("/oauth/token", data={"grant_type": "authorization_code", "code": code2,
                   "redirect_uri": REDIRECT, "client_id": client_id, "code_verifier": v2})
        check("token issued", r.status_code, 200)
        body = r.json()
        check("bearer token", body["token_type"], "Bearer")
        check("scoped to read", body["scope"], "recur:read")
        access = body["access_token"]

        # --- the token is stored hashed, not in the clear
        with db.admin() as conn:
            n = conn.execute("SELECT count(*) FROM oauth_token WHERE token_hash = %s",
                             (access,)).fetchone()[0]
        check("the raw access token is not what's stored", n, 0)

        # --- resolves for this audience, and only this one
        aud = prm["resource"]
        check("token resolves at its own audience", oauth.resolve_token(access, aud), uid)
        check("token is refused at another audience",
              oauth.resolve_token(access, "https://someone-else.example/mcp"), None)
        check("a made-up token resolves to nobody",
              oauth.resolve_token("not-a-token", aud), None)

        # --- a code issued to one client cannot be redeemed by another
        reg2 = c.post("/oauth/register",
                      json={"client_name": "Other", "redirect_uris": [REDIRECT]}).json()
        v3 = secrets.token_urlsafe(48)
        q3 = {**q, "code_challenge": challenge(v3)}
        loc = c.post("/oauth/approve", params=q3, follow_redirects=False).headers["location"]
        code3 = loc.split("code=")[1].split("&")[0]
        r = c.post("/oauth/token", data={"grant_type": "authorization_code", "code": code3,
                   "redirect_uri": REDIRECT, "client_id": reg2["client_id"],
                   "code_verifier": v3})
        check("a stolen code cannot be redeemed by another client", r.status_code, 400)

        # --- MCP endpoint refuses anonymous callers and advertises where to go
        r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        check("unauthenticated /mcp is 401", r.status_code, 401)
        check("401 carries WWW-Authenticate for discovery",
              "resource_metadata" in r.headers.get("www-authenticate", ""), True)

        # --- the user can see and revoke the grant
        grants = c.get("/api/connections").json()
        check("the grant is listed", [g["client"] for g in grants].count("Claude") >= 1, True)
        gid = [g for g in grants if g["client"] == "Claude"][0]["id"]
        check("revoking returns ok", c.delete(f"/api/connections/{gid}").status_code, 200)
        check("a revoked token stops resolving", oauth.resolve_token(access, aud), None)

        with db.admin() as conn:
            conn.execute("DELETE FROM app_user WHERE email LIKE %s", ("%@example.com",))
            conn.execute("DELETE FROM oauth_client")
            conn.commit()

    if FAILURES:
        print(f"FAIL ({len(FAILURES)})")
        print("\n".join(FAILURES))
        raise SystemExit(1)
    print("ok  (26 oauth checks)")


if __name__ == "__main__":
    main()
