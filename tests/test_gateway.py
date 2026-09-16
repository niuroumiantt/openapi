import os
import socket
import threading
import time

import httpx
import pytest
import uvicorn


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def fake_ollama():
    from tests.fake_ollama import app
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True


@pytest.fixture()
def gw(fake_ollama, tmp_path, monkeypatch):
    monkeypatch.setenv("OLLAMA_URL", fake_ollama)
    monkeypatch.setenv("GATEWAY_DB", str(tmp_path / "keys.sqlite3"))
    monkeypatch.setenv("GATEWAY_MODEL", "fake:27b")
    import importlib
    import gateway.app as mod
    mod = importlib.reload(mod)
    from fastapi.testclient import TestClient
    with TestClient(mod.app) as c:
        yield c


def _chat(gw, key, stream=False):
    return gw.post("/v1/chat/completions", headers={"authorization": f"Bearer {key}"},
                   json={"messages": [{"role": "user", "content": "hi"}], "stream": stream})


def test_status_and_admin_page(gw):
    assert gw.get("/admin/status").json()["ok"] is True
    assert "Ollama Key Gateway" in gw.get("/").text


def test_token_key_meters_and_exhausts(gw):
    k = gw.post("/admin/keys", json={"kind": "tokens", "tokens": 250}).json()
    assert k["key"].startswith("sk-local-")
    assert _chat(gw, k["key"]).status_code == 200            # 100 used
    r = _chat(gw, k["key"], stream=True)                      # 200 used
    assert r.status_code == 200 and "hello" in r.text
    assert _chat(gw, k["key"]).status_code == 200            # 300 used, last one allowed
    assert _chat(gw, k["key"]).status_code == 403            # exhausted
    row = gw.get("/admin/keys").json()[0]
    assert row["status"] == "exhausted" and row["tokens_used"] == 300 and row["requests"] == 3
    assert "key_hash" not in row and "key" not in row


def test_time_key_expires(gw, monkeypatch):
    k = gw.post("/admin/keys", json={"kind": "time", "minutes": 5}).json()
    assert _chat(gw, k["key"]).status_code == 200
    import gateway.db as db
    later = time.time() + 6 * 60
    monkeypatch.setattr(db.time, "time", lambda: later)
    assert _chat(gw, k["key"]).status_code == 403


def test_revoke_and_bad_keys(gw):
    k = gw.post("/admin/keys", json={"kind": "time", "minutes": 5}).json()
    assert gw.delete(f"/admin/keys/{k['id']}").status_code == 200
    assert _chat(gw, k["key"]).status_code == 403
    assert _chat(gw, "sk-local-nope").status_code == 401
    assert gw.post("/v1/chat/completions", json={}).status_code == 401
    assert gw.post("/admin/keys", json={"kind": "tokens"}).status_code == 400


def test_admin_is_localhost_only(gw):
    r = gw.get("/admin/keys", headers={"host": "x"})
    assert r.status_code == 200  # TestClient is treated as local
    from fastapi import Request
    import gateway.app as mod
    class C: host = "10.0.0.5"
    class R: client = C()
    with pytest.raises(Exception):
        mod._require_local(R())
