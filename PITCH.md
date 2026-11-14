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
| Technician late (English) | ✓ booking CD-1042 | English | Technician delay → human | 3.2 s |
| Filter question (Hindi) | ✓ | Hindi | Filter replacement | 3.1 s |
| Technician late (Telugu) | ✓ booking CD-2042 | Telugu | Technician delay → human | 3.8 s |
| Refund, double charge (Hinglish) | ✓ booking CD-3042 | Hindi | Payment issue → human | 2.4 s |

Prisma finalises speech in ~150 ms. Median end-to-end response is **3.15 s**, and the LLM is the largest share.
55 automated tests cover the call loop, Gnani protocol handling, guardrails and the hosted-demo protections.

## What's novel

- **Language-true voice:** reply language follows the caller, and a separate *spoken* version of each reply spells
  out IDs ("C D one zero four two") so Timbre pronounces them correctly.
- **Built on what the Gnani APIs actually do,** found by testing the live service: live partial transcripts
  (`x-partials`), a second TTS completion frame that breaks reused connections, and why browser audio must be
  anti-aliased before 16 kHz streaming (without it, Telugu came back as Devanagari gibberish).
- **Owner-safe by design:** versioned policies (every call keeps the version it started on), cited answers,
  enforced escalation, and per-turn latency shown on screen.

## Next

Telephony through Gnani's LiveKit/Pipecat plugins and 8 kHz µ-law Timbre output; streaming LLM output into TTS to
reach about 2 s; multilingual retrieval for large knowledge bases; post-call analytics with Gnani Batch STT.

**Code:** github.com/sg2006-developer/awaazdesk · **Stack:** FastAPI, WebSockets, AudioWorklet, Gnani Prisma v3.0 + Timbre v3.0/v2.5, Gemini 2.5 Flash
