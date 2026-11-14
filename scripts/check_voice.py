"""Opt-in live voice benchmark: real Gnani STT/TTS + your LLM, through the app's WebSocket.

Each scenario's caller audio is synthesised with Gnani Timbre (a different voice from the
agent), streamed into /ws/calls/<id> at real-time speed exactly like the browser does, and
the agent's reply is timed from the end of the caller's speech to its first audio byte.

    uv run uvicorn app.main:app --port 8000      # in another terminal
    uv run python scripts/check_voice.py --live [--url http://127.0.0.1:8000]

Uses Gnani and LLM credits (~6 short TTS/STT requests + 4 LLM calls).
"""
import argparse
import asyncio
import base64
import json
import statistics
import sys
import time
from pathlib import Path

import httpx
import websockets

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import ROOT  # noqa: E402  (also loads .env)
from app.speech import api_key  # noqa: E402

SCENARIOS = [
    ("English", "en-IN", "My technician has not arrived yet. My booking ID is C D one zero four two."),
    ("Hindi", "hi-IN", "मेरे वाटर प्यूरीफायर का फ़िल्टर कितने समय में बदलना चाहिए?"),
    ("Telugu", "te-IN", "టెక్నీషియన్ ఇంకా రాలేదు. నా బుకింగ్ ఐడీ సి డి రెండు సున్నా నాలుగు రెండు."),
    ("Hinglish", "hi-IN", "मुझे double charge हुआ है, refund चाहिए. Booking ID सी डी तीन शून्य चार दो है."),
]
FRAME = 1280  # 40 ms of 16 kHz PCM16, as the browser worklet sends.


async def caller_audio(text, language):
    pcm = b""
    async with websockets.connect("wss://api.vachana.ai/api/v1/tts", additional_headers={"X-API-Key-ID": api_key()}) as ws:
        await ws.send(json.dumps({"text": text, "voice": "Devansh", "model": "timbre-v3.0", "language": language,
                                  "audio_config": {"sample_rate": 16000, "num_channels": 1, "sample_width": 2, "encoding": "linear_pcm", "container": "raw"}}))
        async for raw in ws:
            message = json.loads(raw)
            if message["type"] == "audio":
                pcm += base64.b64decode(message["data"]["audio"])
            elif message["type"] == "complete" and "data" in message:
                return pcm
            elif message["type"] == "error":
                raise SystemExit(f"Could not synthesise caller audio: {message.get('message')}")


async def run_scenario(base, label, language, text):
    pcm = await caller_audio(text, language)
    async with httpx.AsyncClient(base_url=base) as http:
        call = (await http.post("/api/calls")).json()
    if "id" not in call:
        raise SystemExit(f"Could not start a call: {call.get('detail')}")
    ws_url = base.replace("http", "ws", 1) + f"/ws/calls/{call['id']}"
    result = {"label": label}
    async with websockets.connect(ws_url, origin=base) as ws:
        greeting_done = asyncio.Event()

        async def microphone():
            silence = b"\0" * FRAME
            while not greeting_done.is_set():
                await ws.send(silence)
                await asyncio.sleep(0.04)
            for i in range(0, len(pcm), FRAME):
                await ws.send(pcm[i:i + FRAME])
                await asyncio.sleep(0.04)
            while True:
                await ws.send(silence)
                await asyncio.sleep(0.04)

        mic = asyncio.create_task(microphone())
        try:
            async with asyncio.timeout(60):
                async for raw in ws:
                    if isinstance(raw, bytes):
                        continue
                    event = json.loads(raw)
                    if event["type"] == "audio_end" and not greeting_done.is_set():
                        greeting_done.set()
                    elif event["type"] == "final":   # A pause can split one line into several segments.
                        result["heard"] = f"{result.get('heard', '')} {event['text']}".strip()
                        result["heard_language"] = event.get("language")
                    elif event["type"] == "call":
                        reply = event["call"]["messages"][-1]
                        result.update(reply=reply["content"], reply_language=reply["language"],
                                      category=event["call"]["ticket"]["category"], status=event["call"]["ticket"]["status"])
                    elif event["type"] == "metrics" and event.get("llm_ms") is not None:
                        result.update(event)
                    elif event["type"] == "audio_end" and "reply" in result:
                        break
                    elif event["type"] == "error":
                        print(f"  [{label}] error: {event['message']}")
        finally:
            mic.cancel()
    return result


async def main(base):
    async with httpx.AsyncClient(base_url=base) as http:
        health = (await http.get("/api/health")).json()
        if not health["speech_configured"] or health["reasoning"] != "hosted-llm":
            raise SystemExit("The server needs GNANI_API_KEY and a reasoning provider configured.")
        if not (await http.get("/api/business")).json()["versions"]:
            raise SystemExit("Publish a business configuration first (Agent studio → Save & publish).")
        print(f"Model: {health['model']}\n")
    results = []
    for label, language, text in SCENARIOS:
        result = await run_scenario(base, label, language, text)
        results.append(result)
        print(f"{label:9} heard ({result.get('heard_language')}): {result.get('heard')}")
        print(f"{'':9} reply ({result.get('reply_language')}): {result.get('reply')}")
        print(f"{'':9} ticket: {result.get('category')} / {result.get('status')}\n")
    print(f"{'Scenario':10}{'STT':>8}{'LLM':>8}{'TTS 1st':>9}{'Total':>8}   (caller stops speaking → agent's first audio)")
    for r in results:
        print(f"{r['label']:10}{r.get('stt_ms', 0):>6}ms{r.get('llm_ms', 0):>6}ms{r.get('tts_ms', 0):>7}ms{r.get('response_ms', 0) / 1000:>7.2f}s")
    totals = [r["response_ms"] for r in results if "response_ms" in r]
    if totals:
        print(f"\nMedian response: {statistics.median(totals) / 1000:.2f}s over {len(totals)} turns")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true", help="Authorize live requests that use Gnani and LLM credits")
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    if not args.live:
        parser.error("Use --live to make requests that consume provider credits.")
    _ = ROOT
    asyncio.run(main(args.url.rstrip("/")))
