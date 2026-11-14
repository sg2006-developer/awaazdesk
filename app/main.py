import io
import os
import uuid
import wave
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .config import ROOT, llm_ready, llm_settings
from .engine import draft_from_notes, take_turn
from .live import LiveCall
from .providers import close_client
from .schemas import LANGUAGE_NAMES, DraftRequest, PublishRequest, SpeechRequest, TextRequest, TurnRequest
from .speech import V3_VOICES, V25_VOICE_FOR_LANGUAGE, recognition_hints, synthesize, transcribe
from .store import ConflictError, Store

MAX_RECORDING_SECONDS = 30  # Gnani Prisma REST accepts up to 30 s per request.


def same_origin(origin, host, scheme):
    """Local single-operator workspace: refuse cross-site calls that would spend credits."""
    return not origin or origin == f"{scheme}://{host}"


def create_app(store=None, live_call=LiveCall):
    store = store or Store()

    @asynccontextmanager
    async def lifespan(app):
        store.initialize()
        yield
        await close_client()

    app = FastAPI(title="AwaazDesk", version="0.2.0", lifespan=lifespan)

    @app.middleware("http")
    async def headers_and_origin(request: Request, call_next):
        if request.method not in ("GET", "HEAD") and not same_origin(request.headers.get("origin"), request.url.netloc, request.url.scheme):
            return JSONResponse({"detail": "Cross-origin requests are not allowed"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(ConflictError)
    async def conflict(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.get("/api/health")
    def health():
        settings = llm_settings()
        return {"status": "ok", "speech_configured": bool(os.getenv("GNANI_API_KEY")),
                "reasoning": "hosted-llm" if llm_ready() else "limited-local",
                "model": settings["model"] if llm_ready() else None,
                "provider": settings["provider"] if llm_ready() else None,
                "max_recording_seconds": MAX_RECORDING_SECONDS}

    @app.get("/api/voices")
    def voices():
        return {"timbre-v3.0": V3_VOICES, "timbre-v2.5": V25_VOICE_FOR_LANGUAGE, "languages": LANGUAGE_NAMES}

    @app.get("/api/business")
    def business():
        return {**store.draft(), "versions": store.versions()}

    @app.put("/api/business")
    def save(data: DraftRequest):
        return store.save_draft(data.config.model_dump(), data.revision)

    @app.post("/api/business/propose")
    async def propose(data: TextRequest):
        return await draft_from_notes(data.text, store.draft()["config"])

    @app.post("/api/business/publish")
    def publish(data: PublishRequest):
        return {"version": store.publish(data.revision)}

    @app.get("/api/versions/{version}")
    def version(version: int):
        result = store.version(version)
        if not result:
            raise HTTPException(404, "Version not found")
        return result

    @app.post("/api/calls")
    def start_call():
        version = store.version()
        if not version:
            raise HTTPException(409, "Publish a business configuration before starting a call.")
        return store.create_call(str(uuid.uuid4()), version["id"], version["config"]["greeting"])

    @app.get("/api/calls")
    def calls():
        return store.calls()

    @app.get("/api/calls/{call_id}")
    def get_call(call_id: str):
        call = store.call(call_id)
        if not call:
            raise HTTPException(404, "Conversation not found")
        return call

    @app.post("/api/calls/{call_id}/turn")
    async def turn(call_id: str, data: TurnRequest):
        return await take_turn(store, call_id, data.text, data.revision)

    @app.post("/api/calls/{call_id}/end")
    def end(call_id: str):
        get_call(call_id)
        return store.end(call_id)

    @app.websocket("/ws/calls/{call_id}")
    async def live(ws: WebSocket, call_id: str):
        scheme = "https" if ws.url.scheme == "wss" else "http"
        if not same_origin(ws.headers.get("origin"), ws.url.netloc, scheme):
            await ws.close(code=1008, reason="Cross-origin connections are not allowed")
            return
        call = store.call(call_id)
        await ws.accept()
        if not call or call["ended"]:
            await ws.send_json({"type": "error", "message": "This call has ended or does not exist. Start a new call."})
            await ws.close(code=1000)
            return
        await live_call(ws, store, call_id).run()

    @app.post("/api/stt")
    async def stt(request: Request, call_id: str | None = None):
        audio = bytearray()
        async for chunk in request.stream():
            audio.extend(chunk)
            if len(audio) > 6_000_000:
                raise HTTPException(413, f"Recording is too large. Keep notes under {MAX_RECORDING_SECONDS} seconds.")
        try:
            with wave.open(io.BytesIO(audio)) as wav:
                duration = wav.getnframes() / wav.getframerate()
                if not 0.1 <= duration <= MAX_RECORDING_SECONDS or wav.getnchannels() != 1 or wav.getsampwidth() != 2:
                    raise ValueError()
        except (wave.Error, EOFError, ValueError, ZeroDivisionError):
            raise HTTPException(422, f"Invalid WAV recording. Expected mono 16-bit audio between 0.1 and {MAX_RECORDING_SECONDS} seconds.")
        hints = None
        call = store.call(call_id) if call_id else None
        if call:
            last_agent = next((m["content"] for m in reversed(call["messages"]) if m["role"] == "assistant"), "")
            hints = recognition_hints(store.version(call["version"])["config"], last_agent)
        return await transcribe(bytes(audio), hints)

    @app.post("/api/tts")
    async def tts(data: SpeechRequest):
        settings = data.voice.model_dump() if data.voice else store.draft()["config"].get("voice")
        return Response(await synthesize(data.text, data.language, settings), media_type="audio/wav")

    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    @app.get("/")
    def index():
        return FileResponse(ROOT / "static" / "index.html")

    return app


app = create_app()
