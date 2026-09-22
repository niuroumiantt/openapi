"""Model key gateway.

Sits in front of one or more OpenAI-compatible upstreams (a local Ollama, a
GPU box on AWS, DeepSeek, ...) and only lets through requests that carry a
valid, unexpired, unexhausted key. Every response is metered in *points*
(tokens x the model's weight) and charged against the key.

    client --(Bearer sk-local-...)--> gateway --> upstream chosen by `model`
"""
import hmac
import json
import os
import time
from pathlib import Path

import httpx
import stripe
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .db import KeyStore
from .mailer import Mailer
from .platform import PlatformStore
from .upstreams import Catalogue

DB_PATH = os.environ.get("GATEWAY_DB", "keys.sqlite3")
ADMIN_TOKEN = os.environ.get("GATEWAY_ADMIN_TOKEN", "")   # empty = admin only from localhost
CAPTURE_LABELS = {s.strip() for s in os.environ.get("GATEWAY_CAPTURE_LABELS", "").split(",") if s.strip()}
LOCAL_HOSTS = {"127.0.0.1", "::1", "testclient"}
SESSION_COOKIE = "semifly_session"
COOKIE_SECURE = os.environ.get("SEMIFLY_COOKIE_SECURE", "1") != "0"
REQUIRE_VERIFIED_EMAIL = os.environ.get("SEMIFLY_REQUIRE_VERIFIED_EMAIL", "1") != "0"
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
ENABLE_DOCS = os.environ.get("SEMIFLY_ENABLE_DOCS", "0") == "1"
MAX_COMPLETION_TOKENS = int(os.environ.get("SEMIFLY_MAX_COMPLETION_TOKENS", "4096"))

app = FastAPI(title="Semifly API", docs_url="/docs" if ENABLE_DOCS else None,
              redoc_url=None, openapi_url="/openapi.json" if ENABLE_DOCS else None)
store = KeyStore(DB_PATH)
platform = PlatformStore(DB_PATH)
mailer = Mailer()
catalogue = Catalogue.load()
client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0))
STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def _same_origin(request: Request) -> None:
    """Cookie-authenticated writes may only come from this site."""
    origin = request.headers.get("origin")
    # Behind TLS-terminating Caddy, request.base_url is the internal HTTP URL.
    # The explicitly configured public origin is therefore the authority for
    # browser write checks as well as links in transactional email.
    expected_origin = os.environ.get("SEMIFLY_PUBLIC_BASE_URL", str(request.base_url)).rstrip("/")
    if origin and origin.rstrip("/") != expected_origin:
        raise HTTPException(403, "same-origin request required")


def _portal_user(request: Request) -> dict:
    raw = request.cookies.get(SESSION_COOKIE, "")
    user = platform.session_user(raw) if raw else None
    if not user:
        raise HTTPException(401, "sign in required")
    return user


def _system_admin(request: Request) -> dict:
    user = _portal_user(request)
    if user["role"] != "system_admin":
        raise HTTPException(403, "system administrator required")
    return user


def _session_response(user: dict, token: str, *, status_code: int = 200) -> JSONResponse:
    response = JSONResponse({"user": user}, status_code=status_code)
    response.set_cookie(SESSION_COOKIE, token, httponly=True, secure=COOKIE_SECURE,
                        samesite="strict", max_age=60 * 60 * 24 * 14, path="/")
    return response


def _bootstrap_admin() -> None:
    email, password = os.environ.get("SEMIFLY_ADMIN_EMAIL", ""), os.environ.get("SEMIFLY_ADMIN_PASSWORD", "")
    if password and not email:
        raise RuntimeError("SEMIFLY_ADMIN_PASSWORD requires SEMIFLY_ADMIN_EMAIL")
    if email:
        if password:
            platform.bootstrap_admin(email=email, password=password)


_bootstrap_admin()


def _public_base(request: Request) -> str:
    return os.environ.get("SEMIFLY_PUBLIC_BASE_URL", str(request.base_url).rstrip("/")).rstrip("/")


