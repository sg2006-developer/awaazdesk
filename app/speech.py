"""Gnani speech: Prisma v3.0 (STT) and Timbre (TTS), REST and realtime WebSocket.

Behaviour verified against the live API (Nov 2026) beyond what the docs state:
- STT `x-partials: true` streams `partial_transcript` frames (`stable_text`/`volatile_text`)
  and then sends `speech_end` + `final_transcript` instead of `processing` + `transcript`.
- TTS sends a second, data-less `complete` ("Streaming completed") after each request, so a
  reused connection must skip it or the next request appears to finish instantly.
- Opening a TTS WebSocket costs ~1.5 s, so one connection is kept warm per call.
"""
import asyncio
import base64
import json
import os
import re

import websockets
from fastapi import HTTPException

from . import providers
from .schemas import LANGUAGE_NAMES

STT_REST_URL = "https://api.vachana.ai/stt/v3"
STT_WS_URL = "wss://api.vachana.ai/stt/v3/stream"
TTS_REST_URL = "https://api.vachana.ai/api/v1/tts/inference"
TTS_WS_URL = "wss://api.vachana.ai/api/v1/tts"
STT_MODEL = "prisma-v3.0"
SAMPLE_RATE = 16000          # Browser downsamples to 16 kHz: 3x less upload than 48 kHz.
FRAME_BYTES = 1024           # Prisma streaming expects exactly 512 samples per frame.

# Timbre v3.0: every voice speaks all 10 languages.
V3_VOICES = {
    "Shivani": "Female · patient, inbound support", "Kalpana": "Female · composed customer care",
    "Chetana": "Female · gentle, healthcare & telecom", "Tarika": "Female · reassuring, banking care",
    "Riyaa": "Female · confident, sales", "Lakshmi": "Female · warm storyteller",
    "Vasudha": "Female · composed narrator", "Yashraj": "Male · warm, IVR",
    "Devansh": "Male · energetic, young", "Shubham": "Male · confident, sales",
    "Avinash": "Male · firm, professional", "Harshad": "Male · firm, collections",
    "Prakash": "Male · steady, BFSI",
}
# Timbre v2.5: one preferred language per voice, so we pick a native voice per reply.
V25_VOICE_FOR_LANGUAGE = {
    "en-IN": "Kaveri", "hi-IN": "Nalini", "ta-IN": "Asmita", "te-IN": "Suhana", "kn-IN": "Saanvi",
    "ml-IN": "Reshma", "mr-IN": "Zahira", "bn-IN": "Kirra", "gu-IN": "Falak", "pa-IN": "Mehuli",
}

stt_lock = asyncio.Semaphore(int(os.getenv("GNANI_STT_CONCURRENCY", "2")))
tts_lock = asyncio.Semaphore(int(os.getenv("GNANI_TTS_CONCURRENCY", "2")))


class SpeechError(Exception):
    """A speech-provider failure that is safe to show to the user."""


def api_key():
    key = os.getenv("GNANI_API_KEY")
    if not key:
        raise HTTPException(503, "Add GNANI_API_KEY to .env and restart the server to enable voice.")
    return key


# ---------------------------------------------------------------- voice + hints

def pick_voice(settings, language):
    """Return (model, voice) for a reply in `language`."""
    settings = settings or {}
    model = settings.get("model", "timbre-v3.0")
    if model == "timbre-v2.5":
        return model, V25_VOICE_FOR_LANGUAGE.get(language, "Kaveri")
    voice = settings.get("voice", "Shivani")
    return "timbre-v3.0", voice if voice in V3_VOICES else "Shivani"


def tts_language(text, language):
    """Gnani recommends `auto` for code-mixed text (e.g. Hinglish in two scripts)."""
    has_latin = re.search(r"[A-Za-z]{3,}", text)
    has_indic = re.search(r"[ऀ-෿]", text)
    if language not in LANGUAGE_NAMES or (has_latin and has_indic):
        return "auto"
    return language


