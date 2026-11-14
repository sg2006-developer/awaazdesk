# AwaazDesk

**A multilingual voice support agent that any small business can configure in minutes, built on Gnani's Indian-language speech stack.**

A business owner describes their services, rules and FAQs. A caller then just *talks* in English, Hindi,
Telugu, Tamil or any of ten Indian languages (or mixes them). AwaazDesk listens with **Gnani Prisma v3.0**,
reasons over the business's approved knowledge with an LLM, answers aloud in the caller's language with
**Gnani Timbre**, and fills in a support ticket as the conversation happens.

| Live call | Agent studio |
| --- | --- |
| Live captions, interruptions, language detection, latency breakdown, live ticket | Business knowledge, escalation rules, voice choice, speech recognition hints, versioned publishing |

---

## What it does

- **Hands-free voice calls.** Streaming STT with server-side voice activity detection and *live partial
  captions*; streaming TTS that starts playing before synthesis finishes; **barge-in** (talk over the agent
  and it stops).
- **Replies in the caller's language and script.** Prisma detects the language per utterance; the LLM
  answers in it (natural Hinglish for code-mixed callers); Timbre speaks it with a voice that can actually
  pronounce it.
- **Speech-aware replies.** The LLM writes a separate `spoken_reply` with IDs spelled out
  ("CD1042" → "C D one zero four two") because Timbre mispronounces raw codes. The screen shows the
  readable version.
- **Hears your brand names.** The business name, category keywords and custom terms are sent to Prisma
  v3.0 as `keyterms` with a `topic`, so "ClearDrop", "AMC" or "RO" are recognised.
- **Configurable voice.** Timbre v3.0 (13 voices, each speaks all 10 languages) or Timbre v2.5 (a native
  voice picked per reply language), with speed control and a preview in every language.
- **Safe by construction.** Answers cite approved knowledge; escalation categories and existing handoffs
  can't be downgraded by the model; replies may not promise refunds, callbacks or timelines in *any*
  language; every call is pinned to the policy version it started on.
- **Measured.** Every voice turn reports STT, reasoning and time-to-first-audio, and the end-to-end
  "caller stopped speaking → agent's voice" time.

## Measured performance

Real Gnani + Gemini 2.5 Flash, caller audio streamed at real-time speed through the app's WebSocket
(`scripts/check_voice.py --live`, Nov 2026):

| Caller says (language) | Prisma heard | Reply language | Ticket | STT | LLM | TTS first audio | **Total** |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: |
| Technician late (English) | …booking ID is CD-1042 | English | Technician delay → human | 157 ms | 2.15 s | 0.85 s | **3.16 s** |
| When to change filter (Hindi) | मेरे water purifier का filter… | Hindi | Filter replacement | 118 ms | 1.79 s | 1.22 s | **3.13 s** |
| Technician late (Telugu) | టెక్నీషియన్ ఇంకా రాలేదు… CD-2042 | Telugu | Technician delay → human | 156 ms | 2.04 s | 1.55 s | **3.75 s** |
| Double charge, refund (Hinglish) | मुझे double charge हुआ है… CD-3042 | Hindi | Payment issue → human | 159 ms | 2.01 s | 0.20 s | **2.38 s** |

Median **3.15 s**. Prisma finalises a transcript ~150 ms after the caller stops; the LLM is the largest
share (see *Next steps*). Timbre's first-chunk time varies from 0.2 to 1.5 s request to request
on the same warm connection.

## Architecture

```
 Browser                                   AwaazDesk (FastAPI)                         Gnani
 ───────                                   ───────────────────                         ─────
 mic → AudioWorklet ──16 kHz PCM16──▶  /ws/calls/{id}  ──1,024-byte frames──▶  Prisma v3.0 stream
   (anti-aliased resample, 40 ms)        LiveCall                ◀── speech_start / partial /
                                          │                           final + language
 captions, meters ◀── JSON events ──────  │ turn queue
                                          ▼
                                       engine.take_turn ──▶ LLM (JSON: reply, spoken_reply,
                                          │                  language, category, status, fields,
                                          │                  source_ids) ── validated + policy
                                          ▼                                  guardrails ──▶ SQLite
 speaker ◀── PCM chunks (gapless) ────  speak() ◀──── base64 audio ──── Timbre (warm WebSocket)
   barge_in ⇒ stop instantly             cancel on caller speech ⇒ drop socket, re-warm
```

| Module | Responsibility |
| --- | --- |
| `app/speech.py` | Everything Gnani: REST + realtime STT/TTS, voice selection, recognition hints, protocol quirks |
| `app/live.py` | One live call: relays audio, runs turns sequentially, streams speech, barge-in, metrics |
| `app/engine.py` | Retrieval, LLM decision + validation + guardrails, `take_turn` shared by text and voice |
| `app/store.py` | Versioned config, calls and tickets with optimistic concurrency (`ConflictError`) |
| `app/providers.py` | Pooled HTTP client, LLM call, service-specific error messages |
| `static/mic-worklet.js` | Real-time mic capture, 63-tap windowed-sinc low-pass + resample to 16 kHz |
| `static/live.mjs`, `audio.mjs` | WebSocket session, gapless PCM player, meters |

