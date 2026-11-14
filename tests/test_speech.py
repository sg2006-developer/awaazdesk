"""Gnani speech helpers and protocol handling, with fake sockets (no credits used)."""
import asyncio
import base64
import json

import httpx
import pytest
import websockets
from fastapi import HTTPException

from app import providers, speech
from app.schemas import Decision


@pytest.fixture(autouse=True)
def gnani_key(monkeypatch):
    monkeypatch.setenv("GNANI_API_KEY", "test-gnani-key")


# ------------------------------------------------------------- voice + hints

def test_v3_voice_speaks_every_language():
    assert speech.pick_voice({"model": "timbre-v3.0", "voice": "Kalpana"}, "te-IN") == ("timbre-v3.0", "Kalpana")
    # Unknown or legacy voice names fall back to a valid v3.0 voice instead of a 400 from Gnani.
    assert speech.pick_voice({"model": "timbre-v3.0", "voice": "Nalini"}, "hi-IN") == ("timbre-v3.0", "Shivani")
    assert speech.pick_voice(None, "en-IN") == ("timbre-v3.0", "Shivani")


@pytest.mark.parametrize("language,voice", [("te-IN", "Suhana"), ("hi-IN", "Nalini"), ("ta-IN", "Asmita"), ("xx", "Kaveri")])
def test_v25_picks_native_voice_per_language(language, voice):
    assert speech.pick_voice({"model": "timbre-v2.5"}, language) == ("timbre-v2.5", voice)


@pytest.mark.parametrize("text,language,expected", [
    ("నమస్కారం, మీ బుకింగ్ నమోదు చేశాము.", "te-IN", "te-IN"),
    ("आपकी booking flag हो गई है।", "hi-IN", "auto"),   # Code-mixed scripts: let Timbre detect.
    ("Hello there", "auto", "auto"),
])
def test_tts_language(text, language, expected):
    assert speech.tts_language(text, language) == expected


def test_recognition_hints_from_business(business):
    hints = speech.recognition_hints(business, "x" * 900)
    assert hints["keyterms"][0] == "ClearDrop Care"
    assert "AMC" in hints["keyterms"] and "Technician delay" in hints["keyterms"]
    assert len({k.casefold() for k in hints["keyterms"]}) == len(hints["keyterms"])
    assert len(hints["keyterms"]) <= 200 and len(hints["topic"]) <= 1600
    assert len(hints["previous_text"]) == 600


def test_streaming_headers_are_latin1_safe(business):
    business["description"] = "पानी purifier service — fast"
    stt = speech.RealtimeSTT(speech.recognition_hints(business))
    for value in stt.headers.values():
        value.encode("latin-1")
    assert json.loads(stt.headers["keyterms"])[0] == "ClearDrop Care"
    assert stt.headers["x-partials"] == "true" and stt.headers["x-sample-rate"] == "16000"


@pytest.mark.parametrize("raw,expected", [("te-IN", "te-IN"), ("hi", "hi-IN"), ("Tamil", "ta-IN"), ("klingon", "en-IN"), (None, "en-IN")])
def test_decision_language_is_normalised(raw, expected):
    decision = Decision(reply="ok", language=raw, category="x", status="open", summary="")
    assert decision.language == expected


# ------------------------------------------------------------- REST

def test_rest_transcribe_sends_context_and_reports_gnani_errors(monkeypatch):
    seen = {}

    async def fake_request(url, service, **kwargs):
        seen.update(kwargs["data"], service=service)
        return httpx.Response(200, json={"success": False, "error": {"type": "INVALID_CONTEXT", "message": "topic too long"}})
    monkeypatch.setattr(providers, "request", fake_request)
    with pytest.raises(HTTPException) as error:
        asyncio.run(speech.transcribe(b"RIFF", {"keyterms": ["ClearDrop", "फ़िल्टर"], "topic": "Support", "previous_text": "Hi"}))
    assert error.value.status_code == 502 and "topic too long" in error.value.detail
    assert json.loads(seen["keyterms"]) == ["ClearDrop", "फ़िल्टर"]
    assert seen["topic"] == "Support" and seen["model"] == "prisma-v3.0"
    assert seen["service"] == "Gnani speech recognition"


