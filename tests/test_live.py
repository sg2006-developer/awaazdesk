"""Live WebSocket calls and HTTP endpoints, with fake Gnani + LLM (no credits used)."""
import asyncio
import functools
import io
import wave

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import engine, speech
from app.live import LiveCall
from app.main import create_app
from app.store import Store


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for name in ("OPENROUTER", "LLM_PROVIDER", "LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("GNANI_API_KEY", "test-gnani-key")


@pytest.fixture
def fake_llm(monkeypatch):
    calls = []

    async def reason(system, payload):
        calls.append(payload)
        return {"reply": "మీ అభ్యర్థన డాష్‌బోర్డ్‌లో ఫ్లాగ్ చేయబడింది. CD2042", "spoken_reply": "మీ అభ్యర్థన ఫ్లాగ్ చేయబడింది. సి డి రెండు సున్నా నాలుగు రెండు",
                "language": "te-IN", "category": "Technician delay", "status": "needs_human",
                "summary": "Technician late", "fields": {"booking_id": "CD2042"}, "source_ids": []}
    monkeypatch.setattr(engine, "reason", reason)
    return calls


class FakeSTT:
    """Turns marker bytes from the 'browser' into Prisma-style events."""
    instances = []

    def __init__(self, hints):
        self.hints = hints
        self.queue = asyncio.Queue()
        FakeSTT.instances.append(self)

    async def open(self):
        pass

    async def send(self, pcm):
        if pcm == b"SAY":
            for event in ({"type": "speech_start"}, {"type": "partial", "text": "టెక్నీషియన్"}, {"type": "speech_end"},
                          {"type": "final", "text": "టెక్నీషియన్ రాలేదు CD2042", "language": "te-IN", "latency_ms": 150, "audio_ms": 1800}):
                await self.queue.put(event)
        elif pcm == b"INTERRUPT":
            await self.queue.put({"type": "partial", "text": "wait wait"})
        elif pcm in (b"FRAGMENT", b"LONE"):
            await self.queue.put({"type": "speech_start"})
            await self.queue.put({"type": "final", "text": "సరే.", "language": "te-IN", "latency_ms": 90, "audio_ms": 700})
            if pcm == b"FRAGMENT":   # The caller pauses, then carries on.
                async def carry_on():
                    await asyncio.sleep(0.3)
                    await self.queue.put({"type": "speech_start"})
                    await self.queue.put({"type": "final", "text": "ఫిల్టర్ ఎప్పుడు మార్చాలి?", "language": "te-IN", "latency_ms": 120, "audio_ms": 2100})
                asyncio.get_running_loop().create_task(carry_on())

    async def events(self):
        while True:
            yield await self.queue.get()

    async def close(self):
        pass


class FakeTTS:
    def __init__(self, chunks=3, delay=0.0):
        self.chunks, self.delay, self.requests = chunks, delay, []

    def warm(self):
        pass

    async def stream(self, text, language, settings):
        self.requests.append((text, language, settings))
        for i in range(self.chunks):
            await asyncio.sleep(self.delay)
            yield bytes([i]) * 640

    async def close(self):
        pass


def make_client(tmp_path, tts):
    store = Store(f"sqlite:///{tmp_path / 'live.db'}")
    app = create_app(store, live_call=functools.partial(LiveCall, stt_factory=FakeSTT, tts_factory=lambda: tts))
    return TestClient(app), store


def receive_until(ws, kind, limit=50):
    events = []
    for _ in range(limit):
        message = ws.receive()
        event = {"type": "audio", "bytes": message["bytes"]} if message.get("bytes") is not None else __import__("json").loads(message["text"])
        events.append(event)
        if event["type"] == kind:
            return events
    raise AssertionError(f"no {kind} in {[e['type'] for e in events]}")


def start_call(client):
    revision = client.get("/api/business").json()["revision"]
    assert client.post("/api/business/publish", json={"revision": revision}).status_code == 200
    return client.post("/api/calls").json()


def test_live_call_round_trip(tmp_path, fake_llm):
    tts = FakeTTS()
    client, store = make_client(tmp_path, tts)
    with client:
        call = start_call(client)
        with client.websocket_connect(f"/ws/calls/{call['id']}") as ws:
            events = receive_until(ws, "audio_end")
            assert events[0]["type"] == "ready" and events[0]["voice"] is True
            assert sum(len(e["bytes"]) for e in events if e["type"] == "audio") == 3 * 640     # Greeting is spoken.
            assert tts.requests[0][0].startswith("Hello! I'm the AI support assistant")
            # Recognition hints come from the published business.
            assert "ClearDrop Care" in FakeSTT.instances[-1].hints["keyterms"]

            ws.send_bytes(b"SAY")
            events = receive_until(ws, "audio_end")
            kinds = [e["type"] for e in events]
            assert kinds.index("partial") < kinds.index("final") < kinds.index("call") < kinds.index("metrics")
            final = next(e for e in events if e["type"] == "final")
            assert final["language"] == "te-IN"
            metrics = next(e for e in events if e["type"] == "metrics")
            assert metrics["stt_ms"] == 150 and metrics["llm_ms"] is not None and "response_ms" in metrics

            # The spoken form (IDs spelled out) is what Timbre receives, in the reply language.
            text, language, settings = tts.requests[-1]
            assert "సి డి రెండు" in text and language == "te-IN" and settings["model"] == "timbre-v2.5"
            assert fake_llm[-1]["caller_language"] == "te-IN"

            ws.send_text('{"type": "end"}')
            assert receive_until(ws, "ended")[-1]["call"]["ended"] is True

    saved = store.call(call["id"])
    customer, agent = saved["messages"][1], saved["messages"][2]
    assert customer["language"] == "te-IN" and customer["meta"]["stt_ms"] == 150
    assert agent["language"] == "te-IN" and agent["spoken"].startswith("మీ అభ్యర్థన ఫ్లాగ్")
    assert saved["ticket"]["language"] == "te-IN" and saved["ticket"]["status"] == "needs_human"
    store.engine.dispose()


def test_caller_can_interrupt_the_agent(tmp_path, fake_llm):
    client, store = make_client(tmp_path, FakeTTS(chunks=200, delay=0.02))
    with client:
        call = start_call(client)
        with client.websocket_connect(f"/ws/calls/{call['id']}") as ws:
            receive_until(ws, "audio_start")
            ws.send_bytes(b"INTERRUPT")
            events = receive_until(ws, "barge_in")
            assert sum(len(e["bytes"]) for e in events if e["type"] == "audio") < 200 * 640
    store.engine.dispose()


def test_barge_in_can_be_disabled(tmp_path, fake_llm):
    client, store = make_client(tmp_path, FakeTTS(chunks=15, delay=0.01))
    with client:
        call = start_call(client)
        with client.websocket_connect(f"/ws/calls/{call['id']}") as ws:
            ws.send_text('{"type": "settings", "barge_in": false}')
            receive_until(ws, "audio_start")
            ws.send_bytes(b"INTERRUPT")
            kinds = [e["type"] for e in receive_until(ws, "audio_end")]
            assert "barge_in" not in kinds
    store.engine.dispose()


def test_typed_text_in_live_call(tmp_path, fake_llm):
    client, store = make_client(tmp_path, FakeTTS())
    with client:
        call = start_call(client)
        with client.websocket_connect(f"/ws/calls/{call['id']}") as ws:
            receive_until(ws, "audio_end")
            ws.send_text('{"type": "text", "text": "Technician late"}')
            events = receive_until(ws, "audio_end")
            assert any(e["type"] == "call" for e in events)
    assert store.call(call["id"])["messages"][1] == {"role": "user", "content": "Technician late"}
    store.engine.dispose()


def test_live_call_rejects_cross_origin_and_ended_calls(tmp_path, fake_llm):
    client, store = make_client(tmp_path, FakeTTS())
    with client:
        call = start_call(client)
        with pytest.raises(WebSocketDisconnect) as closed:
            with client.websocket_connect(f"/ws/calls/{call['id']}", headers={"origin": "https://evil.example"}) as ws:
                ws.receive_json()
        assert closed.value.code == 1008
        client.post(f"/api/calls/{call['id']}/end")
        with client.websocket_connect(f"/ws/calls/{call['id']}") as ws:
            assert "ended" in ws.receive_json()["message"]
    store.engine.dispose()


def test_live_call_without_speech_key_still_accepts_text(tmp_path, fake_llm, monkeypatch):
    monkeypatch.delenv("GNANI_API_KEY")
    store = Store(f"sqlite:///{tmp_path / 'nokey.db'}")
    client = TestClient(create_app(store, live_call=functools.partial(LiveCall, tts_factory=lambda: FakeTTS())))
    with client:
        call = start_call(client)
        with client.websocket_connect(f"/ws/calls/{call['id']}") as ws:
            error = ws.receive_json()
            assert error["type"] == "error" and "GNANI_API_KEY" in error["message"]
            assert ws.receive_json()["voice"] is False
    store.engine.dispose()


# ------------------------------------------------------------- HTTP

def wav_bytes(seconds, channels=1, rate=16000):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\0\0" * channels * int(rate * seconds))
    return buffer.getvalue()


@pytest.mark.parametrize("audio,status", [(b"not audio", 422), (wav_bytes(31), 422), (wav_bytes(1, channels=2), 422)])
def test_stt_rejects_bad_recordings(tmp_path, audio, status):
    client, store = make_client(tmp_path, FakeTTS())
    with client:
        assert client.post("/api/stt", content=audio).status_code == status
    store.engine.dispose()


def test_stt_uses_call_context(tmp_path, monkeypatch):
    seen = {}

    async def transcribe(wav, hints=None):
        seen["hints"] = hints
        return {"text": "ok", "language": "en-IN", "model": "prisma-v3.0"}
    monkeypatch.setattr("app.main.transcribe", transcribe)
    client, store = make_client(tmp_path, FakeTTS())
    with client:
        call = start_call(client)
        assert client.post(f"/api/stt?call_id={call['id']}", content=wav_bytes(1)).json()["text"] == "ok"
        assert seen["hints"]["previous_text"].startswith("Hello!")
        client.post("/api/stt", content=wav_bytes(1))
        assert seen["hints"] is None
    store.engine.dispose()


def test_tts_uses_draft_voice_or_override(tmp_path, monkeypatch):
    seen = []

    async def synthesize(text, language, settings):
        seen.append((language, settings))
        return b"RIFF...."
    monkeypatch.setattr("app.main.synthesize", synthesize)
    client, store = make_client(tmp_path, FakeTTS())
    with client:
        assert client.post("/api/tts", json={"text": "Hi", "language": "te-IN"}).content == b"RIFF...."
        client.post("/api/tts", json={"text": "Hi", "voice": {"model": "timbre-v2.5", "voice": "x", "speed": 0.9}})
        assert client.post("/api/tts", json={"text": "Hi", "voice": {"speed": 3}}).status_code == 422
    assert seen[0] == ("te-IN", {"model": "timbre-v2.5", "voice": "Shivani", "speed": 1.0})
    assert seen[1][1]["model"] == "timbre-v2.5"
    store.engine.dispose()


def test_stale_draft_is_a_conflict_not_a_crash(tmp_path):
    client, store = make_client(tmp_path, FakeTTS())
    with client:
        draft = client.get("/api/business").json()
        assert client.put("/api/business", json={"config": draft["config"], "revision": draft["revision"]}).status_code == 200
        stale = client.put("/api/business", json={"config": draft["config"], "revision": draft["revision"]})
        assert stale.status_code == 409 and "another tab" in stale.json()["detail"]
    store.engine.dispose()


def test_voices_endpoint(tmp_path):
    client, store = make_client(tmp_path, FakeTTS())
    with client:
        data = client.get("/api/voices").json()
    assert len(data["timbre-v3.0"]) == 13 and data["timbre-v2.5"]["te-IN"] == "Suhana"
    store.engine.dispose()


def test_limited_local_mode_detects_language(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY")
    client, store = make_client(tmp_path, FakeTTS())
    with client:
        call = start_call(client)
        result = client.post(f"/api/calls/{call['id']}/turn", json={"text": "ఫిల్టర్ technician late booking CD1042", "revision": 0}).json()
    assert result["ticket"]["engine"] == "limited-local" and result["ticket"]["language"] == "te-IN"
    store.engine.dispose()


def test_call_list_is_one_query(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'list.db'}")
    store.initialize()
    store.publish(1)
    for i in range(5):
        store.create_call(f"call-{i}", 1, "Hello")
    statements = []
    from sqlalchemy import event
    event.listen(store.engine, "before_cursor_execute", lambda *args: statements.append(args[2]))
    assert len(store.calls()) == 5
    assert len(statements) == 1
    store.engine.dispose()


def test_short_fragment_waits_for_the_rest_of_the_sentence(tmp_path, fake_llm):
    client, store = make_client(tmp_path, FakeTTS())
    with client:
        call = start_call(client)
        with client.websocket_connect(f"/ws/calls/{call['id']}") as ws:
            receive_until(ws, "audio_end")
            ws.send_bytes(b"FRAGMENT")
            events = receive_until(ws, "audio_end")
            assert sum(e["type"] == "call" for e in events) == 1
    messages = store.call(call["id"])["messages"]
    assert len(messages) == 3 and messages[1]["content"] == "సరే. ఫిల్టర్ ఎప్పుడు మార్చాలి?"
    store.engine.dispose()


def test_lone_fragment_is_still_answered(tmp_path, fake_llm):
    client, store = make_client(tmp_path, FakeTTS())
    with client:
        call = start_call(client)
        with client.websocket_connect(f"/ws/calls/{call['id']}") as ws:
            receive_until(ws, "audio_end")
            ws.send_bytes(b"LONE")
            assert receive_until(ws, "call")[-1]["call"]["messages"][1]["content"] == "సరే."
    store.engine.dispose()


@pytest.mark.parametrize("replies,expected,calls", [
    (["ज़रूर, आपकी रिक्वेस्ट नोट कर ली है।", "Sure, your request is flagged for staff review."], "Sure, your request is flagged for staff review.", 2),
    (["Sure, noted."], "Sure, noted.", 1),
    (["ज़रूर, नोट कर लिया।", "फिर से हिंदी।"], "ज़रूर, नोट कर लिया।", 2),   # Still wrong: keep the first, don't drop the turn.
])
def test_reply_language_is_corrected_once(monkeypatch, business, replies, expected, calls):
    seen = []

    async def reason(system, payload):
        seen.append(system)
        return {"reply": replies[len(seen) - 1], "language": "en-IN", "category": "Installation", "status": "open",
                "summary": "s", "fields": {}, "source_ids": []}
    monkeypatch.setattr(engine, "reason", reason)
    messages = [{"role": "assistant", "content": "नमस्ते"}, {"role": "user", "content": "Can I talk to a person?"}]
    decision, _ = asyncio.run(engine.respond(business, messages, {}, caller_language="en-IN"))
    assert decision.reply == expected and len(seen) == calls
    if calls == 2:
        assert "CORRECTION" in seen[1] and "English" in seen[1]


def test_reply_start_is_buffered_past_the_first_tiny_chunk(tmp_path, fake_llm):
    class StutteringTTS(FakeTTS):
        async def stream(self, text, language, settings):
            self.requests.append((text, language, settings))
            yield b"\x01" * 1958                 # Timbre v3.0: ~60 ms first...
            await asyncio.sleep(0.2)              # ...then a pause...
            for _ in range(4):
                yield b"\x02" * 4096              # ...then the rest.
    client, store = make_client(tmp_path, StutteringTTS())
    with client:
        call = start_call(client)
        with client.websocket_connect(f"/ws/calls/{call['id']}") as ws:
            events = receive_until(ws, "audio_end")
            audio = [e["bytes"] for e in events if e["type"] == "audio"]
            assert len(audio[0]) >= 8000            # First send already holds 250 ms.
            kinds = [e["type"] for e in events]
            assert kinds.index("metrics") < kinds.index("audio")
    store.engine.dispose()


def test_caller_can_interrupt_after_the_reply_was_fully_sent(tmp_path, fake_llm):
    # Fast TTS: 4 s of audio arrives at once, but the browser is still playing it.
    client, store = make_client(tmp_path, FakeTTS(chunks=100, delay=0))
    with client:
        call = start_call(client)
        with client.websocket_connect(f"/ws/calls/{call['id']}") as ws:
            receive_until(ws, "audio_end", limit=300)   # Server has sent everything...
            ws.send_bytes(b"INTERRUPT")            # ...while the caller hears it, they talk.
            assert receive_until(ws, "barge_in")[-1]["type"] == "barge_in"
    store.engine.dispose()


@pytest.mark.parametrize("reply,language,flagged", [
    ("हमने इसे समीक्षा के लिए हमारी टीम को भेज दिया है।", "hi-IN", True),
    ("I've flagged your request in our dashboard. They will be able to assist you further.", "en-IN", True),
    ("Our team will get back to you shortly.", "en-IN", True),
    ("మీ అభ్యర్థనను బృందానికి పంపాము.", "te-IN", True),
    ("Please send your booking ID so I can flag this for staff review.", "en-IN", False),
    ("आपकी बुकिंग ID नोट कर ली है और डैशबोर्ड में स्टाफ रिव्यू के लिए फ़्लैग कर दी है।", "hi-IN", False),
])
def test_forbidden_claims_are_detected(reply, language, flagged):
    decision = engine.Decision(reply=reply, language=language, category="x", status="open", summary="")
    assert bool(engine.reply_problems(decision, language)) is flagged


def test_forbidden_claim_triggers_one_corrective_retry(monkeypatch, business):
    replies = ["हमने इसे हमारी टीम को भेज दिया है।", "आपका अनुरोध डैशबोर्ड में स्टाफ रिव्यू के लिए फ़्लैग कर दिया गया है।"]
    seen = []

    async def reason(system, payload):
        seen.append(system)
        return {"reply": replies[len(seen) - 1], "language": "hi-IN", "category": "Technician delay", "status": "needs_human",
                "summary": "s", "fields": {}, "source_ids": []}
    monkeypatch.setattr(engine, "reason", reason)
    messages = [{"role": "user", "content": "मेरा टेक्नीशियन नहीं आया"}]
    decision, _ = asyncio.run(engine.respond(business, messages, {}, caller_language="hi-IN"))
    assert decision.reply == replies[1] and len(seen) == 2 and "भेज दिया" in seen[1]


def test_typed_turn_gets_language_hint_from_its_script(tmp_path, monkeypatch, business):
    hints = []

    async def reason(system, payload):
        hints.append(payload["caller_language"])
        return {"reply": "Noted and flagged for staff review.", "language": "en-IN", "category": "Technician delay",
                "status": "needs_human", "summary": "s", "fields": {}, "source_ids": []}
    monkeypatch.setattr(engine, "reason", reason)
    client, store = make_client(tmp_path, FakeTTS())
    with client:
        call = start_call(client)
        client.post(f"/api/calls/{call['id']}/turn", json={"text": "My technician has not arrived.", "revision": 0})
        client.post(f"/api/calls/{call['id']}/turn", json={"text": "టెక్నీషియన్ రాలేదు", "revision": 1})
    assert hints[0] == "en-IN" and hints[1] == "te-IN"
    store.engine.dispose()


def test_claim_is_stripped_when_retry_repeats_it(monkeypatch, business):
    reply = "Of course, I've flagged your request in the dashboard for a staff member to review. They will be able to assist you further."
    calls = []

    async def reason(system, payload):
        calls.append(1)
        return {"reply": reply, "spoken_reply": reply, "language": "en-IN", "category": "Installation", "status": "needs_human",
                "summary": "s", "fields": {}, "source_ids": []}
    monkeypatch.setattr(engine, "reason", reason)
    decision, _ = asyncio.run(engine.respond(business, [{"role": "user", "content": "Can I talk to a person?"}], {}, caller_language="en-IN"))
    assert len(calls) == 2
    assert decision.reply == decision.spoken_reply == "Of course, I've flagged your request in the dashboard for a staff member to review."
