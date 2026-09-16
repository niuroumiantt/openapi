"""Ollama key gateway.

Sits in front of Ollama's OpenAI-compatible endpoint and only lets through
requests that carry a valid, unexpired, unexhausted key. Every response is
metered and charged against the key.

    client --(Bearer sk-local-...)--> gateway :8800 --> ollama :11434
"""
import json
import os
import re
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from .db import KeyStore

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
MODEL = os.environ.get("GATEWAY_MODEL", "")          # empty = let the client choose
DB_PATH = os.environ.get("GATEWAY_DB", "keys.sqlite3")
ADMIN_HOSTS = {"127.0.0.1", "::1", "testclient"}

app = FastAPI(title="ollama-key-gateway")
store = KeyStore(DB_PATH)
client = httpx.AsyncClient(base_url=OLLAMA_URL, timeout=httpx.Timeout(600.0, connect=10.0))
STATIC = Path(__file__).parent / "static"


# ---------------------------------------------------------------------------
# admin (localhost only)
# ---------------------------------------------------------------------------

def _require_local(request: Request) -> None:
    host = request.client.host if request.client else ""
    if host not in ADMIN_HOSTS:
        raise HTTPException(403, "admin endpoints are only available from localhost")


@app.get("/", response_class=HTMLResponse)
async def admin_page(request: Request):
    _require_local(request)
    return (STATIC / "index.html").read_text(encoding="utf-8")


@app.get("/admin/status")
async def admin_status(request: Request):
    _require_local(request)
    try:
        r = await client.get("/api/tags", timeout=3.0)
        r.raise_for_status()
        models = [m["name"] for m in r.json().get("models", [])]
        return {"ok": True, "ollama": OLLAMA_URL, "model": MODEL or (models[0] if models else ""), "models": models}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "ollama": OLLAMA_URL, "error": str(e)}


@app.get("/admin/keys")
async def admin_list(request: Request):
    _require_local(request)
    return store.list()


@app.post("/admin/keys", status_code=201)
async def admin_create(request: Request):
    _require_local(request)
    body = await request.json()
    try:
        raw, meta = store.create(
            body.get("kind", ""),
            minutes=body.get("minutes"),
            tokens=body.get("tokens"),
            label=str(body.get("label", ""))[:64],
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"key": raw, **meta}      # the raw key is returned exactly once


@app.delete("/admin/keys/{key_id}")
async def admin_revoke(request: Request, key_id: int):
    _require_local(request)
    if not store.revoke(key_id):
        raise HTTPException(404, "no such key")
    return {"ok": True}


# ---------------------------------------------------------------------------
# OpenAI-compatible proxy
# ---------------------------------------------------------------------------

def _auth(request: Request) -> dict:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer key")
    key = store.lookup(auth[7:].strip())
    if key is None:
        raise HTTPException(401, "invalid key")
    if key["status"] != "active":
        raise HTTPException(403, f"key is {key['status']}")
    return key


def _usage_tokens(payload: dict) -> int:
    u = payload.get("usage") or {}
    return int(u.get("total_tokens") or (u.get("prompt_tokens", 0) + u.get("completion_tokens", 0)))


def _estimate(text: str) -> int:
    """Fallback when upstream sends no usage: ~4 chars per token."""
    return max(1, len(text) // 4)


@app.get("/v1/models")
async def models(request: Request):
    _auth(request)
    r = await client.get("/v1/models")
    return JSONResponse(r.json(), status_code=r.status_code)


@app.post("/v1/{path:path}")
async def proxy(request: Request, path: str):
    if path not in ("chat/completions", "completions", "embeddings"):
        raise HTTPException(404, "unsupported endpoint")
    key = _auth(request)
    body = await request.json()
    if MODEL:
        body["model"] = MODEL
    stream = bool(body.get("stream")) and path != "embeddings"
    if stream:
        body.setdefault("stream_options", {})["include_usage"] = True

    upstream = client.build_request("POST", f"/v1/{path}", json=body)

    if not stream:
        r = await client.send(upstream)
        if r.status_code == 200:
            try:
                data = r.json()
                tokens = _usage_tokens(data) or _estimate(json.dumps(body) + r.text)
            except ValueError:
                tokens = _estimate(json.dumps(body) + r.text)
            store.add_usage(key["id"], tokens)
        return JSONResponse(content=r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text,
                            status_code=r.status_code)

    r = await client.send(upstream, stream=True)
    if r.status_code != 200:
        text = await r.aread()
        await r.aclose()
        return JSONResponse(content=_safe_json(text), status_code=r.status_code)

    async def relay():
        seen_usage = 0
        collected = []
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
            store.add_usage(key["id"], seen_usage or _estimate(json.dumps(body) + "".join(collected)))

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
    print(f"管理页面：http://{host}:{port}   (Ollama: {OLLAMA_URL})")
    uvicorn.run("gateway.app:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