def _send_email_token(request: Request, user: dict, purpose: str) -> bool:
    if not mailer.configured:
        return False
    try:
        if purpose == "verify_email":
            raw = platform.issue_email_token(user["id"], purpose, lifetime_seconds=60 * 60 * 24)
            subject = "Verify your Semifly email"
            url = f"{_public_base(request)}/auth/verify?token={raw}"
            text = f"Verify your Semifly account within 24 hours:\n\n{url}\n\nIf you did not request this, ignore this email."
        else:
            raw = platform.issue_email_token(user["id"], purpose, lifetime_seconds=60 * 30)
            subject = "Reset your Semifly password"
            url = f"{_public_base(request)}/auth/reset?token={raw}"
            text = f"Reset your Semifly password within 30 minutes:\n\n{url}\n\nIf you did not request this, ignore this email."
    except ValueError:
        return False
    return mailer.send(recipient=user["email"], subject=subject, text=text)


@app.middleware("http")
async def protect_local_admin(request: Request, call_next):
    if request.url.path.startswith("/admin/") and not ADMIN_TOKEN:
        if request.url.hostname not in {"localhost", "127.0.0.1", "::1", "testserver"}:
            return JSONResponse({"detail": "local admin host required"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail": "same-origin admin access required"}, status_code=403)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Content-Security-Policy",
                                "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
                                "form-action 'self'; connect-src 'self'; object-src 'none'")
    if request.url.path.startswith(("/admin/", "/auth/", "/portal/")):
        response.headers["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------------------------
# admin
# ---------------------------------------------------------------------------

def _require_admin(request: Request) -> None:
    if ADMIN_TOKEN:
        given = request.headers.get("x-admin-token") or request.cookies.get("admin_token") or ""
        if not hmac.compare_digest(given, ADMIN_TOKEN):
            raise HTTPException(401, "admin token required")
        return
    host = request.client.host if request.client else ""
    if host not in LOCAL_HOSTS:
        raise HTTPException(403, "admin endpoints are only available from localhost "
                                 "(set GATEWAY_ADMIN_TOKEN to manage remotely)")


@app.get("/", response_class=HTMLResponse)
async def customer_portal():
    return (STATIC / "portal.html").read_text(encoding="utf-8")


@app.get("/admin/console", response_class=HTMLResponse)
async def admin_page(request: Request):
    _require_admin(request)
    return (STATIC / "index.html").read_text(encoding="utf-8")


@app.get("/health")
async def health():
    """Unauthenticated liveness endpoint; never reports credentials or account data."""
    return {"ok": True, "service": "semifly"}


# ---------------------------------------------------------------------------
# customer identity and catalogue
# ---------------------------------------------------------------------------

@app.post("/auth/register", status_code=201)
async def register(request: Request):
    _same_origin(request)
    body = await request.json()
    if REQUIRE_VERIFIED_EMAIL and not mailer.configured:
        raise HTTPException(503, "account email delivery is not configured")
    try:
        user = platform.create_user(email=str(body.get("email", "")), username=str(body.get("username", "")),
                                    password=str(body.get("password", "")), verified=not REQUIRE_VERIFIED_EMAIL)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if REQUIRE_VERIFIED_EMAIL:
        if not _send_email_token(request, user, "verify_email"):
            platform.remove_unverified_user(user["id"])
            raise HTTPException(503, "could not send verification email")
        return JSONResponse({"user": user, "verification_required": True}, status_code=201)
    return _session_response(user, platform.create_session(user["id"]), status_code=201)


@app.post("/auth/login")
async def login(request: Request):
    _same_origin(request)
    body = await request.json()
    identity = str(body.get("identity", ""))
    remote_addr = request.client.host if request.client else "unknown"
    if not platform.login_allowed(identity, remote_addr):
        raise HTTPException(429, "too many sign-in attempts; try again later")
    user = platform.authenticate(identity, str(body.get("password", "")))
    if not user or (REQUIRE_VERIFIED_EMAIL and not user["email_verified"]):
        # Do not distinguish an unknown account from a wrong password.
        platform.record_login_failure(identity, remote_addr)
        raise HTTPException(401, "invalid sign-in credentials")
    platform.clear_login_failures(identity, remote_addr)
    return _session_response(user, platform.create_session(user["id"]))


@app.post("/auth/logout")
async def logout(request: Request):
    _same_origin(request)
    raw = request.cookies.get(SESSION_COOKIE, "")
    if raw:
        platform.revoke_session(raw)
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/auth/me")
async def current_user(request: Request):
    return _portal_user(request)


@app.get("/auth/verify")
async def verify_email(token: str):
    user = platform.verify_email(token)
    if not user:
        raise HTTPException(400, "verification link is invalid or expired")
    return {"ok": True, "email": user["email"]}


@app.post("/auth/verify/resend", status_code=202)
async def resend_verification(request: Request):
    _same_origin(request)
    body = await request.json()
    user = platform.pending_user_by_email(str(body.get("email", "")))
    if user and not user["email_verified"] and mailer.configured:
        _send_email_token(request, user, "verify_email")
    # Same status in every case prevents account enumeration.
    return {"ok": True}


@app.post("/auth/password/reset", status_code=202)
async def request_password_reset(request: Request):
    _same_origin(request)
    body = await request.json()
    user = platform.pending_user_by_email(str(body.get("email", "")))
    if user and mailer.configured:
        _send_email_token(request, user, "reset_password")
    return {"ok": True}


@app.post("/auth/password/reset/confirm")
async def confirm_password_reset(request: Request):
    _same_origin(request)
    body = await request.json()
    try:
        changed = platform.reset_password(str(body.get("token", "")), str(body.get("password", "")))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if not changed:
        raise HTTPException(400, "reset link is invalid or expired")
    return {"ok": True}


@app.get("/portal/catalog")
async def customer_catalogue():
    """Public products only; prices are read from the server-side product catalogue."""
    return platform.catalogue()


@app.get("/portal/dashboard")
async def customer_dashboard(request: Request):
    return platform.dashboard(_portal_user(request)["id"])


@app.get("/portal/usage")
async def customer_usage(request: Request):
    """Customer-scoped usage only; request content is never exposed here."""
    return platform.dashboard(_portal_user(request)["id"])["usage"]


@app.post("/portal/projects", status_code=201)
async def customer_project(request: Request):
    _same_origin(request)
    user = _portal_user(request)
    body = await request.json()
    try:
        return platform.create_project(user["id"], str(body.get("name", "")))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/portal/projects/{project_id}/keys", status_code=201)
async def customer_key(request: Request, project_id: int):
    """Create a customer API key; its balance belongs to the account, not this key."""
    _same_origin(request)
    user = _portal_user(request)
    if not platform.owns_project(user["id"], project_id):
        raise HTTPException(404, "project was not found")
    body = await request.json()
    models = body.get("models")
    if not isinstance(models, list) or not models:
        raise HTTPException(400, "choose at least one model")
    unknown = [m for m in models if m not in catalogue.routes]
    if unknown:
        raise HTTPException(400, f"unknown models: {unknown}")
    try:
        raw, meta = store.create("account", label=str(body.get("label", ""))[:64], models=models,
                                 user_id=user["id"], project_id=project_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"key": raw, **meta}


@app.get("/portal/keys")
async def customer_keys(request: Request):
    return platform.api_keys(_portal_user(request)["id"])


@app.delete("/portal/keys/{key_id}")
async def customer_revoke_key(request: Request, key_id: int):
    _same_origin(request)
    user = _portal_user(request)
    if key_id not in {k["id"] for k in platform.api_keys(user["id"])}:
        raise HTTPException(404, "API key was not found")
    store.revoke(key_id)
    return {"ok": True}


@app.post("/portal/admin/products", status_code=201)
async def create_product(request: Request):
    _same_origin(request)
    admin = _system_admin(request)
    body = await request.json()
    if not catalogue.get(str(body.get("model", ""))):
        raise HTTPException(400, "product model must be an enabled public model route")
    try:
        product = platform.create_product(code=str(body.get("code", "")), model=str(body.get("model", "")),
                                          token_amount=int(body.get("token_amount", 0)),
                                          price_cents=int(body.get("price_cents", -1)),
                                          currency=str(body.get("currency", "usd")))
        platform.audit(actor_user_id=admin["id"], action="product.created", detail={"product_id": product["id"]})
        return product
    except (TypeError, ValueError) as e:
        raise HTTPException(400, str(e)) from e


@app.get("/portal/admin/users")
async def list_users(request: Request):
    _system_admin(request)
    return platform.admin_users()


@app.patch("/portal/admin/users/{user_id}")
async def update_user(request: Request, user_id: int):
    _same_origin(request)
    admin = _system_admin(request)
    body = await request.json()
    try:
        return platform.update_user(actor_user_id=admin["id"], target_user_id=user_id,
                                    role=body.get("role"), disabled=body.get("disabled"))
    except ValueError as e:
        raise HTTPException(400 if "last active" in str(e) else 404, str(e)) from e


@app.post("/portal/checkout", status_code=201)
async def create_checkout(request: Request):
    """Create a Stripe-hosted one-time, model-specific token purchase."""
    _same_origin(request)
    user = _portal_user(request)
    body = await request.json()
    try:
        product_id = int(body.get("product_id"))
    except (TypeError, ValueError) as e:
        raise HTTPException(400, "valid product_id is required") from e
    product = platform.product(product_id)
    if not product:
        raise HTTPException(404, "product is unavailable")
    if not STRIPE_SECRET_KEY:
        raise HTTPException(503, "Stripe checkout is not configured")
    order = platform.create_order(user["id"], product, provider="stripe")
    stripe.api_key = STRIPE_SECRET_KEY
    try:
        session = stripe.checkout.Session.create(
            mode="payment", customer_email=user["email"], client_reference_id=str(order["id"]),
            line_items=[{"price_data": {"currency": product["currency"], "unit_amount": product["price_cents"],
                "product_data": {"name": f"{product['model']} · {product['token_amount']:,} tokens"}}, "quantity": 1}],
            success_url=_public_base(request) + "?checkout=success",
            cancel_url=_public_base(request) + "?checkout=cancelled",
            metadata={"semifly_order_id": str(order["id"]), "product_code": product["code"]},
        )
        platform.attach_checkout(order["id"], session.id)
    except Exception as e:  # Stripe exceptions deliberately do not reach the customer.
        raise HTTPException(502, "could not start payment checkout") from e
    return {"checkout_url": session.url, "order_id": order["id"]}


@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request):
    """The only route allowed to credit a paid Stripe purchase."""
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(503, "Stripe webhook is not configured")
    payload = await request.body()
    try:
        event = stripe.Webhook.construct_event(payload, request.headers.get("stripe-signature", ""), STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        raise HTTPException(400, "invalid Stripe webhook") from e
    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        if session["payment_status"] == "paid":
            platform.record_paid_order(provider="stripe", event_id=event["id"], payload=payload,
                                       checkout_id=session["id"])
    return {"received": True}


@app.get("/admin/status")
async def admin_status(request: Request):
    _require_admin(request)
    out = []
    for r in catalogue.routes.values():
        try:
            resp = await client.get(f"{r.base_url}/models", headers=_upstream_headers(r), timeout=5.0)
            ok = resp.status_code == 200
            err = "" if ok else f"HTTP {resp.status_code}"
        except Exception as e:  # noqa: BLE001
            ok, err = False, str(e)
        out.append({"model": r.public_name, "upstream": r.upstream, "weight": r.weight, "ok": ok, "error": err})
    return {"ok": any(m["ok"] for m in out) if out else False, "models": out}


@app.get("/admin/keys")
async def admin_list(request: Request):
    _require_admin(request)
    return store.list()


@app.get("/admin/usage")
async def admin_usage(request: Request, limit: int = 100):
    _require_admin(request)
    return store.usage(limit)


@app.get("/admin/usage/{event_id}/content")
async def admin_content(event_id: int, request: Request):
    _require_admin(request)
    value = store.content(event_id)
    if value is None:
        raise HTTPException(404, "content was not recorded")
    return JSONResponse(value, headers={"Cache-Control": "no-store"})


@app.post("/admin/keys", status_code=201)
async def admin_create(request: Request):
    _require_admin(request)
    body = await request.json()
    models = body.get("models")
    if models is not None:
        unknown = [m for m in models if m not in catalogue.routes]
        if unknown:
            raise HTTPException(400, f"unknown models: {unknown}")
    try:
        raw, meta = store.create(
            body.get("kind", ""),
            minutes=body.get("minutes"),
            tokens=body.get("tokens"),
            label=str(body.get("label", ""))[:64],
            models=models or None,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"key": raw, **meta}      # the raw key is returned exactly once


@app.delete("/admin/keys/{key_id}")
async def admin_revoke(request: Request, key_id: int):
    _require_admin(request)
    if not store.revoke(key_id):
        raise HTTPException(404, "no such key")
    return {"ok": True}


# ---------------------------------------------------------------------------
# OpenAI-compatible API
# ---------------------------------------------------------------------------

def _bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer key")
    return auth[7:].strip()


def _auth(request: Request) -> dict:
    key = store.lookup(_bearer(request))
    if key is None:
        raise HTTPException(401, "invalid key")
    if key["status"] != "active":
        raise HTTPException(403, f"key is {key['status']}")
    return key


def _allowed(key: dict) -> list[str]:
    return catalogue.names() if key["models"] == "*" else [m for m in key["models"] if m in catalogue.routes]


def _upstream_headers(route) -> dict:
    return {"authorization": f"Bearer {route.api_key}"} if route.api_key else {}


def _usage_tokens(payload: dict) -> int:
    u = payload.get("usage") or {}
    return int(u.get("total_tokens") or (u.get("prompt_tokens", 0) + u.get("completion_tokens", 0)))


def _estimate(text: str) -> int:
    """Fallback when upstream sends no usage: ~4 chars per token."""
    return max(1, len(text) // 4)


@app.get("/v1/key")
async def key_info(request: Request):
    """Self-service lookup: what does this key allow, and how much is left?"""
    key = store.lookup(_bearer(request))
    if key is None:
        raise HTTPException(401, "invalid key")
    info = {
        "status": key["status"], "kind": key["kind"], "label": key["label"],
        "models": _allowed(key), "created_at": key["created_at"],
        "requests": key["requests"], "points_used": key["tokens_used"],
    }
    if key["kind"] == "time":
        info["expires_at"], info["remaining_seconds"] = key["expires_at"], key["remaining_seconds"]
    elif key["kind"] == "tokens":
        info["point_limit"], info["remaining_points"] = key["token_limit"], key["remaining_tokens"]
    else:
        info["model_balances"] = platform.balances(key["user_id"])
    return info


@app.get("/v1/models")
async def models(request: Request):
    key = _auth(request)
    return {"object": "list", "data": [{"id": m, "object": "model", "owned_by": "semifly"} for m in _allowed(key)]}


@app.post("/v1/{path:path}")
async def proxy(request: Request, path: str):
    if path not in ("chat/completions", "completions", "embeddings"):
        raise HTTPException(404, "unsupported endpoint")
    key = _auth(request)
    body = await request.json()

    allowed = _allowed(key)
    name = body.get("model") or (allowed[0] if len(allowed) == 1 else None)
    if not name:
        raise HTTPException(400, f"model is required; this key may use: {allowed}")
    route = catalogue.get(name)
    if route is None or name not in allowed:
        raise HTTPException(403, f"model '{name}' is not available to this key; allowed: {allowed}")
    if key["kind"] == "account":
        if not key.get("user_id") or not platform.can_use_model(key["user_id"], name):
            raise HTTPException(403, f"no remaining {name} capacity")
    body["model"] = route.model

    reservation_id = None
    if key["kind"] == "account":
        if "max_tokens" in body and "max_completion_tokens" in body:
            raise HTTPException(400, "send only one of max_tokens or max_completion_tokens")
        requested_output = body.get("max_completion_tokens", body.get("max_tokens", MAX_COMPLETION_TOKENS))
        if type(requested_output) is not int or requested_output < 1 or requested_output > MAX_COMPLETION_TOKENS:
            raise HTTPException(400, f"max_tokens must be between 1 and {MAX_COMPLETION_TOKENS}")
        # Byte-pair tokenizers cannot emit more tokens than the UTF-8 byte
        # length of the serialized prompt. This deliberately conservative
        # bound prevents a final request from spending unpaid capacity.
        reserve_tokens = len(json.dumps(body, ensure_ascii=False).encode("utf-8")) + requested_output
        reservation_id = platform.reserve_model_tokens(key["user_id"], name, reserve_tokens)
        if reservation_id is None:
            raise HTTPException(403, f"insufficient {name} capacity for this request")

    started = time.monotonic()
    capture = key['label'] in CAPTURE_LABELS
    messages = [{"role": m.get("role"), "content": m.get("content")}
                for m in body.get("messages", [])] if capture else []

    def charge(tokens: int, usage=None, status="ok", actual_model=None, answer="") -> None:
        # A customer key consumes raw model tokens from the account-wide,
        # model-specific ledger. Legacy operator keys retain weighted points.
        if reservation_id and not platform.settle_reservation(reservation_id, tokens):
            # A non-compliant upstream exceeded the pre-reserved maximum. The
            # whole reservation was charged and the event is marked for audit.
            status = "usage_settlement_error"
        store.record_usage(key["id"], model=name, upstream=route.upstream,
                           actual_model=actual_model or route.model,
                           elapsed_ms=round((time.monotonic() - started) * 1000),
                           status=status, usage=usage or {},
                           points=round(tokens * route.weight),
                           usage_source="reported" if usage else "estimated" if tokens else "unknown",
                           content={"project": key['label'], "messages": messages, "answer": answer}
                           if capture else None)

    stream = bool(body.get("stream")) and path != "embeddings"
    if stream:
        body.setdefault("stream_options", {})["include_usage"] = True
    upstream = client.build_request("POST", f"{route.base_url}/{path}", json=body, headers=_upstream_headers(route))

    if not stream:
        try:
            r = await client.send(upstream)
        except httpx.RequestError:
            charge(0, status="transport_error")
            return JSONResponse({"error": "upstream unavailable"}, status_code=502)
        is_json = r.headers.get("content-type", "").startswith("application/json")
        data = r.json() if is_json else {"error": r.text}
        if r.status_code == 200:
            usage = data.get("usage")
            truncated = any(c.get("finish_reason") == "length" for c in data.get("choices", []))
            charge(_usage_tokens(data) if usage else _estimate(json.dumps(body) + r.text),
                   usage, status="output_limit" if truncated else "ok",
                   actual_model=data.get("model"), answer="\n".join(
                       c.get("message", {}).get("content") or "" for c in data.get("choices", [])))
            if "model" in data:
                data["model"] = name
        else:
            charge(0, status=f"http_{r.status_code}")
        return JSONResponse(content=data, status_code=r.status_code)

    try:
        r = await client.send(upstream, stream=True)
    except httpx.RequestError:
        charge(0, status="transport_error")
        return JSONResponse({"error": "upstream unavailable"}, status_code=502)
    if r.status_code != 200:
        text = await r.aread()
        await r.aclose()
        charge(0, status=f"http_{r.status_code}")
        return JSONResponse(content=_safe_json(text), status_code=r.status_code)

    async def relay():
        seen_usage, collected, usage, completed = 0, [], None, False
        try:
            async for line in r.aiter_lines():
                yield line + "\n"
                if line == "data: [DONE]":
                    completed = True
                if line.startswith("data: ") and line != "data: [DONE]":
                    try:
                        chunk = json.loads(line[6:])
                    except ValueError:
                        continue
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                        seen_usage = _usage_tokens(chunk)
                    for ch in chunk.get("choices", []):
                        collected.append((ch.get("delta") or {}).get("content") or "")
        finally:
            await r.aclose()
            charge(seen_usage if usage else _estimate(json.dumps(body) + "".join(collected)),
                   usage, status="ok" if completed else "incomplete_stream",
                   answer="".join(collected))

    return StreamingResponse(relay(), media_type="text/event-stream",
                             headers={"cache-control": "no-cache", "x-accel-buffering": "no"})


def _safe_json(raw: bytes):
    try:
        return json.loads(raw)
    except ValueError:
        return {"error": raw.decode(errors="replace")}


def main() -> None:
    import uvicorn
    host = os.environ.get("GATEWAY_HOST", "127.0.0.1")
    port = int(os.environ.get("GATEWAY_PORT", "8800"))
    print(f"管理页面：http://{host}:{port}   模型：{catalogue.names() or '(无，请配置 upstreams.json 或 GATEWAY_MODEL)'}")
    uvicorn.run("gateway.app:app", host=host, port=port, log_level="info", proxy_headers=True)


if __name__ == "__main__":
    main()