### Things I learned about the Gnani APIs (verified against the live service)

These aren't in the docs (or differ from them) and shaped the design. Tests in `tests/test_speech.py` pin them down.

1. **Live partials exist.** The STT `connected` message advertises `partials_enabled`; sending the header
   `x-partials: true` streams `partial_transcript` frames with `stable_text`/`volatile_text`, then
   `speech_end` + `final_transcript` (instead of the documented `processing` + `transcript`).
2. **TTS sends a second, data-less `complete`** ("Streaming completed") after each request. On a reused
   connection the next request appears to finish instantly unless you wait for `start` first.
3. **Opening a TTS WebSocket costs ~1.5 s.** So each call keeps one warm connection and pre-warms it
   during the greeting; after a barge-in the interrupted socket is dropped and a new one warmed in the
   background (the next reply still started in 0.69 s).
4. **Downsampling must be filtered.** Naïve 48→16 kHz interpolation turned clear Telugu into Devanagari
   gibberish; with an anti-aliasing filter (or a 16 kHz `AudioContext`) the same audio transcribes perfectly.
5. The original code always used voice *Nalini* (a Hindi-preferred Timbre v2.5 voice) for every language.
   v3.0 voices speak all ten; v2.5 voices must match the language.

## Run it

Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
cp .env.example .env          # add GNANI_API_KEY and OPENROUTER_API_KEY
uv sync --locked
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open http://localhost:8000 → **Agent studio → Save & publish** → **Live call → Start new call**, allow
the microphone, and talk. Headphones give the cleanest barge-in; on speakers, untick *Let me interrupt*
to switch to half-duplex. Untick *Hands-free voice* for a typed call with optional read-aloud.

### Configuration

```dotenv
GNANI_API_KEY=...                       # Prisma + Timbre
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=...
OPENROUTER_MODEL=google/gemini-2.5-flash
# Optional: another OpenAI-compatible provider
# LLM_PROVIDER=compatible  LLM_BASE_URL=...  LLM_API_KEY=...  LLM_MODEL=...
# Optional: Gnani concurrency per service (default 2)
# GNANI_STT_CONCURRENCY=2  GNANI_TTS_CONCURRENCY=2
DATABASE_URL=sqlite:///./data/awaazdesk.db
```

Without an LLM key the app runs in a clearly labelled *limited local* mode (keyword lookup, no
reasoning). A configured provider failing is reported, never silently replaced with a fake answer.

## Tests

```sh
uv run pytest -q                                  # 47 offline tests, no credits used
uv run python scripts/check_reasoning.py --live   # LLM decisions in English/Hindi/Telugu
uv run python scripts/check_voice.py --live       # full voice loop in 4 languages + latency table
```

The offline suite drives complete live calls over the WebSocket with fake Gnani/LLM services (greeting,
partial → final → reply → spoken audio, barge-in on/off, typed text, cross-origin rejection, missing key),
plus the Gnani protocol handling, recognition hints, voice selection and HTTP edge cases.

## Design decisions

- **Server-side proxy for streaming.** Browsers can't set the `x-api-key-id` header on a WebSocket, and the
  key must never reach the client anyway, so the browser talks to `/ws/calls/{id}` and the server holds
  both Gnani sockets.
- **One code path for a turn.** Typed and spoken turns both go through `engine.take_turn`, so the same
  validation, guardrails and optimistic-concurrency checks apply.
- **Barge-in on words, not noise.** The agent is interrupted on the first non-empty partial transcript
  rather than raw `speech_start`, so a cough doesn't cut it off.
- **Turns queue rather than overlap.** If the caller says two things while the agent is thinking, they're
  answered together.
- **Metrics are honest.** `response_ms` is measured from Prisma's end-of-speech event to the first audio
  byte the server forwards; it excludes browser playback buffering (~30 ms).

## Limits and next steps

- **LLM latency is the bottleneck** (~2 s of ~3 s). Next: stream the LLM response and start TTS on the
  first sentence of `spoken_reply` while the ticket JSON is still generating.
- **Retrieval for large knowledge bases is lexical.** Small libraries (≤8,000 chars) go to the model
  whole, so a Telugu question can use English documents; larger ones need multilingual embeddings.
- **Telephony.** This is a browser simulator. Gnani ships LiveKit and Pipecat plugins and Timbre outputs
  8 kHz µ-law directly, which is the path to a real phone number.
- **Post-call analytics** with Gnani Batch STT (speaker separation, up to 4 h files).
- **Single business, local only.** No authentication, multi-tenancy or rate limits. Bind to localhost and
  use synthetic data only; `data/awaazdesk.db` holds transcripts.

Costs: OpenRouter billing is separate from Gnani credits. Gemini 2.5 Flash was $0.30/M input and
$2.50/M output tokens at setup; check current pricing before heavy use.
