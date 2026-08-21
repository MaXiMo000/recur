"""The remote MCP endpoint and the OAuth routes that guard it.

Mounted into api.py. Three pieces:

  - the two `.well-known` documents an MCP client reads to discover where to
    authenticate (RFC 9728 protected-resource, RFC 8414 authorization-server)
  - the authorization-server endpoints: register, authorize, token, revoke
  - /mcp itself, behind a bearer token, running the same tools the stdio server
    exposes against the same functions the REST API uses

The stdio server stays. Local use should not require an OAuth round trip to
talk to a database on the same machine, and a local process on a pipe has no
network surface to attack.
"""

from __future__ import annotations

import json
import logging
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from app import auth
from app import config
from app import mcp_tools
from app import oauth

router = APIRouter()

MCP_PATH = "/mcp"


def base_url(request: Request) -> str:
    """The public origin, from configuration rather than the Host header --
    a forged Host would otherwise let an attacker mint tokens whose audience
    points somewhere they control."""
    return config.PUBLIC_URL or str(request.base_url).rstrip("/")


def resource_id(request: Request) -> str:
    return f"{base_url(request)}{MCP_PATH}"


# ------------------------------------------------------------ discovery --

@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
def protected_resource_metadata(request: Request) -> dict:
    """RFC 9728. Tells a client which authorization server to go to."""
    return {
        "resource": resource_id(request),
        "authorization_servers": [base_url(request)],
        "scopes_supported": sorted(oauth.SCOPES),
        "bearer_methods_supported": ["header"],
    }


@router.get("/.well-known/oauth-authorization-server")
def authorization_server_metadata(request: Request) -> dict:
    """RFC 8414."""
    b = base_url(request)
    return {
        "issuer": b,
        "authorization_endpoint": f"{b}/oauth/authorize",
        "token_endpoint": f"{b}/oauth/token",
        "registration_endpoint": f"{b}/oauth/register",
        "revocation_endpoint": f"{b}/oauth/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "token_endpoint_auth_methods_supported": ["none"],   # public clients
        "code_challenge_methods_supported": ["S256"],        # never "plain"
        "scopes_supported": sorted(oauth.SCOPES),
    }


# --------------------------------------------------------- registration --

class RegisterIn(BaseModel):
    client_name: str = Field(default="MCP client", max_length=120)
    redirect_uris: list[str] = Field(min_length=1, max_length=8)


@router.post("/oauth/register", status_code=201)
def register_client(body: RegisterIn) -> dict:
    try:
        return oauth.register_client(body.client_name, body.redirect_uris)
    except oauth.OAuthError as e:
        raise HTTPException(e.status, {"error": e.code, "error_description": e.description})


# ------------------------------------------------------------ authorize --

@router.get("/oauth/authorize")
def authorize(request: Request, client_id: str = "", redirect_uri: str = "",
              response_type: str = "code", scope: str = "recur:read",
              state: str = "", code_challenge: str = "",
              code_challenge_method: str = "", resource: str = ""):
    """The consent screen. Requires a signed-in human -- an MCP client cannot
    grant itself access to an account."""
    if response_type != "code":
        raise HTTPException(400, "Only the authorization_code flow is supported.")
    try:
        client = oauth.check_authorize_request(
            client_id, redirect_uri, scope, code_challenge, code_challenge_method)
    except oauth.OAuthError as e:
        # Errors about the client or the redirect URI must be shown here, never
        # bounced to a URI we have not validated.
        return HTMLResponse(_page(f"<h1>Can't continue</h1><p>{e.description}</p>"),
                            status_code=e.status)

    user_id = auth.resolve_session(request.cookies.get(config.COOKIE_NAME))
    if user_id is None:
        nxt = f"/oauth/authorize?{urlencode(dict(request.query_params))}"
        return RedirectResponse(f"/?next={nxt}", status_code=303)

    approve = f"/oauth/approve?{urlencode(dict(request.query_params))}"
    return HTMLResponse(_page(f"""
      <h1>Connect {_escape(client['client_name'])}?</h1>
      <p>It will be able to <strong>read</strong> your subscriptions, upcoming
         charges and price changes.</p>
      <p class="muted">It cannot upload statements, change anything, or see your
         password. You can revoke this at any time from your account page.</p>
      <form method="post" action="{_escape(approve)}">
        <button class="primary" type="submit">Allow read access</button>
      </form>
      <p class="muted">Signing in as a different account? Sign out first.</p>
    """))


@router.post("/oauth/approve")
def approve(request: Request, client_id: str = "", redirect_uri: str = "",
            scope: str = "recur:read", state: str = "", code_challenge: str = "",
            code_challenge_method: str = "", resource: str = ""):
    try:
        oauth.check_authorize_request(client_id, redirect_uri, scope,
                                      code_challenge, code_challenge_method)
    except oauth.OAuthError as e:
        raise HTTPException(e.status, e.description)

    user_id = auth.resolve_session(request.cookies.get(config.COOKIE_NAME))
    if user_id is None:
        raise HTTPException(401, "Sign in to continue.")

    code = oauth.issue_code(client_id, user_id, redirect_uri, scope,
                            code_challenge, resource or resource_id(request))
    params = {"code": code}
    if state:
        params["state"] = state       # returned verbatim: the client's CSRF check
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}{urlencode(params)}", status_code=303)


