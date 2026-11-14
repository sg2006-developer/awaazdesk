# AwaazDesk

**A multilingual voice support agent that any small business can configure in minutes, built on Gnani's Indian-language speech stack.**

📄 **[One-page solution pitch](PITCH.md)**

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
(`scripts/check_voice.py --live`, Nov 2026, Timbre v2.5 voices). One of two runs:

| Caller says (language) | Prisma heard | Reply language | Ticket | STT | LLM | Timbre audio | **Total** |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: |
| Technician late (English) | …booking ID is CD-1042 | English | Technician delay → human | 151 ms | 2.13 s | 0.23 s | **2.52 s** |
| When to change filter (Hindi) | मेरे water purifier का filter… | Hindi | Filter replacement | 101 ms | 1.63 s | 0.29 s | **2.02 s** |
| Technician late (Telugu) | టెక్నీషియన్ ఇంకా రాలేదు… CD-2042 | Telugu | Technician delay → human | 174 ms | 1.67 s | 0.17 s | **2.02 s** |
| Double charge, refund (Hinglish) | मुझे double charge हुआ है… CD-3042 | Hindi | Payment issue → human | 100 ms | 1.73 s | 0.30 s | **2.13 s** |

Over both runs (8 turns): language detected correctly 8/8, every ticket correct, **median 2.06 s** from the
end of the caller's speech to the agent's voice (range 1.89–2.52 s). "Timbre audio" is the time until 250 ms
of reply audio is ready to play, which is when the caller actually starts hearing it. The LLM is ~80% of the wait.

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
| `static/mic-worklet.js` | Real-time mic capture; anti-aliasing low-pass (63-tap windowed sinc, >9 kHz cut ~60 dB) + resample to 16 kHz |
| `static/live.mjs`, `audio.mjs` | WebSocket session, gapless PCM player, meters |

### Things I learned about the Gnani APIs (verified against the live service)

These aren't in the docs (or differ from them) and shaped the design. Tests pin the behaviour down.

1. **Indic-script `keyterms` change the output script for every caller.** With the sample business's Devanagari
   keywords (फिल्टर, नहीं आया) in `keyterms`, Prisma wrote Telugu speech in Hindi script 3/3 times
   ("टेक्नीशियन इंका रालेदु"); without them, 3/3 correct. AwaazDesk now sends only Latin-script terms.
2. **Timbre v3.0 streams a ~60 ms chunk, then pauses ~1 s.** Played naively that's a blip and a gap, and a
   "first audio" metric based on the first chunk is ~1 s too optimistic. Timbre v2.5 had 250 ms of audio ready
   in a median 0.72 s versus 1.85 s for v3.0, so v2.5 (native voice per language) is the default, and every reply
   is buffered to 250 ms before playing.
3. **Live partials exist.** The STT `connected` message advertises `partials_enabled`; the header
   `x-partials: true` streams `partial_transcript` frames (`stable_text`/`volatile_text`), then `speech_end` +
   `final_transcript` instead of the documented `processing` + `transcript`.
4. **Streaming accepts one language code only.** The docs say v3.0 takes 1–4 comma-separated codes; over the
   WebSocket that returns "exactly one locale is required", so streaming uses `indic-auto`.
5. **TTS sends a second, data-less `complete`** ("Streaming completed") after each request; a reused
   connection must wait for `start` or the next reply appears to finish instantly.
6. **Opening a TTS WebSocket costs ~1.5 s,** so each call keeps one warm. There's no cancel message: after a
   barge-in the socket is dropped and re-warmed, and with Timbre v3.0 the next reply sometimes started up to 4 s
   late, consistent with the interrupted synthesis still running server-side.

App-side behaviours that came out of live testing: the LLM drifted into the previous turn's language (an
English request after Hindi turns got a Hindi reply), and despite the prompt it sometimes said a request was
"sent to the team" (हमने इसे टीम को भेज दिया है) or that staff "will assist you". Replies are now checked for the
caller's script and for forbidden claims in English, Hindi, Telugu and Tamil, with one corrective retry. And a short "సరే." followed by a pause was answered on its own, so utterances of one or two
words wait 1.2 s for the caller to continue.

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

## Host it (Render, free tier)

The repo includes a `Dockerfile` and a `render.yaml` Blueprint.

1. Sign in at [render.com](https://render.com) with GitHub, then **New → Blueprint** and pick this repository.
2. Fill in the three secrets it asks for: `GNANI_API_KEY`, `OPENROUTER_API_KEY` and a `DEMO_ACCESS_CODE` of your choice.
3. Deploy. Open `https://<service>.onrender.com`, enter the access code, publish the sample business and call.

A public URL could spend your credits, so the hosted demo is protected by:

| Protection | Default (env var) |
| --- | --- |
| Access code, then a signed `HttpOnly` session cookie (HTTP and WebSocket) | `DEMO_ACCESS_CODE` |
| New calls per visitor | 10 per hour (`CALLS_PER_HOUR_PER_VISITOR`) |
| Speech requests per visitor | 20 per minute (`SPEECH_REQUESTS_PER_MINUTE`) |
| Calls / LLM turns per day, all visitors | 150 / 800 (`DAILY_CALL_LIMIT`, `DAILY_LLM_LIMIT`) |
| Simultaneous live calls, max call length | 4, 5 minutes (`MAX_LIVE_CALLS`, `MAX_CALL_SECONDS`) |

Render's free tier sleeps after 15 idle minutes (first visit then takes ~50 s), and its disk is ephemeral, so
the demo resets to the sample business on each restart. Everything a visitor creates is visible to other
visitors with the code, so keep the data synthetic.

## Tests

```sh
uv run pytest -q                                  # 70 offline tests, no credits used
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

- **LLM latency is the bottleneck** (~1.7 s of ~2 s). Next: stream the LLM response and start TTS on the
  first sentence of `spoken_reply` while the ticket JSON is still generating.
- **Retrieval for large knowledge bases is lexical.** Small libraries (≤8,000 chars) go to the model
  whole, so a Telugu question can use English documents; larger ones need multilingual embeddings.
- **Turn-taking is silence-based.** A pause over 0.6 s mid-sentence ends the caller's turn; the agent may
  answer the first half (e.g. ask for a booking ID the caller was about to give). Semantic endpointing would help.
- **Telephony.** This is a browser simulator. Gnani ships LiveKit and Pipecat plugins and Timbre outputs
  8 kHz µ-law directly, which is the path to a real phone number.
- **Post-call analytics** with Gnani Batch STT (speaker separation, up to 4 h files).
- **Single business demo.** The hosted version is protected by an access code and spend limits, not by
  user accounts or multi-tenancy. Use synthetic data only; `data/awaazdesk.db` holds transcripts.

Costs: OpenRouter billing is separate from Gnani credits. Gemini 2.5 Flash was $0.30/M input and
$2.50/M output tokens at setup; check current pricing before heavy use.
