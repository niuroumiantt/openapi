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
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from .db import KeyStore
from .upstreams import Catalogue

DB_PATH = os.environ.get("GATEWAY_DB", "keys.sqlite3")
ADMIN_TOKEN = os.environ.get("GATEWAY_ADMIN_TOKEN", "")   # empty = admin only from localhost
LOCAL_HOSTS = {"127.0.0.1", "::1", "testclient"}

app = FastAPI(title="model-key-gateway")
store = KeyStore(DB_PATH)
catalogue = Catalogue.load()
client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0))
STATIC = Path(__file__).parent / "static"


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
async def admin_page():
    return (STATIC / "index.html").read_text(encoding="utf-8")


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
    else:
        info["point_limit"], info["remaining_points"] = key["token_limit"], key["remaining_tokens"]
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
    body["model"] = route.model

    def charge(tokens: int) -> None:
        store.add_usage(key["id"], round(tokens * route.weight))

    stream = bool(body.get("stream")) and path != "embeddings"
    if stream:
        body.setdefault("stream_options", {})["include_usage"] = True
    upstream = client.build_request("POST", f"{route.base_url}/{path}", json=body, headers=_upstream_headers(route))

    if not stream:
        r = await client.send(upstream)
        is_json = r.headers.get("content-type", "").startswith("application/json")
        data = r.json() if is_json else {"error": r.text}
        if r.status_code == 200:
            charge(_usage_tokens(data) or _estimate(json.dumps(body) + r.text))
            if "model" in data:
                data["model"] = name
        return JSONResponse(content=data, status_code=r.status_code)

    r = await client.send(upstream, stream=True)
    if r.status_code != 200:
        text = await r.aread()
        await r.aclose()
        return JSONResponse(content=_safe_json(text), status_code=r.status_code)

    async def relay():
        seen_usage, collected = 0, []
        try:
            async for line in r.aiter_lines():
                yield line + "\n"
                if line.startswith("data: ") and line != "data: [DONE]":
                    try:
                        chunk = json.loads(line[6:])
                    except ValueError:
                        continue
                    if chunk.get("usage"):
                        seen_usage = _usage_tokens(chunk)
                    for ch in chunk.get("choices", []):
                        collected.append((ch.get("delta") or {}).get("content") or "")
        finally:
            await r.aclose()
            charge(seen_usage or _estimate(json.dumps(body) + "".join(collected)))

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
