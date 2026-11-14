"""A hands-free voice call over one browser WebSocket.

    browser mic --16 kHz PCM--> server --1,024-byte frames--> Gnani Prisma (VAD + partials)
    browser speaker <--PCM chunks-- server <--streamed audio-- Gnani Timbre <-- LLM reply

Client -> server: binary PCM16 frames, or JSON {"type": "text" | "settings" | "stop_speaking" | "end"}.
Server -> client: binary PCM16 (agent voice) and JSON events: ready, speech_start, partial, final,
thinking, call, audio_start, audio_end, barge_in, metrics, error, ended.
"""
import asyncio
import json
import logging
import time

from fastapi import HTTPException, WebSocket, WebSocketDisconnect

from .engine import take_turn
from .speech import SAMPLE_RATE, RealtimeSTT, RealtimeTTS, SpeechError, recognition_hints

log = logging.getLogger("awaazdesk.live")


class LiveCall:
    def __init__(self, ws: WebSocket, store, call_id, stt_factory=RealtimeSTT, tts_factory=RealtimeTTS):
        self.ws, self.store, self.call_id = ws, store, call_id
        self.stt_factory, self.tts_factory = stt_factory, tts_factory
        self.stt = None
        self.tts = None
        self.turns: asyncio.Queue = asyncio.Queue()
        self.speaking: asyncio.Task | None = None
        self.barge_in = True
        self.send_lock = asyncio.Lock()
        self.speech_ended_at = None
        self.stt_latency = None
        self.voice_ready = False

    # ------------------------------------------------------------ plumbing
    async def send(self, **event):
        async with self.send_lock:
            await self.ws.send_json(event)

    async def send_audio(self, chunk):
        async with self.send_lock:
            await self.ws.send_bytes(chunk)

    def config(self):
        call = self.store.call(self.call_id)
        return call, self.store.version(call["version"])["config"]

    # ------------------------------------------------------------ lifecycle
    async def run(self):
        call, config = self.config()
        tasks = []
        try:
            self.tts = self.tts_factory()
            self.tts.warm()
            last_agent = next((m["content"] for m in reversed(call["messages"]) if m["role"] == "assistant"), "")
            try:
                self.stt = self.stt_factory(recognition_hints(config, last_agent))
                await self.stt.open()
                self.voice_ready = True
            except (SpeechError, HTTPException) as exc:
                await self.send(type="error", message=f"{getattr(exc, 'detail', exc)} You can still type messages.")
            await self.send(type="ready", voice=self.voice_ready, sample_rate=SAMPLE_RATE, call=call)
            tasks = [asyncio.create_task(self.listen()), asyncio.create_task(self.take_turns())]
            if len(call["messages"]) == 1:  # Fresh call: speak the greeting.
                greeting = call["messages"][0]
                self.speaking = asyncio.create_task(self.speak(greeting["content"], greeting.get("language", "en-IN"), config))
            await self.read_client()
        finally:
            for task in tasks + [self.speaking]:
                if task and not task.done():
                    task.cancel()
            for service in (self.stt, self.tts):
                if service is not None:
                    try:
                        await service.close()
                    except Exception:  # Closing a half-open socket must not mask the real error.
                        log.debug("speech close failed", exc_info=True)

    async def read_client(self):
        try:
            while True:
                message = await self.ws.receive()
                if message["type"] == "websocket.disconnect":
                    return
                if message.get("bytes") is not None:
                    if self.stt is not None and self.voice_ready:
                        await self.stt.send(message["bytes"])
                    continue
                data = json.loads(message.get("text") or "{}")
                kind = data.get("type")
                if kind == "text" and str(data.get("text", "")).strip():
                    await self.interrupt()
                    self.speech_ended_at, self.stt_latency = time.perf_counter(), None
                    await self.turns.put((str(data["text"]).strip()[:12000], None, None))
                elif kind == "settings":
                    self.barge_in = bool(data.get("barge_in", True))
                elif kind == "stop_speaking":
                    await self.interrupt()
                elif kind == "end":
                    call = self.store.end(self.call_id)
                    await self.send(type="ended", call=call)
                    return
        except WebSocketDisconnect:
            return

    # ------------------------------------------------------------ speech in
    async def listen(self):
        """Relay Prisma events; reconnect if the streaming session closes (15-minute cap)."""
        if self.stt is None:
            return
        while True:
            if self.voice_ready:
                async for event in self.stt.events():
                    await self.on_stt_event(event)
                self.voice_ready = False
            await asyncio.sleep(1)
            try:
                await self.stt.open()
                self.voice_ready = True
            except SpeechError as exc:
                log.warning("STT reconnect failed: %s", exc)
                await asyncio.sleep(4)

    async def on_stt_event(self, event):
        kind = event["type"]
        if kind == "speech_start":
            await self.send(type="speech_start")
        elif kind == "partial":
            # Barge-in on real words, not on a cough: wait for a non-empty partial.
            if self.barge_in and self.is_speaking() and len(event["text"].strip()) >= 2:
                await self.interrupt()
            await self.send(type="partial", text=event["text"])
        elif kind == "speech_end":
            self.speech_ended_at = time.perf_counter()
        elif kind == "final":
            if not event["text"]:
                return
            if self.is_speaking() and self.barge_in:
                await self.interrupt()
            self.stt_latency = event.get("latency_ms")
            await self.send(type="final", text=event["text"], language=event.get("language"))
            meta = {"stt_ms": event.get("latency_ms"), "audio_ms": event.get("audio_ms")}
            await self.turns.put((event["text"], event.get("language"), meta))
        elif kind == "error":
            await self.send(type="error", message=f"Transcription: {event['message']}")

    # ------------------------------------------------------------ turns
    async def take_turns(self):
        while True:
            text, language, meta = await self.turns.get()
            # If the caller said several things while we were busy, answer them together.
            while not self.turns.empty():
                more, more_language, _ = self.turns.get_nowait()
                text, language = f"{text} {more}", more_language or language
            await self.send(type="thinking")
            try:
                call = self.store.call(self.call_id)
                call = await take_turn(self.store, self.call_id, text, call["revision"], language, meta)
            except HTTPException as exc:
                await self.send(type="error", message=str(exc.detail))
                continue
            await self.send(type="call", call=call)
            reply = call["messages"][-1]
            _, config = self.config()
            self.speaking = asyncio.create_task(self.speak(reply.get("spoken") or reply["content"], reply.get("language", "en-IN"),
                                                           config, llm_ms=reply.get("meta", {}).get("llm_ms")))
            try:
                await self.speaking
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise

    # ------------------------------------------------------------ speech out
    def is_speaking(self):
        return self.speaking is not None and not self.speaking.done()

    async def interrupt(self):
        if self.is_speaking():
            self.speaking.cancel()
            await self.send(type="barge_in")

    async def speak(self, text, language, config, llm_ms=None):
        started = time.perf_counter()
        first_audio = None
        await self.send(type="audio_start", sample_rate=SAMPLE_RATE, language=language)
        try:
            async for chunk in self.tts.stream(text, language, config.get("voice")):
                if first_audio is None:
                    first_audio = time.perf_counter()
                    await self.send(type="metrics", **self.metrics(started, first_audio, llm_ms))
                await self.send_audio(chunk)
        except (SpeechError, HTTPException) as exc:
            await self.send(type="error", message=f"{getattr(exc, 'detail', exc)} The text reply is shown above.")
        finally:
            if not asyncio.current_task().cancelling():
                await self.send(type="audio_end")

    def metrics(self, tts_started, first_audio, llm_ms):
        result = {"tts_ms": round((first_audio - tts_started) * 1000), "llm_ms": llm_ms, "stt_ms": self.stt_latency}
        if self.speech_ended_at and llm_ms is not None:
            # Caller stopped speaking -> first byte of the agent's voice.
            result["response_ms"] = round((first_audio - self.speech_ended_at) * 1000)
        self.speech_ended_at = None
        return result
