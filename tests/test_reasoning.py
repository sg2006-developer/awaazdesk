import asyncio
import json

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import config, engine, providers
from app.main import create_app
from app.store import Store


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    for name in ("OPENROUTER", "OPENROUTER_API_KEY", "OPENROUTER_MODEL", "LLM_PROVIDER",
                 "LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def business():
    return json.loads((config.ROOT / "sample_business.json").read_text())


def test_router_key_never_uses_stale_endpoint(monkeypatch):
    monkeypatch.setenv("OPENROUTER", "test-router-key")
    monkeypatch.setenv("LLM_API_KEY", "old-provider-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://old-provider.invalid/v1")
    monkeypatch.setenv("LLM_MODEL", "old-model")
    assert config.llm_ready()
    assert config.llm_settings() == {
        "provider": "openrouter", "base_url": "https://openrouter.ai/api/v1",
        "key": "test-router-key", "model": "google/gemini-2.5-flash"}


def test_missing_router_key_does_not_fall_back_to_old_key(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("LLM_API_KEY", "old-key")
    assert not config.llm_ready()


def test_standard_key_and_model_override(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "new-key")
    monkeypatch.setenv("OPENROUTER", "old-alias-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "custom/model")
    assert config.llm_settings()["key"] == "new-key"
    assert config.llm_settings()["model"] == "custom/model"


def test_provider_request(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    async def fake_request(url, **kwargs):
        assert url == "https://openrouter.ai/api/v1/chat/completions"
        assert kwargs["headers"]["Authorization"] == "Bearer test-key"
        body = kwargs["json"]
        assert body["max_tokens"] == 4096
        assert body["provider"]["require_parameters"] is True
        assert body["response_format"] == {"type": "json_object"}
        assert body["reasoning"] == {"enabled": False}
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": '{"ok": true}'}}]})
    monkeypatch.setattr(providers, "request", fake_request)
    assert asyncio.run(providers.reason("Return JSON", {})) == {"ok": True}


@pytest.mark.parametrize("content,finish", [("not json", "stop"), ("[]", "stop"), ('{"ok":true}', "length")])
def test_invalid_or_truncated_response_rejected(monkeypatch, content, finish):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    async def fake_request(*args, **kwargs):
        return httpx.Response(200, json={"choices": [{"finish_reason": finish, "message": {"content": content}}]})
    monkeypatch.setattr(providers, "request", fake_request)
    with pytest.raises(HTTPException) as error:
        asyncio.run(providers.reason("Return JSON", {}))
    assert error.value.status_code == 502


def test_small_knowledge_available_across_languages(business):
    sources = engine.retrieve(business, "ఫిల్టర్ ఎప్పుడు మార్చాలి?", include_small_corpus=True)
    assert any(s["title"] == "Filter care FAQ" for s in sources)
    assert len(sources) == 4


def test_large_corpus_stays_bounded(business):
    business["documents"] = [{"title": "Filter", "text": "filter policy " * 1000}]
    assert len(engine.retrieve(business, "filter", include_small_corpus=True)) <= 4


@pytest.mark.parametrize("status,source_ids,expected", [("resolved", [3], 200), ("open", [999], 502)])
def test_api_validation_and_persistence(tmp_path, monkeypatch, business, status, source_ids, expected):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    async def fake_reason(*args, **kwargs):
        return {"reply": "Your request is flagged for staff review.", "category": "Technician delay",
                "status": status, "summary": "Late technician", "fields": {"booking_id": "CD1042"},
                "source_ids": source_ids}
    monkeypatch.setattr(engine, "reason", fake_reason)
    url = f"sqlite:///{tmp_path / 'test.db'}"
    store = Store(url)
    with TestClient(create_app(store)) as client:
        assert client.post('/api/calls').status_code == 409
        assert client.post('/api/business/publish', json={"revision": 1}).status_code == 200
        call = client.post('/api/calls').json()
        result = client.post(f'/api/calls/{call["id"]}/turn', json={"text": "Technician late, booking CD1042", "revision": 0})
        assert result.status_code == expected
        if expected == 502:
            assert store.call(call['id'])['revision'] == 0
            assert len(store.call(call['id'])['messages']) == 1
        else:
            assert result.json()['ticket']['status'] == 'needs_human'
            assert result.json()['ticket']['model'] == 'google/gemini-2.5-flash'
            assert client.post(f'/api/calls/{call["id"]}/turn', json={"text": "Again", "revision": 0}).status_code == 409
        health = client.get('/api/health')
        assert 'test-key' not in health.text
        assert health.json()['provider'] == 'openrouter'
    with TestClient(create_app(Store(url))) as restarted:
        saved = restarted.get(f'/api/calls/{call["id"]}').json()
        assert saved['revision'] == (1 if expected == 200 else 0)
    store.engine.dispose()