def test_provider_errors_name_the_service(monkeypatch):
    async def fail(*args, **kwargs):
        raise httpx.ConnectTimeout("slow")
    monkeypatch.setattr(providers.client(), "post", fail)
    with pytest.raises(HTTPException) as error:
        asyncio.run(providers.request("https://x.invalid", service="Gnani speech synthesis"))
    assert error.value.status_code == 504 and error.value.detail.startswith("Gnani speech synthesis timed out")


# ------------------------------------------------------------- realtime sockets

class FakeSocket:
    def __init__(self, incoming):
        self.incoming = [m if isinstance(m, (str, bytes)) else json.dumps(m) for m in incoming]
        self.sent = []
        self.state = websockets.protocol.State.OPEN

    async def send(self, data):
        self.sent.append(data)

    async def recv(self):
        return self.incoming.pop(0)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.incoming:
            raise StopAsyncIteration
        return self.incoming.pop(0)

    async def close(self):
        self.state = websockets.protocol.State.CLOSED


def audio(data, index):
    return {"type": "audio", "data": {"chunk_index": index, "audio": base64.b64encode(data).decode(), "is_final": False}}


def test_tts_reuses_connection_and_skips_trailing_complete():
    socket = FakeSocket([
        {"type": "start", "request_id": "1"}, audio(b"ab", 1), audio(b"cd", 2),
        {"type": "complete", "data": {"audio": "", "is_final": True}},
        {"type": "complete", "message": "Streaming completed"},     # Undocumented trailing frame.
        {"type": "start", "request_id": "2"}, audio(b"ef", 1),
        {"type": "complete", "data": {"audio": "", "is_final": True}},
    ])

    async def connect(*args, **kwargs):
        return socket

    async def scenario():
        tts = speech.RealtimeTTS(connect=connect)
        first = [c async for c in tts.stream("Hello", "en-IN", {"model": "timbre-v3.0", "voice": "Riyaa"})]
        second = [c async for c in tts.stream("Again", "en-IN", None)]
        return first, second

    first, second = asyncio.run(scenario())
    assert first == [b"ab", b"cd"] and second == [b"ef"]
    request = json.loads(socket.sent[0])
    assert request["voice"] == "Riyaa" and request["audio_config"]["container"] == "raw"
    assert request["audio_config"]["sample_rate"] == 16000


def test_tts_error_is_reported():
    async def connect(*args, **kwargs):
        return FakeSocket([{"type": "error", "message": "Invalid voice"}])

    async def scenario():
        return [c async for c in speech.RealtimeTTS(connect=connect).stream("Hi", "en-IN", None)]
    with pytest.raises(speech.SpeechError, match="Invalid voice"):
        asyncio.run(scenario())


def test_stt_rechunks_audio_and_normalises_events(business):
    socket = FakeSocket([
        {"type": "connected", "config": {}},
        {"type": "speech_start", "utterance_id": "u1"},
        {"type": "partial_transcript", "text": "मेरा technician", "stable_text": "मेरा"},
        {"type": "speech_end", "utterance_id": "u1"},
        {"type": "final_transcript", "text": " मेरा technician नहीं आया ", "detected_language_code": "hi-IN", "latency": 170, "audio_duration_ms": 2000},
        {"type": "transcript", "text": "legacy shape", "latency": 10},   # Without x-partials.
    ])

    async def connect(*args, **kwargs):
        return socket

    async def scenario():
        stt = speech.RealtimeSTT(speech.recognition_hints(business), connect=connect)
        await stt.open()
        await stt.send(b"\x01" * 1500)
        await stt.send(b"\x02" * 600)
        return [e async for e in stt.events()]

    events = asyncio.run(scenario())
    assert [len(f) for f in socket.sent] == [1024, 1024]   # 2,100 bytes in -> two exact frames, 52 buffered.
    assert [e["type"] for e in events] == ["speech_start", "partial", "speech_end", "final", "final"]
    assert events[3] == {"type": "final", "text": "मेरा technician नहीं आया", "language": "hi-IN", "latency_ms": 170, "audio_ms": 2000}


def test_stt_refused_session_is_a_speech_error(business):
    async def connect(*args, **kwargs):
        return FakeSocket([{"type": "error", "message": "Concurrent session limit (2) reached"}])

    with pytest.raises(speech.SpeechError, match="Concurrent session limit"):
        asyncio.run(speech.RealtimeSTT(speech.recognition_hints(business), connect=connect).open())
