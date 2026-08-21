#!/usr/bin/env python3
"""The public API.

    uvicorn api:app --port 8000

Every data endpoint is behind a session and scoped by row-level security, so a
handler that forgets its tenant filter returns nothing rather than someone
else's statement. `db.tenant()` is the only way in; there is no code path here
that reads transaction data without one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
from contextlib import asynccontextmanager, suppress
from datetime import date, timedelta

from fastapi import (Depends, FastAPI, File, Form, HTTPException, Query,
                     Request, Response, UploadFile)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field

from app import auth
from app import config
from app import db
from app import mailer
from app import mcp_http
from app import oauth
from app import pipeline
from app.core.detect import CADENCES, USAGE_CV

PERIOD = dict((c[0], c[1]) for c in CADENCES)
log = logging.getLogger("recur")

HOUSEKEEPING_SECONDS = 3600
MAX_PAGE = 500          # nothing returns an unbounded number of rows


async def _housekeeping() -> None:
    """Purging only at startup means a process that stays up for weeks never
    purges again: expired sessions, spent tokens and rate-limit rows accumulate
    for as long as the instance lives, which on a healthy deployment is the
    normal case rather than the exception."""
    while True:
        try:
            auth.purge_expired()
            oauth.purge_expired()
        except Exception:
            log.exception("housekeeping failed")   # never kill the loop
        await asyncio.sleep(HOUSEKEEPING_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.check()
    config.init_error_tracking()
    db.apply_schema()
    db.open_pool()
    task = asyncio.create_task(_housekeeping())
    yield
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    db.close_pool()


app = FastAPI(title="Recur", lifespan=lifespan, docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.ALLOWED_ORIGINS,
    allow_credentials=True,          # required for the session cookie
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    r = await call_next(request)
    r.headers["X-Content-Type-Options"] = "nosniff"
    r.headers["X-Frame-Options"] = "DENY"
    r.headers["Referrer-Policy"] = "no-referrer"
    r.headers["Cache-Control"] = "no-store"
    if config.IS_PROD:
        r.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return r


# ------------------------------------------------------------ rate limits --

def client_ip(request: Request) -> str:
    # Render terminates TLS and forwards; the leftmost entry is the client.
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() if fwd
            else (request.client.host if request.client else "unknown"))


def rate_limit(kind: str, key: str) -> None:
    """Counted in Postgres rather than in memory, because an in-process counter
    resets on every deploy and is per-instance -- two instances would double
    every limit, and a restart would clear a brute-force in progress."""
    limit, window = config.LIMITS[kind]
    with db.admin() as conn:
        n = conn.execute(
            "SELECT count(*) FROM auth_attempt WHERE key = %s AND kind = %s "
            "AND at > now() - make_interval(secs => %s)",
            (key, kind, window)).fetchone()[0]
        if n >= limit:
            conn.commit()
            raise HTTPException(429, "Too many attempts. Try again later.")
        conn.execute("INSERT INTO auth_attempt (key, kind) VALUES (%s, %s)",
                     (key, kind))
        conn.commit()


def clear_attempts(kind: str, key: str) -> None:
    with db.admin() as conn:
        conn.execute("DELETE FROM auth_attempt WHERE key = %s AND kind = %s",
                     (key, kind))
        conn.commit()


# --------------------------------------------------------------- session --

def current_user(request: Request) -> int:
    uid = auth.resolve_session(request.cookies.get(config.COOKIE_NAME))
    if uid is None:
        raise HTTPException(401, "Sign in to continue.")
    return uid


def set_session_cookie(response: Response, token: str, expires) -> None:
    response.set_cookie(
        config.COOKIE_NAME, token,
        httponly=True,                       # JavaScript can never read it
        secure=config.COOKIE_SECURE,
        samesite=config.COOKIE_SAMESITE,     # the CSRF defence for this API
        domain=config.COOKIE_DOMAIN,
        expires=expires, path="/",
    )


class Credentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=auth.MAX_PASSWORD)


class TokenIn(BaseModel):
    token: str = Field(min_length=1, max_length=500)


class ResetIn(TokenIn):
    password: str = Field(min_length=1, max_length=auth.MAX_PASSWORD)


class EmailIn(BaseModel):
    email: EmailStr


# ------------------------------------------------------------------ auth --

@app.post("/api/auth/register", status_code=202)
def register(body: Credentials, request: Request) -> dict:
    if not config.REGISTRATION_OPEN:
        raise HTTPException(403, "Registration is closed.")
    rate_limit("register", client_ip(request))
    try:
        _, token = auth.register(body.email, body.password)
    except auth.AuthError as e:
        raise HTTPException(400, str(e))
    mailer.send_verification(body.email, token)
    # Identical response whether or not the address was already registered.
    return {"status": "check_your_email"}


@app.post("/api/auth/verify")
def verify(body: TokenIn) -> dict:
    try:
        auth.consume_email_token(body.token, "verify")
    except auth.AuthError as e:
        raise HTTPException(400, str(e))
    return {"status": "verified"}


@app.post("/api/auth/login")
def login(body: Credentials, request: Request, response: Response) -> dict:
    ip = client_ip(request)
    rate_limit("login", ip)
    rate_limit("login", body.email.lower())   # and per account, not just per IP
    try:
        uid = auth.authenticate(body.email, body.password)
    except auth.AuthError as e:
        raise HTTPException(401, str(e))
    clear_attempts("login", body.email.lower())
    token, expires = auth.start_session(uid, request.headers.get("user-agent"))
    set_session_cookie(response, token, expires)
    return {"status": "ok"}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response) -> dict:
    auth.end_session(request.cookies.get(config.COOKIE_NAME))
    response.delete_cookie(config.COOKIE_NAME, path="/",
                           domain=config.COOKIE_DOMAIN)
    return {"status": "ok"}


@app.post("/api/auth/forgot", status_code=202)
def forgot(body: EmailIn, request: Request) -> dict:
    rate_limit("reset", client_ip(request))
    # Also per address. Limiting only by IP lets someone rotate IPs and flood a
    # person's inbox with reset mail. Applied before the lookup and regardless
    # of whether the account exists, so a 429 does not reveal that it does.
    rate_limit("reset", body.email.lower())
    with db.admin() as conn:
        row = conn.execute("SELECT id FROM app_user WHERE email = %s",
                           (body.email.lower(),)).fetchone()
    if row:
        mailer.send_reset(body.email, auth.issue_email_token(row[0], "reset"))
    # Same answer either way: this endpoint must not confirm who has an account.
    return {"status": "check_your_email"}


@app.post("/api/auth/reset")
def reset(body: ResetIn) -> dict:
    try:
        uid = auth.consume_email_token(body.token, "reset")
        auth.set_password(uid, body.password)
    except auth.AuthError as e:
        raise HTTPException(400, str(e))
    return {"status": "ok"}


@app.get("/api/me")
def me(uid: int = Depends(current_user)) -> dict:
    with db.admin() as conn:
        row = conn.execute(
            "SELECT email, created_at FROM app_user WHERE id = %s", (uid,)).fetchone()
    return {"email": row[0], "since": row[1]}


@app.delete("/api/me")
def delete_me(request: Request, response: Response,
              uid: int = Depends(current_user)) -> dict:
    """Erasure. Everything cascades from app_user, so this is the whole
    deletion -- there is no second copy elsewhere to forget about."""
    auth.end_session(request.cookies.get(config.COOKIE_NAME))
    auth.delete_user(uid)
    response.delete_cookie(config.COOKIE_NAME, path="/",
                           domain=config.COOKIE_DOMAIN)
    return {"status": "deleted"}


# Table names are literals in this module, never user input.
_EXPORT_TABLES = ("account", "merchant", "raw_transaction", "subscription",
                  "price_change", "resolution_queue")


@app.get("/api/export")
def export_everything(uid: int = Depends(current_user)) -> StreamingResponse:
    """Portability: the user's own data back, in full.

    Streamed with a server-side cursor rather than assembled in memory. The
    previous version built every transaction into a list and then serialised
    it, so one person with years of statements could take the process down --
    and this is the one endpoint that is *supposed* to return everything, so a
    page limit is not the answer here.
    """
    def chunks():
        yield '{\n'
        with db.tenant(uid) as conn:
            for t_i, table in enumerate(_EXPORT_TABLES):
                yield ('' if t_i == 0 else ',\n') + f'  "{table}": [\n'
                # server_cursor: rows arrive in batches instead of all at once
                with conn.cursor(name=f"export_{table}") as cur:
                    cur.itersize = 1000
                    cur.execute(f"SELECT * FROM {table}")
                    cols = [d.name for d in cur.description]
                    for r_i, row in enumerate(cur):
                        yield ('' if r_i == 0 else ',\n') + "    " + json.dumps(
                            dict(zip(cols, row)), default=str)
                yield '\n  ]'
        yield '\n}\n'

    return StreamingResponse(
        chunks(), media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="recur-export.json"'})


# ------------------------------------------------------------------ data --

def _rows(uid: int, sql: str, params: tuple = ()) -> list[dict]:
    with db.tenant(uid) as conn:
        cur = conn.execute(sql, params)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _as_of(uid: int) -> date:
    with db.tenant(uid) as conn:
        return conn.execute(
            "SELECT max(posted_date) FROM raw_transaction").fetchone()[0] or date.today()


@app.post("/api/upload")
async def upload(request: Request, file: UploadFile = File(...),
                 account: str = Form("card"), dayfirst: bool = Form(False),
                 currency: str = Form("USD"),
                 uid: int = Depends(current_user)) -> dict:
    rate_limit("upload", str(uid))
    if not (file.filename or "").lower().endswith((".csv", ".txt", ".tsv")):
        raise HTTPException(400, "Upload a CSV exported from your bank.")

    # Refuse on the declared size first. Reading the body and *then* measuring
    # it means a 500 MB upload is already in this process's memory by the time
    # it is rejected.
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > pipeline.MAX_BYTES * 2:
        raise HTTPException(413, "That file is too large.")
    raw = await file.read(pipeline.MAX_BYTES + 1)
    try:
        return pipeline.run(uid, raw, account.strip()[:64] or "card",
                            dayfirst=dayfirst, currency=currency.upper()[:3],
                            source=(file.filename or "upload")[:120])
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception:
        # The traceback goes to the log, never to the caller: it would name
        # tables and columns.
        log.exception("upload failed for user %s", uid)
        raise HTTPException(500, "Could not process that file.")


@app.get("/api/summary")
def summary(uid: int = Depends(current_user)) -> dict:
    rows = _rows(uid, "SELECT cadence, current_amount_cents, status FROM subscription")
    active = [r for r in rows if r["status"] == "active"]
    annual = sum(r["current_amount_cents"] * 365.25 / PERIOD[r["cadence"]]
                 for r in active)
    changes = _rows(uid, "SELECT p.new_amount_cents - p.old_amount_cents AS d,"
                         " s.period_days FROM price_change p"
                         " JOIN subscription s ON s.id = p.subscription_id")
    pending = _rows(uid, "SELECT count(*) AS n FROM resolution_queue"
                         " WHERE status = 'pending'")[0]["n"]
    return {
        "as_of": _as_of(uid),
        "active_count": len(active),
        "inactive_count": len(rows) - len(active),
        "annual_cents": round(annual),
        "monthly_cents": round(annual / 12),
        "price_increase_annual_cents": round(
            sum(c["d"] * 365.25 / float(c["period_days"]) for c in changes)),
        # Surfaced because an unworked queue makes every figure above too low.
        "awaiting_review": pending,
    }


@app.get("/api/subscriptions")
def subscriptions(limit: int = Query(MAX_PAGE, ge=1, le=MAX_PAGE),
                  offset: int = Query(0, ge=0),
                  uid: int = Depends(current_user)) -> list[dict]:
    rows = _rows(uid,
        "SELECT s.id, m.canonical_name AS merchant, s.cadence, s.period_days,"
        "       s.current_amount_cents, s.amount_cv, s.charge_count, s.confidence,"
        "       s.status, s.first_seen, s.last_seen, s.next_due "
        "FROM subscription s JOIN merchant m ON m.id = s.merchant_id "
        "ORDER BY s.current_amount_cents * (365.25 / s.period_days) DESC "
        "LIMIT %s OFFSET %s", (limit, offset))
    for r in rows:
        r["annual_cents"] = round(
            r["current_amount_cents"] * 365.25 / PERIOD[r["cadence"]])
        r["usage_based"] = float(r["amount_cv"]) > USAGE_CV
    return rows


@app.get("/api/upcoming")
def upcoming(days: int = Query(30, ge=1, le=365),
             uid: int = Depends(current_user)) -> list[dict]:
    return _rows(uid,
        "SELECT m.canonical_name AS merchant, s.next_due, s.current_amount_cents,"
        "       s.cadence FROM subscription s JOIN merchant m ON m.id = s.merchant_id "
        "WHERE s.status = 'active' AND s.next_due <= %s ORDER BY s.next_due",
        (_as_of(uid) + timedelta(days=days),))


@app.get("/api/increases")
def increases(uid: int = Depends(current_user)) -> list[dict]:
    rows = _rows(uid,
        "SELECT m.canonical_name AS merchant, p.effective_date, p.old_amount_cents,"
        "       p.new_amount_cents, p.pct_change, s.period_days "
        "FROM price_change p JOIN subscription s ON s.id = p.subscription_id "
        "JOIN merchant m ON m.id = s.merchant_id ORDER BY p.effective_date DESC")
    for r in rows:
        r["annual_impact_cents"] = round(
            (r["new_amount_cents"] - r["old_amount_cents"])
            * 365.25 / float(r["period_days"]))
    return rows


@app.get("/api/history/{subscription_id}")
def history(subscription_id: int,
            limit: int = Query(MAX_PAGE, ge=1, le=MAX_PAGE),
            offset: int = Query(0, ge=0),
            uid: int = Depends(current_user)) -> list[dict]:
    return _rows(uid,
        "SELECT t.posted_date, -t.amount_cents AS amount_cents "
        "FROM raw_transaction t JOIN subscription s ON s.merchant_id = t.merchant_id "
        "AND s.account_id = t.account_id "
        "WHERE s.id = %s AND t.amount_cents < 0 ORDER BY t.posted_date "
        "LIMIT %s OFFSET %s", (subscription_id, limit, offset))


# ---------------------------------------------------------- review queue --

@app.get("/api/review-queue")
def review_queue(limit: int = Query(MAX_PAGE, ge=1, le=MAX_PAGE),
                 offset: int = Query(0, ge=0),
                 uid: int = Depends(current_user)) -> list[dict]:
    return _rows(uid,
        "SELECT id, scrubbed, txn_count, candidates, top_score, reason "
        "FROM resolution_queue WHERE status = 'pending' ORDER BY txn_count DESC "
        "LIMIT %s OFFSET %s", (limit, offset))


class ResolveIn(BaseModel):
    queue_id: int
    merchant: str | None = Field(default=None, max_length=200)
    ignore: bool = False


@app.post("/api/review-queue/resolve")
def resolve_queue_item(body: ResolveIn, uid: int = Depends(current_user)) -> dict:
    """Answer one queued descriptor. The answer is written back as an alias, so
    the same string is never asked about again."""
    with db.tenant(uid) as conn, conn.cursor() as cur:
        row = cur.execute(
            "SELECT scrubbed FROM resolution_queue WHERE id = %s AND status = 'pending'",
            (body.queue_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "That item is not in your review queue.")
        scrubbed = row[0]

        if body.ignore:
            cur.execute("UPDATE resolution_queue SET status = 'ignored' WHERE id = %s",
                        (body.queue_id,))
            conn.commit()
            return {"status": "ignored"}

        name = (body.merchant or scrubbed).strip()[:200]
        if not name:
            raise HTTPException(400, "Give the merchant a name.")
        cur.execute(
            "INSERT INTO merchant (user_id, canonical_name) VALUES (%s, %s) "
            "ON CONFLICT (user_id, canonical_name) DO UPDATE "
            "SET canonical_name = EXCLUDED.canonical_name RETURNING id", (uid, name))
        mid = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO merchant_alias (user_id, scrubbed_pattern, merchant_id,"
            " resolved_by) VALUES (%s, %s, %s, 'human') "
            "ON CONFLICT (user_id, scrubbed_pattern) DO UPDATE "
            "SET merchant_id = EXCLUDED.merchant_id", (uid, scrubbed, mid))
        cur.execute("UPDATE raw_transaction SET merchant_id = %s WHERE scrubbed = %s",
                    (mid, scrubbed))
        cur.execute("UPDATE resolution_queue SET status = 'resolved' WHERE id = %s",
                    (body.queue_id,))
        conn.commit()

    # One answer can merge two merchants, which changes every total downstream.
    return {"status": "resolved", **pipeline.rerun(uid)}


@app.get("/api/health")
def health() -> dict:
    with db.admin() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


# The OAuth and MCP routes are included before the SPA catch-all below, which
# would otherwise swallow /.well-known/* and /oauth/* and answer them with HTML.
app.include_router(mcp_http.router)


# --------------------------------------------------------------- frontend --
# Serving the built app from the same origin as the API is a security decision,
# not a packaging one: a cross-origin frontend needs SameSite=None cookies,
# which throws away the CSRF protection SameSite is there to give. Same origin
# also means the CORS config above is unused in production.

_DIST = pathlib.Path(__file__).resolve().parents[1] / "web" / "dist"

if _DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        # Unknown /api/* must 404 as JSON rather than quietly returning the
        # HTML shell, which would make a typo look like a working endpoint.
        if full_path.startswith("api/"):
            raise HTTPException(404, "Not found.")
        candidate = (_DIST / full_path).resolve()
        if full_path and candidate.is_file() and candidate.is_relative_to(_DIST.resolve()):
            return FileResponse(candidate)
        return FileResponse(_DIST / "index.html")
