# Recur

Find out what you're actually paying for. Upload a bank CSV, get the truth about
your recurring charges: what renews, what quietly went up, what hits next week.

**No bank credentials. No Plaid. No OAuth.** You download a CSV yourself and the
data stays on your machine. This system has nowhere to put a bank password.

---

## Status: deployed shape, multi-tenant

| | |
|---|---|
| Isolation | Postgres row-level security, app connects as a `NOBYPASSRLS` role |
| Auth | argon2id, revocable opaque sessions, email verification |
| Remote MCP | OAuth 2.1 + PKCE (S256), audience-bound tokens |
| Deploy | one Render web service + private managed Postgres |
| Email | Resend (3,000/mo free); `check_email.py` verifies it before deploy |

```
test_scrub    20 descriptors, 12 amounts    test_pipeline  16 checks
test_resolve   9 checks                     test_api       28 checks
test_detect   20 checks                     test_mcp       13 checks
test_tenancy   9 isolation checks           test_oauth     26 checks
test_auth     18 checks
```

Run everything: `for t in tests/test_*.py; do .venv/bin/python $t; done`

## Layout

```
app/                the application; everything importable lives here
  core/             the domain logic, no web and no auth
    scrub.py        tier 0 — descriptor normalization
    ingest.py       CSV dialect sniffing, sign/date/decimal handling, dedup
    resolve.py      merchant resolution ladder + review queue
    detect.py       periodicity, price changes, forecast
  db.py             connections and the tenant context RLS keys on
  schema.sql        tables, policies, the recur_current_user_id() function
  config.py         settings, and the refusal to boot without production ones
  auth.py           argon2 passwords, sessions, email tokens
  oauth.py          OAuth 2.1 authorization server (PKCE, audience-bound)
  mailer.py         Resend adapter
  pipeline.py       one call: bytes in, detected subscriptions out
  api.py            FastAPI app; serves the React build from the same origin
  mcp_http.py       remote MCP endpoint + the OAuth routes guarding it
  mcp_tools.py      the MCP tools, as functions taking a user id

tests/              one file per layer, runnable directly
scripts/            CLIs: mcp_server, make_sample, check_email, benchmark, run_all
migrations/         Alembic; 0001 initial, 0002 oauth
web/                React app (Vite); built into the image
```

`core/` is the line worth keeping: those four modules do the actual work and
know nothing about HTTP, sessions or tenants. Everything above them is the
service layer. `pip install -e .` makes `app` importable everywhere, so no file
needs a `sys.path` hack.

## The security decisions worth reading

**Tenant isolation is the database's job.** Every data table carries `user_id`
and a policy keyed on a per-connection setting, so a handler that forgets its
tenant filter returns *zero rows* rather than someone else's statement.
`test_tenancy.py` proves the adversarial cases: unscoped `SELECT`, naming
another user's id explicitly, joins, `INSERT` branded as another user, `UPDATE`
reassigning ownership, cross-tenant `DELETE`.

**The bug that made that necessary.** The first implementation had RLS enabled,
forced, with a policy — and Alice could read every one of Bob's transactions.
The app was connecting as a **superuser**, and superusers bypass RLS
unconditionally; `FORCE` binds only the table owner. `pg_class` reported
`relrowsecurity = true` the whole time. The app now connects as a dedicated
`NOSUPERUSER NOBYPASSRLS` role, and the privileged DSN is used only for
migrations.

**`TRUNCATE` ignores row-level security.** Detection used it to clear old
results; one user re-running detection would have wiped every other tenant's
subscriptions. RLS does not save you there.

**Same origin, on purpose.** The API serves the React build. A separate
frontend origin forces `SameSite=None` cookies, which throws away the CSRF
protection `SameSite` exists to give.

**Rate limits live in Postgres.** An in-process counter is per-instance and
resets on deploy: two instances double every limit, and a restart clears a
brute-force mid-attempt.

**Production refuses to boot without its secrets.** `RECUR_ENV=production`
fails at startup — before migrations — listing each missing variable and what
goes wrong without it.

## Remote MCP

`/mcp`, behind OAuth 2.1. 41% of public MCP servers have no authentication and
8.5% use OAuth; this is what keeps it out of the first number.

- **PKCE mandatory, S256 only.** `plain` is rejected. MCP clients are desktop
  apps and cannot hold a secret, so the verifier is what proves the client
  redeeming a code is the one that asked for it.
- **Redirect URIs match exactly.** No prefixes, no wildcards.
- **Tokens are audience-bound** — a token minted here is refused anywhere else,
  which closes the confused-deputy problem when a client talks to several MCP
  servers.
- **Codes are deleted as they are read**, in one statement, so two concurrent
  redemptions cannot both succeed.
- **Tool descriptions are static literals**, never interpolated with a merchant
  name. A descriptor is attacker-controlled text; a tool description is read by
  the model as instructions. `test_mcp.py` asserts it.
- Every grant is listable and revocable by the user.

The stdio server still exists for local use: no OAuth round trip to reach a
database on your own machine, and no network surface to attack.

## Deploying to Render

```bash
render blueprint launch     # or point Render at render.yaml in the dashboard
```

`RECUR_APP_PASSWORD` is generated by Render and never seen by anyone. The
database has an empty `ipAllowList`, so it is reachable only from inside
Render's private network. Set `RESEND_API_KEY` and `RECUR_EMAIL_FROM` in the
dashboard — they are `sync: false` and are never committed.

### Email setup (Resend)

Resend's free tier is 3,000/month, 100/day, permanently — Postmark gives 100 a
month and SendGrid retired its free plan in 2025.

1. Sign up at [resend.com](https://resend.com) (no card).
2. **API Keys → Create API Key**, permission **Sending access**. Copy it —
   it is shown once.
3. **Add a domain.** This is the step people skip. Until a domain is verified,
   Resend delivers only to the address that owns the Resend account, so with
   open signup every user except you gets a "check your email" screen and no
   email — and the API reports success either way. Add the DNS records Resend
   gives you (an MX and two TXT: SPF and DKIM) at your registrar and wait for
   verification.
4. Check it before deploying:

```bash
RESEND_API_KEY=re_xxx RECUR_EMAIL_FROM='Recur <noreply@yourdomain.com>' \
    .venv/bin/python check_email.py you@example.com
```

5. In Render: **Environment → Add** `RESEND_API_KEY` and `RECUR_EMAIL_FROM`.
   Both are `sync: false` in the blueprint, so they live in the dashboard and
   never in the repo.

No domain yet? Deploy with `RECUR_REGISTRATION_OPEN=0`. The app runs, you can
use it, and nobody hits a broken signup.

**Before you take real users:** you become a data controller. Erasure
(`DELETE /api/me`) and portability (`GET /api/export`) are built; a privacy
policy, a lawful basis, and breach-notification procedures are not code and are
yours to write.

