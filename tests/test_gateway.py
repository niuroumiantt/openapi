import importlib
import json
import socket
import threading
import time

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


def _boot(monkeypatch, tmp_path, fake_ollama, config=None, admin_token=""):
    monkeypatch.setenv("GATEWAY_DB", str(tmp_path / "keys.sqlite3"))
    monkeypatch.setenv("GATEWAY_ADMIN_TOKEN", admin_token)
    if config is None:
        monkeypatch.setenv("OLLAMA_URL", fake_ollama)
        monkeypatch.setenv("GATEWAY_MODEL", "fake:27b")
        monkeypatch.setenv("GATEWAY_CONFIG", str(tmp_path / "missing.json"))
    else:
        cfg = tmp_path / "upstreams.json"
        cfg.write_text(json.dumps(config))
        monkeypatch.setenv("GATEWAY_CONFIG", str(cfg))
    import gateway.app as mod
    mod = importlib.reload(mod)
    from fastapi.testclient import TestClient
    return TestClient(mod.app), mod


@pytest.fixture()
def gw(fake_ollama, tmp_path, monkeypatch):
    c, _ = _boot(monkeypatch, tmp_path, fake_ollama)
    with c:
        yield c


@pytest.fixture()
def multi(fake_ollama, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "upstream-secret")
    cfg = {
        "upstreams": {"local": {"base_url": fake_ollama + "/v1"},
                      "paid": {"base_url": fake_ollama + "/v1", "api_key_env": "FAKE_KEY"}},
        "models": {"semifly-27b": {"upstream": "local", "model": "fake:27b", "weight": 1},
                   "deepseek-chat": {"upstream": "paid", "model": "fake:27b", "weight": 3}},
    }
    c, _ = _boot(monkeypatch, tmp_path, fake_ollama, config=cfg)
    with c:
        yield c


def _chat(gw, key, stream=False, model=None):
    body = {"messages": [{"role": "user", "content": "hi"}], "stream": stream}
    if model:
        body["model"] = model
    return gw.post("/v1/chat/completions", headers={"authorization": f"Bearer {key}"}, json=body)


def test_status_and_admin_page(gw):
    s = gw.get("/admin/status").json()
    assert s["ok"] is True and s["models"][0]["model"] == "fake:27b"
    assert "Model Key Gateway" in gw.get("/").text


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


def test_key_self_lookup(gw):
    k = gw.post("/admin/keys", json={"kind": "tokens", "tokens": 1000, "label": "demo"}).json()
    h = {"authorization": f"Bearer {k['key']}"}
    info = gw.get("/v1/key", headers=h).json()
    assert info["status"] == "active" and info["models"] == ["fake:27b"] and info["remaining_points"] == 1000
    _chat(gw, k["key"])
    info = gw.get("/v1/key", headers=h).json()
    assert info["remaining_points"] == 900 and info["requests"] == 1 and info["label"] == "demo"
    assert gw.get("/v1/models", headers=h).json()["data"][0]["id"] == "fake:27b"
    assert gw.get("/v1/key", headers={"authorization": "Bearer nope"}).status_code == 401


def test_multi_upstream_routing_weights_and_allowlist(multi):
    s = multi.get("/admin/status").json()
    assert {m["model"] for m in s["models"]} == {"semifly-27b", "deepseek-chat"}

    k = multi.post("/admin/keys", json={"kind": "tokens", "tokens": 1000}).json()   # all models
    r = _chat(multi, k["key"], model="semifly-27b")
    assert r.status_code == 200 and r.json()["model"] == "semifly-27b"           # public name echoed back
    _chat(multi, k["key"], model="deepseek-chat")                                  # 100 tok x 3 = 300
    info = multi.get("/v1/key", headers={"authorization": f"Bearer {k['key']}"}).json()
    assert info["points_used"] == 400 and set(info["models"]) == {"semifly-27b", "deepseek-chat"}
    assert _chat(multi, k["key"]).status_code == 400                               # ambiguous: model required
    assert _chat(multi, k["key"], model="gpt-9").status_code == 403

    restricted = multi.post("/admin/keys", json={"kind": "tokens", "tokens": 1000, "models": ["semifly-27b"]}).json()
    assert _chat(multi, restricted["key"]).status_code == 200                      # single allowed model = default
    assert _chat(multi, restricted["key"], model="deepseek-chat").status_code == 403
    assert multi.get("/v1/models", headers={"authorization": f"Bearer {restricted['key']}"}).json()["data"] == \
        [{"id": "semifly-27b", "object": "model", "owned_by": "semifly"}]
    assert multi.post("/admin/keys", json={"kind": "tokens", "tokens": 1, "models": ["nope"]}).status_code == 400


def test_upstream_key_is_forwarded(multi, fake_ollama):
    from tests.fake_ollama import seen_auth
    k = multi.post("/admin/keys", json={"kind": "time", "minutes": 5}).json()
    _chat(multi, k["key"], model="deepseek-chat")
    assert seen_auth[-1] == "Bearer upstream-secret"
    _chat(multi, k["key"], model="semifly-27b")
    assert seen_auth[-1] == ""


def test_admin_token(fake_ollama, tmp_path, monkeypatch):
    c, _ = _boot(monkeypatch, tmp_path, fake_ollama, admin_token="s3cret")
    with c:
        assert c.get("/admin/keys").status_code == 401
        assert c.get("/admin/keys", headers={"x-admin-token": "wrong"}).status_code == 401
        assert c.get("/admin/keys", headers={"x-admin-token": "s3cret"}).status_code == 200
        assert c.get("/").status_code == 200   # page itself is public, it asks for the token


def test_admin_is_localhost_only_without_token(gw):
    import gateway.app as mod
    class C: host = "10.0.0.5"
    class R:
        client = C(); headers = {}; cookies = {}
    with pytest.raises(Exception):
        mod._require_admin(R())