# ---------------------------------------------------------------- token --

@router.post("/oauth/token")
def token(request: Request, grant_type: str = Form(...), code: str = Form(""),
          redirect_uri: str = Form(""), client_id: str = Form(""),
          code_verifier: str = Form(""), resource: str = Form("")):
    if grant_type != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
    try:
        result = oauth.exchange_code(code, client_id, redirect_uri, code_verifier,
                                     resource or resource_id(request))
    except oauth.OAuthError as e:
        return JSONResponse({"error": e.code, "error_description": e.description},
                            status_code=e.status)
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.post("/oauth/revoke")
def revoke(token: str = Form(...)) -> dict:
    oauth.revoke_token(token)
    # RFC 7009: always 200, even for an unknown token. Distinguishing them
    # would turn this into an oracle for guessing valid tokens.
    return {}


# ------------------------------------------------------- account controls --

@router.get("/api/connections")
def list_connections(request: Request) -> list[dict]:
    uid = _require_session(request)
    return oauth.list_grants(uid)


@router.delete("/api/connections/{grant_id}")
def revoke_connection(grant_id: str, request: Request) -> dict:
    uid = _require_session(request)
    if not oauth.revoke_grant(uid, grant_id):
        raise HTTPException(404, "No such connection.")
    return {"status": "revoked"}


def _require_session(request: Request) -> int:
    uid = auth.resolve_session(request.cookies.get(config.COOKIE_NAME))
    if uid is None:
        raise HTTPException(401, "Sign in to continue.")
    return uid


# ------------------------------------------------------------ mcp guard --

def bearer_user(request: Request) -> int:
    """Every MCP request is authenticated and audience-checked.

    A 401 here must carry WWW-Authenticate pointing at the metadata document,
    because that header is how an MCP client discovers where to authenticate.
    Without it the client cannot start the flow and simply fails.
    """
    header = request.headers.get("authorization", "")
    token_value = header[7:].strip() if header[:7].lower() == "bearer " else None
    uid = oauth.resolve_token(token_value, resource_id(request))
    if uid is None:
        raise HTTPException(
            401, "Authentication required.",
            headers={"WWW-Authenticate":
                     f'Bearer resource_metadata='
                     f'"{base_url(request)}/.well-known/oauth-protected-resource"'})
    return uid


def _escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def _page(inner: str) -> str:
    # A self-contained page: the consent screen must render before the SPA has
    # loaded, and must not depend on it.
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Recur</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 body{{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;background:#0d0d0d;
      color:#fff;display:flex;justify-content:center;padding:12vh 20px;margin:0}}
 .card{{background:#1a1a19;border:1px solid rgba(255,255,255,.1);border-radius:10px;
        padding:26px 28px;max-width:420px;width:100%}}
 h1{{font-size:20px;margin:0 0 12px}} p{{font-size:14px;line-height:1.55}}
 .muted{{color:#c3c2b7;font-size:13px}}
 button.primary{{width:100%;padding:11px;font:inherit;font-weight:600;color:#fff;
   background:#3987e5;border:0;border-radius:7px;cursor:pointer;margin-top:8px}}
</style></head><body><div class="card">{inner}</div></body></html>"""


# ---------------------------------------------------------- the endpoint --

PROTOCOL_VERSION = "2025-06-18"


@router.post(MCP_PATH)
async def mcp_endpoint(request: Request, uid: int = Depends(bearer_user)):
    """MCP over HTTP: JSON-RPC in, JSON-RPC out.

    Written directly rather than mounting the SDK's ASGI app, because every
    call has to be bound to the user the bearer token names. Handing an
    unscoped server object a request and hoping the right tenant is in scope is
    exactly the mistake this whole design is built to make impossible -- `uid`
    is a parameter of every tool, so there is no version of this that reads
    anyone else's data.
    """
    try:
        msg = await request.json()
    except Exception:
        return _rpc_error(None, -32700, "Parse error")
    if not isinstance(msg, dict):
        return _rpc_error(None, -32600, "Invalid request")

    rpc_id = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}

    if method == "initialize":
        return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "recur", "version": "1.0.0"},
        }})

    if method in ("notifications/initialized", "ping"):
        return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": {}})

    if method == "tools/list":
        return JSONResponse({"jsonrpc": "2.0", "id": rpc_id,
                             "result": {"tools": mcp_tools.schema()}})

    if method == "tools/call":
        name = params.get("name")
        try:
            result = mcp_tools.call(uid, name, params.get("arguments"))
        except KeyError:
            return _rpc_error(rpc_id, -32602, f"No such tool: {name}")
        except Exception:
            # The traceback goes to the log. Returning it would name tables and
            # columns to whoever holds a token.
            logging.getLogger("recur.mcp").exception("tool %s failed", name)
            return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": {
                "isError": True,
                "content": [{"type": "text", "text": "That tool failed."}]}})
        return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": {
            "content": [{"type": "text", "text": json.dumps(result, default=str, indent=1)}],
            "isError": False,
        }})

    return _rpc_error(rpc_id, -32601, f"Method not found: {method}")


def _rpc_error(rpc_id, code: int, message: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": rpc_id,
                         "error": {"code": code, "message": message}})
