import importlib
import hashlib
import hmac
import json
import socket
import threading
import time
from types import SimpleNamespace

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


def _boot(monkeypatch, tmp_path, fake_ollama, config=None, admin_token="", stripe_webhook_secret="", stripe_secret_key=""):
    monkeypatch.setenv("GATEWAY_DB", str(tmp_path / "keys.sqlite3"))
    monkeypatch.setenv("GATEWAY_ADMIN_TOKEN", admin_token)
    monkeypatch.setenv("SEMIFLY_COOKIE_SECURE", "0")
    monkeypatch.setenv("SEMIFLY_REQUIRE_VERIFIED_EMAIL", "0")
    monkeypatch.delenv("SEMIFLY_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("SEMIFLY_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    if stripe_secret_key:
        monkeypatch.setenv("STRIPE_SECRET_KEY", stripe_secret_key)
    if stripe_webhook_secret:
        monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", stripe_webhook_secret)
    else:
        monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
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


def test_local_admin_rejects_cross_origin(gw):
    assert gw.get("/admin/keys", headers={"Origin": "https://evil.test"}).status_code == 403
    assert gw.get("/admin/keys", headers={"Host": "evil.test"}).status_code == 403
    assert gw.get("/admin/keys").headers["cache-control"] == "no-store"


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
    body = {"messages": [{"role": "user", "content": "hi"}], "stream": stream, "max_tokens": 100}
    if model:
        body["model"] = model
    return gw.post("/v1/chat/completions", headers={"authorization": f"Bearer {key}"}, json=body)


def test_status_and_admin_page(gw):
    s = gw.get("/admin/status").json()
    assert s["ok"] is True and s["models"][0]["model"] == "fake:27b"
    assert "Semifly" in gw.get("/").text
    assert gw.get("/docs").status_code == 404
    assert gw.get("/admin/console").status_code == 200


def test_content_is_opt_in_and_admin_only(fake_ollama, tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_CAPTURE_LABELS", "mail2leads")
    client, mod = _boot(monkeypatch, tmp_path, fake_ollama, admin_token="private-admin")
    admin = {"x-admin-token": "private-admin"}
    with client:
        k = client.post("/admin/keys", headers=admin, json={
            "kind": "tokens", "tokens": 1000, "label": "mail2leads"}).json()
        assert _chat(client, k['key']).status_code == 200
        rows = client.get('/admin/usage', headers=admin).json()
        assert rows[0]['has_content'] == 1
        assert 'messages' not in rows[0]
        url = f"/admin/usage/{rows[0]['id']}/content"
        assert client.get(url, headers={'authorization': 'Bearer '+k['key']}).status_code == 401
        detail = client.get(url, headers=admin).json()
        assert detail['messages'][0]['content'] == 'hi'
        assert detail['answer'] == 'hello world'
        assert client.get(url, headers=admin).headers['cache-control'] == 'no-store'
        assert _chat(client, k['key'], stream=True).status_code == 200
        newest = client.get('/admin/usage', headers=admin).json()[0]
        streamed = client.get(f"/admin/usage/{newest['id']}/content", headers=admin).json()
        assert streamed['answer'] == 'hello world'
        other = client.post('/admin/keys', headers=admin, json={
            'kind': 'tokens', 'tokens': 1000, 'label': 'other'}).json()
        assert _chat(client, other['key']).status_code == 200
        assert client.get('/admin/usage', headers=admin).json()[0]['has_content'] == 0


def test_usage_ledger_records_reported_tokens_for_both_modes(gw):
    k = gw.post("/admin/keys", json={"kind": "tokens", "tokens": 1000}).json()
    assert _chat(gw, k["key"]).status_code == 200
    assert _chat(gw, k["key"], stream=True).status_code == 200
    rows = gw.get("/admin/usage").json()
    assert len(rows) == 2
    for row in rows:
        assert row["prompt_tokens"] == 40
        assert row["completion_tokens"] == 60
        assert row["total_tokens"] == 100
        assert row["usage_source"] == "reported"
        assert row["status"] == "ok"
        assert row["elapsed_ms"] >= 0
        assert k["key"] not in json.dumps(row)


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
        assert c.get("/").status_code == 200
        assert c.get("/admin/console").status_code == 401
        assert c.get("/admin/console", headers={"x-admin-token": "s3cret"}).status_code == 200


def test_admin_is_localhost_only_without_token(gw):
    import gateway.app as mod
    class C: host = "10.0.0.5"
    class R:
        client = C(); headers = {}; cookies = {}
    with pytest.raises(Exception):
        mod._require_admin(R())


def test_customer_accounts_projects_catalogue_and_model_balances(fake_ollama, tmp_path, monkeypatch):
    config = {
        'upstreams': {'local': {'base_url': fake_ollama + '/v1'}},
        'models': {
            'fake:27b': {'upstream': 'local', 'model': 'fake:27b', 'weight': 1},
            'deepseek-chat': {'upstream': 'local', 'model': 'fake:27b', 'weight': 1},
        },
    }
    client, mod = _boot(monkeypatch, tmp_path, fake_ollama, config=config)
    with client:
        assert client.get('/portal/models').status_code == 401
        assert client.post('/auth/register', json={'email': 'alice@example.com', 'username': 'alice',
                                                    'password': 'a secure password'}).status_code == 201
        assert client.get('/portal/models').json() == [{'id': name} for name in mod.catalogue.names()]
        assert all(set(item) == {'id'} for item in client.get('/portal/models').json())
        assert client.get('/auth/me').headers['cache-control'] == 'no-store'
        assert client.get('/auth/me').headers['x-frame-options'] == 'DENY'
        assert client.get('/auth/me').json()['username'] == 'alice'
        assert client.post('/portal/projects', json={'name': 'production'}).status_code == 201
        assert client.post('/portal/projects', headers={'Origin': 'https://evil.test'},
                           json={'name': 'stolen'}).status_code == 403
        admin = mod.platform.bootstrap_admin(email='admin@example.com', password='a different secure password')
        admin_client, _ = _boot(monkeypatch, tmp_path, fake_ollama, config=config)
        with admin_client:
            assert admin_client.post('/auth/login', json={'identity': 'admin', 'password': 'a different secure password'}).status_code == 200
            product = admin_client.post('/portal/admin/products', json={'code': 'deepseek-5m', 'model': 'deepseek-chat',
                'token_amount': 5_000_000, 'price_cents': 1500, 'currency': 'usd'})
            assert product.status_code == 201
            fake_product = admin_client.post('/portal/admin/products', json={'code': 'fake-1k', 'model': 'fake:27b',
                'token_amount': 1_000, 'price_cents': 100, 'currency': 'usd'})
            assert fake_product.status_code == 201
        assert {p['code'] for p in client.get('/portal/catalog').json()} == {'deepseek-5m', 'fake-1k'}
        order = mod.platform.create_order(1, mod.platform.product(product.json()['id']), provider='stripe', checkout_id='cs_test')
        paid = mod.platform.record_paid_order(provider='stripe', event_id='evt_test', payload=b'{"test":true}', checkout_id='cs_test')
        assert paid['id'] == order['id']
        assert mod.platform.record_paid_order(provider='stripe', event_id='evt_test', payload=b'{"test":true}', checkout_id='cs_test') is None
        fake_order = mod.platform.create_order(1, mod.platform.product(fake_product.json()['id']), provider='stripe', checkout_id='cs_fake')
        assert mod.platform.record_paid_order(provider='stripe', event_id='evt_fake', payload=b'{"fake":true}', checkout_id='cs_fake')['id'] == fake_order['id']
        key = client.post('/portal/projects/1/keys', json={'label': 'production key', 'models': ['fake:27b']})
        assert key.status_code == 201 and key.json()['key'].startswith('sk-local-')
        assert _chat(client, key.json()['key']).status_code == 200
        dashboard = client.get('/portal/dashboard').json()
        assert dashboard['projects'][0]['name'] == 'production'
        assert dashboard['balances'] == [
            {'model': 'deepseek-chat', 'remaining_tokens': 5_000_000},
            {'model': 'fake:27b', 'remaining_tokens': 900},
        ]
        assert dashboard['usage'][0]['project'] == 'production'
        assert dashboard['usage'][0]['total_tokens'] == 100
        assert 'answer' not in dashboard['usage'][0]
        assert client.get('/portal/usage').json() == dashboard['usage']
        assert client.get('/portal/keys').json()[0]['project'] == 'production'
        assert admin['role'] == 'system_admin'
        operator, _ = _boot(monkeypatch, tmp_path, fake_ollama, config=config)
        with operator:
            assert operator.post('/auth/login', json={'identity': 'admin', 'password': 'a different secure password'}).status_code == 200
            users = operator.get('/portal/admin/users').json()
            alice = next(u for u in users if u['username'] == 'alice')
            assert operator.patch(f"/portal/admin/users/{alice['id']}", json={'disabled': True}).json()['disabled'] == 1
            assert client.get('/portal/dashboard').status_code == 401
            assert operator.patch(f"/portal/admin/users/{admin['id']}", json={'disabled': True}).status_code == 400


def test_email_tokens_are_single_use_and_password_reset_revokes_sessions(fake_ollama, tmp_path, monkeypatch):
    client, mod = _boot(monkeypatch, tmp_path, fake_ollama)
    with client:
        user = mod.platform.create_user(email='verify@example.com', username='verify', password='initial safe password')
        verify = mod.platform.issue_email_token(user['id'], 'verify_email', lifetime_seconds=60)
        with pytest.raises(ValueError, match='please wait'):
            mod.platform.issue_email_token(user['id'], 'verify_email', lifetime_seconds=60)
        assert mod.platform.verify_email(verify)['email_verified'] == 1
        assert mod.platform.verify_email(verify) is None
        session = mod.platform.create_session(user['id'])
        reset = mod.platform.issue_email_token(user['id'], 'reset_password', lifetime_seconds=60)
        assert mod.platform.reset_password(reset, 'replacement safe password') is True
        assert mod.platform.session_user(session) is None
        assert mod.platform.authenticate('verify', 'initial safe password') is None
        assert mod.platform.authenticate('verify', 'replacement safe password')['id'] == user['id']


def test_login_rate_limit_is_persistent(fake_ollama, tmp_path, monkeypatch):
    client, mod = _boot(monkeypatch, tmp_path, fake_ollama)
    with client:
        mod.platform.create_user(email='limit@example.com', username='limit', password='correct safe password', verified=True)
        for _ in range(5):
            assert client.post('/auth/login', json={'identity': 'limit', 'password': 'wrong password'}).status_code == 401
        assert client.post('/auth/login', json={'identity': 'limit', 'password': 'correct safe password'}).status_code == 429


def test_verified_stripe_webhook_credits_purchase_once(fake_ollama, tmp_path, monkeypatch):
    secret = 'whsec_test_only'
    client, mod = _boot(monkeypatch, tmp_path, fake_ollama, stripe_webhook_secret=secret)
    with client:
        user = mod.platform.create_user(email='paid@example.com', username='paid', password='a safe password now', verified=True)
        product = mod.platform.create_product(code='fake-pack', model='fake:27b', token_amount=5_000_000,
                                              price_cents=1500)
        mod.platform.create_order(user['id'], product, provider='stripe', checkout_id='cs_paid')
        payload = json.dumps({'id': 'evt_paid', 'object': 'event', 'type': 'checkout.session.completed',
                              'data': {'object': {'id': 'cs_paid', 'payment_status': 'paid'}}}, separators=(',', ':')).encode()
        timestamp = str(int(time.time()))
        signed = f'{timestamp}.{payload.decode()}'.encode()
        signature = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
        headers = {'stripe-signature': f't={timestamp},v1={signature}', 'content-type': 'application/json'}
        assert client.post('/webhooks/stripe', content=payload, headers=headers).status_code == 200
        assert client.post('/webhooks/stripe', content=payload, headers=headers).status_code == 200
        assert mod.platform.balances(user['id']) == [{'model': 'fake:27b', 'remaining_tokens': 5_000_000}]
        assert client.post('/webhooks/stripe', content=payload, headers={'content-type': 'application/json'}).status_code == 400


def test_prepaid_reservation_prevents_overdraft_and_settles_actual_usage(fake_ollama, tmp_path, monkeypatch):
    client, mod = _boot(monkeypatch, tmp_path, fake_ollama)
    with client:
        user = mod.platform.create_user(email='reserve@example.com', username='reserve', password='a safe password now', verified=True)
        product = mod.platform.create_product(code='reserve-pack', model='fake:27b', token_amount=100, price_cents=100)
        mod.platform.create_order(user['id'], product, provider='stripe', checkout_id='cs_reserve')
        mod.platform.record_paid_order(provider='stripe', event_id='evt_reserve', payload=b'{}', checkout_id='cs_reserve')
        assert mod.platform.reserve_model_tokens(user['id'], 'fake:27b', 101) is None
        reservation = mod.platform.reserve_model_tokens(user['id'], 'fake:27b', 100)
        assert reservation is not None
        assert mod.platform.balances(user['id']) == [{'model': 'fake:27b', 'remaining_tokens': 0}]
        assert mod.platform.reserve_model_tokens(user['id'], 'fake:27b', 1) is None
        assert mod.platform.settle_reservation(reservation, 70) is True
        assert mod.platform.balances(user['id']) == [{'model': 'fake:27b', 'remaining_tokens': 30}]
        capped = mod.platform.reserve_model_tokens(user['id'], 'fake:27b', 30)
        assert mod.platform.settle_reservation(capped, 31) is False
        assert mod.platform.balances(user['id']) == [{'model': 'fake:27b', 'remaining_tokens': 0}]


def test_stripe_checkout_creates_server_priced_pending_order(fake_ollama, tmp_path, monkeypatch):
    client, mod = _boot(monkeypatch, tmp_path, fake_ollama, stripe_secret_key='sk_test_not_real')
    with client:
        user = mod.platform.create_user(email='checkout@example.com', username='checkout', password='a safe password now', verified=True)
        product = mod.platform.create_product(code='checkout-pack', model='fake:27b', token_amount=5_000_000, price_cents=1500)
        assert client.post('/auth/login', json={'identity': 'checkout', 'password': 'a safe password now'}).status_code == 200
        seen = {}
        def fake_create(**kwargs):
            seen.update(kwargs)
            return SimpleNamespace(id='cs_new', url='https://checkout.stripe.test/cs_new')
        monkeypatch.setattr(mod.stripe.checkout.Session, 'create', fake_create)
        response = client.post('/portal/checkout', json={'product_id': product['id']})
        assert response.status_code == 201
        assert response.json()['checkout_url'] == 'https://checkout.stripe.test/cs_new'
        assert seen['line_items'][0]['price_data']['unit_amount'] == 1500
        order = mod.platform.dashboard(user['id'])['orders'][0]
        assert order['provider_checkout_id'] == 'cs_new' and order['status'] == 'pending'