def recognition_hints(config, previous_text=""):
    """Prisma v3.0 context: words to favour and what the call is about."""
    terms = [config.get("name", "")]
    terms += config.get("keyterms", [])
    for category in config.get("categories", []):
        terms.append(category["name"])
        terms += [k for k in category.get("keywords", "").split(",")]
    seen, keyterms = set(), []
    for term in (t.strip() for t in terms):
        if term and len(term) <= 50 and "<|" not in term and term.casefold() not in seen:
            seen.add(term.casefold())
            keyterms.append(term)
    topic = f"Customer support phone call for {config.get('name', 'a business')}. {config.get('description', '')}"
    return {"keyterms": keyterms[:200], "topic": topic[:1600], "previous_text": previous_text[-600:]}


def _header_safe(text):
    """WebSocket headers must be Latin-1; keep the ASCII part of free text."""
    return re.sub(r"\s+", " ", text.encode("ascii", "ignore").decode()).strip()


# ---------------------------------------------------------------- REST

async def transcribe(wav, hints=None):
    key = api_key()
    data = {"model": STT_MODEL, "language_code": "indic-auto", "format": "transcribe"}
    if hints:
        if hints.get("keyterms"):
            data["keyterms"] = json.dumps(hints["keyterms"], ensure_ascii=False)
        for field in ("topic", "previous_text"):
            if hints.get(field):
                data[field] = hints[field]
    async with stt_lock:
        response = await providers.request(
            STT_REST_URL, service="Gnani speech recognition", headers={"X-API-Key-ID": key},
            files={"audio_file": ("voice.wav", wav, "audio/wav")}, data=data)
    try:
        result = response.json()
    except ValueError as exc:
        raise HTTPException(502, "Gnani speech recognition returned an unreadable response.") from exc
    if result.get("success") is False:
        error = result.get("error") or {}
        raise HTTPException(502, f"Gnani speech recognition failed: {error.get('message') or error.get('type') or 'unknown error'}.")
    if not result.get("transcript", "").strip():
        raise HTTPException(422, "No speech was detected. Try again closer to the microphone.")
    return {"text": result["transcript"], "language": result.get("detected_language_code"), "model": STT_MODEL}


def tts_request(text, language, settings, container="wav"):
    model, voice = pick_voice(settings, language)
    return {
        "text": text, "model": model, "voice": voice, "language": tts_language(text, language),
        "speed": (settings or {}).get("speed", 1.0),
        "audio_config": {"container": container, "encoding": "linear_pcm", "sample_rate": SAMPLE_RATE if container == "raw" else 24000,
                         "num_channels": 1, "sample_width": 2},
    }


async def synthesize(text, language="auto", settings=None):
    key = api_key()
    async with tts_lock:
        response = await providers.request(TTS_REST_URL, service="Gnani speech synthesis",
                                           headers={"X-API-Key-ID": key}, json=tts_request(text, language, settings))
    if not response.content.startswith(b"RIFF"):
        raise HTTPException(502, "Gnani speech synthesis returned an unexpected audio format.")
    return response.content


# ---------------------------------------------------------------- realtime STT

