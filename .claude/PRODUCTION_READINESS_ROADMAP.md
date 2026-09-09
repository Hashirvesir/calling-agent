# Voice AI Calling Pipeline — Production Readiness Roadmap

Compiled from a full-session audit: live call testing, log forensics, code review of
`app/services/bot.py`, `tts.py`, `rag.py`, `stt_config.py`, `llm_config.py`,
`webhooks.py`, `database.py`, plus a check of pipecat-ai's current release against
what's installed here.

Sources checked live during this audit:
- [pipecat-ai on PyPI](https://pypi.org/project/pipecat-ai/) — current version
- [Smart Turn v3 (Hugging Face)](https://huggingface.co/pipecat-ai/smart-turn-v3)
- [Smart Turn on Pipecat Cloud (docs)](https://docs.pipecat.ai/pipecat-cloud/guides/smart-turn)
- [Improved accuracy in Smart Turn v3.1 (Daily.co blog)](https://www.daily.co/blog/improved-accuracy-in-smart-turn-v3-1/)

---

## Update (2026-09-09): status of every item below

Everything in §1-§4 was written the night of the original audit. Since then, on
the `pipecat-upgrade` branch:

- **§1 (pipecat upgrade): DONE.** Upgraded `0.0.108` → `1.8.1`. Turned out much
  less painful than this section predicted — this codebase was already written
  against pipecat's post-1.0 API shape (`LLMContext`, `UserTurnStrategies`,
  `pipecat.services.<provider>.<kind>` import paths), so almost nothing broke.
  The one real, dangerous find: `TransportParams.vad_analyzer` was removed in
  1.x and is *silently dropped* by Pydantic — passing it (as this codebase
  still did) would have shipped a call pipeline with turn-detection completely
  disabled and zero error/warning anywhere. Fixed by moving VAD onto
  `LLMUserAggregatorParams.vad_analyzer` instead (the correct 1.x location).
  Also fixed a deprecated bare `model=` kwarg on the OpenAI LLM fallback.
  Verified via full-repo compile checks, a live smoke test constructing every
  real service this pipeline builds, and several live phone calls placed
  after the upgrade.
- **§2 (Smart Turn): tried, reverted — worse than advertised.** Wired in and
  live-tested via the Test Agent widget. First read: "a bit more natural, but
  noticeably slower" — matching this section's predicted 1-4s cost. Second
  test surfaced something worse: a session where a legitimate transcription
  passed every filter, Smart Turn logged `EndOfTurnState.COMPLETE`, but the
  turn's own "stopped speaking" event fired ~5s later with no strategy
  attributed, and *no LLM/RAG/TTS activity ever followed for the rest of that
  session* — a turn silently never reaching the LLM at all, not just a slow
  one. Reverted to the proven `SpeechTimeoutUserTurnStopStrategy` timeout
  (`_USE_SMART_TURN = False` in `bot.py`, one line to flip back). Root cause
  of the silent-turn failure was not found — revisiting Smart Turn later needs
  that investigated first, not just re-enabling the flag.
- **§4 (one-way audio): still open, mitigated only.** The watchdog shipped and
  is live (`_AUDIO_WATCHDOG_DELAY_SECS` in `bot.py`) — a call with zero
  inbound audio now ends with a short apology in ~6-10s instead of 30-60s of
  dead air. The underlying carrier-side cause is unchanged and still needs the
  Telnyx-support / different-destination-number investigation this section
  describes.
- **New fixes since the original audit** (all on `pipecat-upgrade`): a Whisper
  hallucination gap (Urdu "موسیقی" wasn't in the English-only hallucination
  list, and briefly "شکریہ" was wrongly added to it — a real word, not a
  hallucination — then removed), a `LongNumberAccumulator` FrameProcessor that
  stitches a CNIC/phone number split across turns instead of relying on the
  LLM to remember earlier fragments, an `on_error` handler so a mid-call LLM
  failure (confirmed live: a transient Together AI 503) makes the bot
  apologize and ask the caller to repeat instead of the failover-only path
  leaving them in silence, and a cleanup of the live Bank Agent script (leaked
  internal documentation text that had been pasted into the RAG-retrievable
  content, plus filling in the real branch/helpline/hours placeholder and
  adding a proper outbound Auto Ijarah campaign section).
- **§3's Deepgram question and §5's test-coverage/observability gaps are
  still open** — not yet acted on.

None of this is merged to `main` yet — it's all on `pipecat-upgrade`, pending
the test matrix in §6 before merging.

---

## 1. The single biggest gap: pipecat-ai is ~100 releases behind — RESOLVED, see update above

*(Original write-up kept below for context on what the upgrade was scoped against.)*

**Was installed: `0.0.108`. Current on PyPI at the time: `1.8.1`.**

This project was pinned to a pre-1.0 pipecat release. Pipecat crossed into a stable
`1.x` line at some point after `0.0.108` — that's not a patch bump, it's the
framework's own graduation to a stable API surface, which normally means:
accumulated bug fixes, performance work, and *new built-in capabilities* that this
codebase had been hand-rolling workarounds for instead. Two concrete examples
found in `bot.py` at the time:

- `_AudioFrameProbe`, `_RealtimeVADGate`, `_BrowserEventBridge` are all custom
  `FrameProcessor` subclasses built to patch gaps in the installed version's
  behavior (realtime turn-taking, browser-widget event bridging, audio-arrival
  diagnostics). Turned out all three were still needed post-upgrade — none of
  this was made obsolete by 1.x.
- Turn detection was bare Silero VAD + a fixed silence timeout
  (`stop_secs`) — see §2, this was the actual mechanism behind "user is still
  talking but the agent doesn't answer" / "agent cuts in too early." Still the
  case post-upgrade, since Smart Turn (the intended fix) was reverted.

## 2. Natural turn-taking: this is the real fix for "agent doesn't respond"

Every "user says hello hello, agent stays silent" symptom hunted down this
session traced back to one of:
- Genuine carrier-side one-way audio (§4) — not fixable in this codebase.
- A too-aggressive or too-relaxed `stop_secs` value (500ms caused stalls,
  600ms split CNIC numbers, 1000ms was the safe value found by testing).
- Provider-specific quirks (Deepgram's own internal endpointing racing against
  our own Silero-based one — see §4).

All of that is because turn detection here is **pure silence-timeout VAD**: "the
caller has been quiet for N milliseconds → their turn is over." This is
fundamentally the wrong tool for natural conversation — it can't tell the
difference between "caller paused mid-sentence to think" and "caller is done
talking." Every value of `stop_secs` is a trade-off between cutting people off
and feeling sluggish; there is no single correct number.

**Pipecat's Smart Turn v3** (bundled as `smart-turn-v3.2-cpu`, an ONNX model,
runs locally with no network dependency) replaces this with a real end-of-turn
classifier — it uses actual speech signal (intonation, pacing, linguistic
completeness) to decide whether a pause means "done" or "thinking," the way a
human listener does. This is the single most direct fix available for
"agent doesn't respond naturally" and "agent talks over the caller" — it is
*the* feature this pipeline is missing, not a config tweak. It ships as part of
the pipecat upgrade in §1 (needs a recent pipecat-ai version).

Until the upgrade happens, the fallback is what's already in place: tuned
`stop_secs` per agent (now user-adjustable in Model Config → STT tab), the
"LONG NUMBER CAPTURE RULE" system-prompt patch for split numbers, and the
one-way-audio watchdog (§4) so a genuinely broken call ends quickly instead of
leaving the caller in dead air.

## 3. Everything fixed or found this session (context for what's already solid)

| Issue | Status |
|---|---|
| CNIC/phone numbers split across turns (VAD too aggressive) | Fixed — `stop_secs` raised + system-prompt stitching rule |
| Together AI blocked by Cloudflare (bot-fingerprint) | Fixed — browser User-Agent on direct API calls |
| Groq models disappearing from listings | Fixed — pricing-shape check was Together-specific, wrongly applied to Groq |
| Model Config page slow to load | Fixed — model listings now lazy-load per tab instead of blocking page load |
| RAG retrieval adding ~1.86s per turn (cold OpenAI connection) | Fixed — background connection warm-up during greeting playback |
| Uplift TTS adding ~1.3-2.1s per turn (HTTP endpoint, new connection every turn) | Fixed — rewritten on Uplift's WebSocket streaming API, live-tested to ~0.3s per turn on a warm connection |
| Deepgram STT: 46-second stall + negative TTFB metrics on a real call | **Still open** — recommend avoiding Deepgram in this pipeline until its streaming-STT integration is revisited; Together AI / Groq batch STT has been reliable across every test this session (including post-upgrade) |
| One-way audio on real outbound Telnyx calls (2 of 3 real cascaded calls) | **Mitigated, not fixed** — root cause looks carrier-side (§4); a watchdog now ends the call gracefully within ~6-10s instead of leaving the caller in dead air for 30-60s |
| pipecat 0.0.108 → 1.8.1 upgrade | **Done** — see the 2026-09-09 update above; the critical find was `TransportParams.vad_analyzer` being silently dropped post-upgrade, fixed by moving it to `LLMUserAggregatorParams` |
| Smart Turn (natural turn detection) | **Tried, reverted** — live-tested, found a session where a valid turn silently never reached the LLM at all (worse than the documented latency cost); back to `SpeechTimeoutUserTurnStopStrategy` pending further investigation |
| Whisper hallucination gap: Urdu "موسیقی" mid-CNIC-collection | Fixed — added to `_WHISPER_HALLUCINATIONS`; a later attempt to also add "شکریہ" (a real word, not a hallucination) was itself a regression, caught live and reverted |
| CNIC/long number lost across turns because the LLM didn't reliably carry earlier fragments forward | Fixed — new `LongNumberAccumulator` FrameProcessor stitches fragments together before the LLM ever sees them, instead of relying on the LLM to remember |
| Caller left in total silence when the LLM errors mid-call (confirmed live: Together AI 503, no failover) | Fixed — `on_error` handlers make the bot apologize and ask the caller to repeat instead of leaving dead air |
| Bank Agent's live script had leaked internal documentation text mixed into RAG-retrievable content | Fixed — cleaned in the database; also filled in the real branch/helpline/hours placeholder and added an Auto Ijarah outbound campaign section |

## 4. Open reliability risk: intermittent one-way audio on outbound calls

Real outbound Telnyx calls in cascaded mode: 2 of 3 test calls had **zero**
inbound audio frames reach the pipeline for the entire call, despite Telnyx
confirming `streaming.started` and the WS handshake parsing normally. The one
that worked and the two that failed used **identical code** — different
STT/LLM/TTS provider combinations on all three, ruling out any single service
as the cause.

This matches the classic signature of carrier-side one-way audio (RTP/media
path not cutting through both directions) — not something fixable from
application code. **Action needed from the user, not from this codebase:**
report the two failed calls' `call_control_id`s to Telnyx support for their
SIP/RTP-level logs, and/or test outbound calls to a different destination
number to see if it's specific to this one number's carrier route.

Mitigation already shipped: the watchdog in `bot.py` (`_AUDIO_WATCHDOG_DELAY_SECS`)
ends a call with a short apology within ~6-10 seconds if zero audio has ever
arrived, instead of the caller sitting through 30-60 seconds of dead air.

## 5. Production-readiness gaps beyond the pipeline itself

- **No automated test suite for the pipeline.** `tests/` has one 138-line file
  covering post-call extraction only; `tests/unit/` and `tests/integration/`
  are empty scaffolding. None of `bot.py`'s pipeline construction, provider
  fallback logic, or turn-taking behavior has any regression coverage — every
  fix this session was verified by hand (live calls, log reading, one-off
  scripts). Before scaling past manual QA, at minimum: unit tests for the
  config-resolution functions (`get_llm_config`/`get_stt_config`/`get_tts_config`/
  `get_pipeline_config` — pure functions, cheap to test exhaustively including
  the agent-override-vs-account-default matrix) and an integration test that
  drives `agent_test.py`'s WS route end-to-end against a fixed script.
- **Single-worker, no shared state.** `REDIS_URL not set — running in
  single-worker mode` is the current default. In-memory state (`_rag_cache` in
  `bot.py`, per-call WS connections, `_active_ws` call-dedup) all lives in one
  process. Fine for the current low call volume; will not survive horizontal
  scaling (multiple backend instances behind a load balancer) without wiring
  Redis in for the state that currently assumes a single process.
- **No error tracking/alerting service** (Sentry or equivalent) found in
  `requirements.txt` or `.env.example`. Errors currently surface only in
  `logs/backend_{date}.log` — someone has to be tailing logs to notice a
  production failure. Worth adding before real customer traffic, especially
  given this session's bugs (Deepgram stall, one-way audio) were only caught
  because a human was reading raw logs in real time.
- **Metrics exist but nothing aggregates them for launch-readiness signal.**
  `CallMetricsCollector` captures per-call STT/LLM/TTS timings already — good
  foundation. What's missing is turning that into p50/p95/p99 latency and
  failure-rate dashboards per provider combination, which is exactly the kind
  of statistical view needed to answer "are we ready to launch" with data
  instead of a handful of manual test calls (this session's entire latency
  and one-way-audio findings came from reading three calls by hand — that
  doesn't scale as a QA method).

## 6. Suggested test matrix before calling this "launch ready"

Manual, but structured — run each combination through both a browser Test
Agent session and a real Telnyx call (inbound *and* outbound; this session
only exercised outbound in cascaded mode), and log pass/fail:

| Dimension | Values to cover |
|---|---|
| LLM | Groq, Cerebras, Together (all currently offered) |
| STT | Groq, Together (Deepgram excluded per §3 until revisited) |
| TTS | ElevenLabs, UpliftAI (WS path) |
| Call direction | Inbound, Outbound |
| Scenario | Normal Q&A, long number (CNIC/phone) capture, interruption/barge-in mid-sentence, caller goes silent mid-call, caller hangs up abruptly |

Each real-call run should be checked against the backend log the same way this
session's bugs were found: audio-probe frame counts, TTFB per stage, any
WARNING/ERROR lines — not just "did the call sound OK."

## 7. On the other engineering disciplines mentioned — honest fit assessment

- **ML/AI — yes, directly relevant.** Smart Turn (§2) is a real ML model doing
  real work here (end-of-turn classification from speech signal). The RAG
  layer (`rag.py`) is already a legitimate small ML system (embeddings +
  cosine similarity retrieval) — reasonable next step there is a smarter
  chunking/retrieval strategy only if the current top-k=3 semantic search
  starts missing relevant script content in practice (no evidence of that yet).
- **Statistics — yes.** Percentile-based latency tracking (§5) and structured
  A/B comparison between provider combinations (rather than one-off manual
  calls) is exactly the right tool for deciding things like "is Together AI's
  hosting of gpt-oss-120b actually worse than Groq's" (§ discussed earlier
  this session) with data instead of a single anecdotal call.
- **LangChain — not recommended here.** This is a hard real-time system where
  every millisecond in the turn loop was worth fixing (this session cut ~3.4s
  of per-turn latency out of RAG + TTS alone). LangChain's abstraction layers
  are built for orchestration flexibility, not for shaving milliseconds off a
  synchronous voice turn — adding it would trade latency and debuggability for
  abstraction this pipeline doesn't need, since pipecat already *is* the
  purpose-built real-time orchestration layer here.
- **Calculus — not a meaningful lever for this system.** There's no continuous
  control-loop or optimization surface here that calculus would apply to
  (this isn't, e.g., tuning a PID controller or a continuous-parameter
  optimization problem). Being honest about this rather than forcing it in:
  the actual gains available are the ones in §1-§5.

---

## Priority order (updated 2026-09-09)

1. ~~**Pipecat upgrade**~~ — **Done.** See the update at the top of this file.
   Smart Turn was unlocked and tried, but reverted after live-testing found a
   session where a turn silently never reached the LLM — worse than the
   latency trade-off it was meant to fix. Revisiting Smart Turn is its own
   follow-up item now, not a side effect of the upgrade being done.
2. **Resolve or route around the one-way-audio issue** (§4) — still open. A
   watchdog mitigates the symptom (call ends in ~6-10s instead of 30-60s of
   dead air) but the carrier-side cause is unconfirmed. Needs the user to
   engage Telnyx support with the logged `call_control_id`s and/or test a
   different destination number.
3. **Decide Deepgram's fate** (§3) — still open. Either revisit its
   integration properly, or drop it from the STT options shown to users until
   it's trustworthy — recommendation leans toward dropping it for now given
   zero reliability issues from Groq/Together across every test this session.
4. **Minimum test coverage + latency/error observability** (§5) — still open,
   and now the most valuable remaining item: every bug fixed this session
   (Deepgram's stall, one-way audio, the CNIC stitching gap, the Together 503
   silence, the Smart Turn silent-turn failure) was found by a human reading
   raw logs during or after a live call. None of it would be caught
   automatically today.
5. **New follow-up: investigate the Smart Turn silent-turn bug properly.**
   Not urgent (the revert already restores reliable behavior), but Smart Turn
   remains the most direct fix available for natural turn-taking (§2) — worth
   revisiting once there's time to root-cause why a valid turn stopped
   reaching the LLM under `TurnAnalyzerUserTurnStopStrategy`, rather than
   re-enabling it blind.
