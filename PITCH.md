# AwaazDesk — solution pitch

**A voice support agent any small Indian business can set up in minutes, that answers customers in their own language.**

## The problem

India's small service businesses (repair shops, clinics, installers, local retailers) get support calls in Hindi,
Telugu, Tamil, Kannada and code-mixed "Hinglish" every day. They can't staff multilingual phone lines, and
English-first IVRs and chatbots fail exactly the customers who need help most. Calls go unanswered, details
get lost, and urgent problems (a late technician, a double charge) don't reach the owner in time.

## The solution

The owner describes the business in plain words (services, rules, FAQs, what must go to a human) and publishes it.
Customers then **just talk** in any of ten Indian languages, or mix them:

- **Gnani Prisma v3.0** streams the caller's speech with live captions, detects the language per sentence, and is
  primed with the business's own vocabulary (brand and product names) through `keyterms` and `topic`.
- An LLM answers **only from approved knowledge**, in the caller's language and script, and fills a support ticket
  (category, booking ID, status, cited evidence) as the call happens.
- **Gnani Timbre** speaks the reply with a voice that can pronounce that language, starting before synthesis
  finishes. The caller can interrupt at any time.
- Anything sensitive (refunds, delays, explicit requests for a person) is flagged for staff. The model cannot
  downgrade it, and replies never promise refunds, callbacks or timelines, in any language.

## Proof it works (measured on the real APIs)

| Caller (language) | Understood | Replied in | Ticket | Caller stops → agent speaks |
| --- | --- | --- | --- | ---: |
| Technician late (English) | ✓ booking CD-1042 | English | Technician delay → human | 2.5 s |
| Filter question (Hindi) | ✓ | Hindi | Filter replacement | 2.0 s |
| Technician late (Telugu) | ✓ booking CD-2042 | Telugu | Technician delay → human | 2.0 s |
| Refund, double charge (Hinglish) | ✓ booking CD-3042 | Hindi | Payment issue → human | 2.1 s |

Across 8 benchmark turns: languages detected 8/8, tickets correct 8/8, **median 2.06 s** from the end of the caller's
speech to the agent's audible voice (Prisma finalises in ~150 ms; the LLM is ~80% of the wait).
70 automated tests cover the call loop, Gnani protocol handling, guardrails and the hosted-demo protections.

## What's novel

- **Language-true voice:** reply language follows the caller (with a corrective retry if the model drifts into an
  earlier turn's language), a native Timbre voice is picked per language, and a separate *spoken* version of each
  reply spells out IDs ("C D one zero four two") so they're pronounced correctly.
- **Built on what the Gnani APIs actually do,** found by measuring the live service: Indic-script `keyterms` made
  Prisma write Telugu callers in Hindi script (wrong 3/3 times before the fix, 0/3 after); Timbre v3.0 pauses ~1 s after its first
  chunk (v2.5 is ~1.1 s faster to audible speech); live partials via `x-partials`; a second TTS completion frame
  that breaks reused connections.
- **Owner-safe by design:** versioned policies (every call keeps the version it started on), cited answers,
  enforced escalation, and replies checked in code (not just by prompt) for false claims like "sent to the team"
  in English, Hindi, Telugu and Tamil, with a corrective retry; per-turn latency is shown on screen.

## Next

Telephony through Gnani's LiveKit/Pipecat plugins and 8 kHz µ-law Timbre output; streaming LLM output into TTS to
reach about 1 s; multilingual retrieval for large knowledge bases; post-call analytics with Gnani Batch STT.

**Code:** github.com/sg2006-developer/awaazdesk · **Stack:** FastAPI, WebSockets, AudioWorklet, Gnani Prisma v3.0 + Timbre v3.0/v2.5, Gemini 2.5 Flash
