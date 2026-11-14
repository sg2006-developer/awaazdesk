"""Hosted-demo protection: access code, rate limits, spend caps, call length."""
import functools

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.guard import Guard
from app.live import LiveCall
from app.main import create_app
from app.store import Store
from tests.test_live import FakeSTT, FakeTTS, fake_llm, receive_until, start_call  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for name in ("OPENROUTER", "LLM_PROVIDER", "LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY", "DEMO_ACCESS_CODE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("GNANI_API_KEY", "test-gnani-key")


def client_for(tmp_path, tts=None, **guard_settings):
    guard = Guard()
    for name, value in guard_settings.items():
        setattr(guard, name, value)
    store = Store(f"sqlite:///{tmp_path / 'guard.db'}")
    live = functools.partial(LiveCall, stt_factory=FakeSTT, tts_factory=lambda: tts or FakeTTS())
    return TestClient(create_app(store, live_call=live, guard=guard)), store


def test_access_code_gates_the_api_and_live_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_ACCESS_CODE", "gnani-2026")
    client, store = client_for(tmp_path)
    with client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/access").json() == {"required": True, "granted": False}
        blocked = client.get("/api/business")
        assert blocked.status_code == 401 and blocked.json()["access_required"] is True
        assert client.post("/api/tts", json={"text": "hi"}).status_code == 401
        with pytest.raises(WebSocketDisconnect) as closed:
            with client.websocket_connect("/ws/calls/anything") as ws:
                ws.receive_json()
        assert closed.value.code == 1008

        assert client.post("/api/access", json={"code": "wrong"}).status_code == 401
        granted = client.post("/api/access", json={"code": " gnani-2026 "})
        assert granted.status_code == 200 and "httponly" in granted.headers["set-cookie"].lower()
        assert client.get("/api/access").json()["granted"] is True
        assert client.get("/api/business").status_code == 200
    store.engine.dispose()


def test_forged_or_expired_sessions_are_rejected(monkeypatch):
    monkeypatch.setenv("DEMO_ACCESS_CODE", "code")
    guard = Guard()
    token = guard.issue()
    assert guard.valid(token)
    assert not guard.valid("9999999999." + "0" * 64)
    assert not guard.valid("1." + guard._sign("1"))          # Expired.
    assert not Guard().valid(token)                          # Different secret (no SESSION_SECRET set).
    assert not guard.valid(None) and not guard.valid("garbage")


def test_open_when_no_code_is_configured(tmp_path):
    client, store = client_for(tmp_path)
    with client:
        assert client.get("/api/access").json() == {"required": False, "granted": True}
        assert client.get("/api/business").status_code == 200
    store.engine.dispose()


def test_calls_per_visitor_are_rate_limited(tmp_path):
    client, store = client_for(tmp_path, calls_per_hour=2)
    with client:
        start_call(client)
        assert client.post("/api/calls").status_code == 200
        limited = client.post("/api/calls")
        assert limited.status_code == 429 and "Retry-After" in limited.headers
    store.engine.dispose()


def test_daily_reasoning_cap(tmp_path, fake_llm):
    client, store = client_for(tmp_path, daily_llm=1)
    with client:
        call = start_call(client)
        assert client.post(f"/api/calls/{call['id']}/turn", json={"text": "Technician late", "revision": 0}).status_code == 200
        capped = client.post(f"/api/calls/{call['id']}/turn", json={"text": "Again", "revision": 1})
        assert capped.status_code == 429 and "today" in capped.json()["detail"]
        assert len(store.call(call["id"])["messages"]) == 3      # The refused turn wasn't saved.
    store.engine.dispose()


def test_speech_requests_are_rate_limited(tmp_path, monkeypatch):
    async def synthesize(*args):
        return b"RIFF"
    monkeypatch.setattr("app.main.synthesize", synthesize)
    client, store = client_for(tmp_path, speech_per_minute=2)
    with client:
        assert [client.post("/api/tts", json={"text": "hi"}).status_code for _ in range(3)] == [200, 200, 429]
    store.engine.dispose()


def test_live_lines_are_capped(tmp_path, fake_llm):
    client, store = client_for(tmp_path, max_live_calls=0)
    with client:
        call = start_call(client)
        with client.websocket_connect(f"/ws/calls/{call['id']}") as ws:
            assert "lines are busy" in ws.receive_json()["message"]
    store.engine.dispose()


def test_live_calls_end_at_the_time_limit(tmp_path, fake_llm):
    client, store = client_for(tmp_path, max_call_seconds=1)
    with client:
        call = start_call(client)
        with client.websocket_connect(f"/ws/calls/{call['id']}") as ws:
            events = receive_until(ws, "ended", limit=100)
            assert any("limited to" in e.get("message", "") for e in events)
    assert store.call(call["id"])["ended"] is True
    store.engine.dispose()