class RealtimeSTT:
    """One Prisma v3.0 streaming session with server-side VAD and live partials."""

    def __init__(self, hints, connect=websockets.connect):
        self.headers = {
            "x-api-key-id": api_key(), "model": STT_MODEL, "language_code": "indic-auto",
            "format": "transcribe", "x-sample-rate": str(SAMPLE_RATE), "x-partials": "true",
            "x-idle-timeout-sec": "900", "x-min-silence-ms": "600",
            "keyterms": json.dumps(hints.get("keyterms", []), ensure_ascii=True),
        }
        topic = _header_safe(hints.get("topic", ""))
        if topic:
            self.headers["topic"] = topic
        self._connect = connect
        self._buffer = bytearray()
        self.ws = None

    async def open(self):
        try:
            self.ws = await self._connect(STT_WS_URL, additional_headers=self.headers, open_timeout=10, max_size=2**20)
            first = json.loads(await asyncio.wait_for(self.ws.recv(), 10))
        except (OSError, asyncio.TimeoutError, websockets.WebSocketException) as exc:
            raise SpeechError(f"Could not start Gnani live transcription ({type(exc).__name__}).") from exc
        if first.get("type") != "connected":
            raise SpeechError(f"Gnani live transcription refused the session: {first.get('message', 'unknown reason')}.")

    @property
    def is_open(self):
        return self.ws is not None and self.ws.state == websockets.protocol.State.OPEN

    async def send(self, pcm):
        """Re-chunk browser audio into the exact 1,024-byte frames Prisma expects."""
        if not self.is_open:
            return
        self._buffer.extend(pcm)
        while len(self._buffer) >= FRAME_BYTES:
            frame = bytes(self._buffer[:FRAME_BYTES])
            del self._buffer[:FRAME_BYTES]
            await self.ws.send(frame)

    async def commit(self):
        if self.is_open:
            await self.ws.send(json.dumps({"type": "commit"}))

    async def events(self):
        """Yield normalised events: speech_start, partial, speech_end, final, error."""
        try:
            async for raw in self.ws:
                if isinstance(raw, bytes):
                    continue
                message = json.loads(raw)
                kind = message.get("type")
                if kind == "speech_start":
                    yield {"type": "speech_start"}
                elif kind == "partial_transcript":
                    yield {"type": "partial", "text": message.get("text", ""), "stable": message.get("stable_text", "")}
                elif kind in ("speech_end", "processing"):
                    yield {"type": "speech_end"}
                elif kind in ("final_transcript", "transcript"):
                    yield {"type": "final", "text": message.get("text", "").strip(),
                           "language": message.get("detected_language_code"),
                           "latency_ms": message.get("latency"), "audio_ms": message.get("audio_duration_ms")}
                elif kind == "error":
                    yield {"type": "error", "message": message.get("message", "Transcription error")}
        except websockets.ConnectionClosed:
            return

    async def close(self):
        if self.ws is not None:
            await self.ws.close()


# ---------------------------------------------------------------- realtime TTS

class RealtimeTTS:
    """A warm Timbre WebSocket that streams raw 16 kHz PCM for each reply."""

    def __init__(self, connect=websockets.connect):
        self._connect = connect
        self.ws = None
        self._opening = None

    async def _open(self):
        try:
            self.ws = await self._connect(TTS_WS_URL, additional_headers={"X-API-Key-ID": api_key()}, open_timeout=10, max_size=2**22)
        except (OSError, asyncio.TimeoutError, websockets.WebSocketException) as exc:
            self.ws = None
            raise SpeechError(f"Could not connect to Gnani speech synthesis ({type(exc).__name__}).") from exc

    def warm(self):
        """Start connecting in the background so the first reply doesn't pay for the handshake."""
        if self._opening is None or self._opening.done():
            self._opening = asyncio.create_task(self._open())
        return self._opening

    async def _ready(self):
        if self.ws is not None and self.ws.state == websockets.protocol.State.OPEN:
            return
        await self.warm()

    async def stream(self, text, language, settings):
        """Yield PCM chunks. Cancelling (barge-in) drops the connection and re-warms it."""
        await self._ready()
        started = False
        try:
            await self.ws.send(json.dumps(tts_request(text, language, settings, container="raw")))
            async for raw in self.ws:
                message = json.loads(raw)
                kind = message.get("type")
                if kind == "start":
                    started = True
                elif kind == "audio" and started:
                    chunk = base64.b64decode(message["data"]["audio"])
                    if chunk:
                        yield chunk
                elif kind == "complete" and started:
                    return  # The trailing data-less "complete" arrives before the next "start" and is skipped above.
                elif kind == "error":
                    raise SpeechError(f"Gnani speech synthesis failed: {message.get('message', 'unknown error')}.")
            raise SpeechError("Gnani speech synthesis closed the connection mid-reply.")
        except websockets.ConnectionClosed as exc:
            self.ws = None
            raise SpeechError("Gnani speech synthesis connection dropped.") from exc
        except (asyncio.CancelledError, GeneratorExit):
            # Unread audio from the interrupted reply would corrupt the next one.
            await self._discard()
            raise

    async def _discard(self):
        ws, self.ws = self.ws, None
        if ws is not None:
            asyncio.create_task(ws.close())
        self.warm()

    async def close(self):
        if self._opening and not self._opening.done():
            self._opening.cancel()
        if self.ws is not None:
            await self.ws.close()
