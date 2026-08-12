# PERFORMANCE.md — Start delay + concurrency reality

> Analysis of the call-start latency ("8-9s before the bot speaks") and an honest
> readiness assessment for multi-user / multi-agent concurrent calling.
> Companion to `FLOW.md` and `CLAUDE.md`.

---

## Part 1 — Start delay: pipeline analysis

### Path from call pickup → first greeting audio
```
Telnyx call.answered → streaming_start → Telnyx opens WS /ws
  └─ WS handler:  get_user_settings   (Supabase round-trip)
                  get_agent_by_number (Supabase round-trip)
                  get_caller_history  (Supabase round-trip)
                  get_call_id_by_ccid (Supabase round-trip)
  └─ bot() → run_bot → build STT/TTS/LLM → build pipeline → runner.run()
  └─ on_client_connected → TTSSpeakFrame(greeting)
  └─ TTS first-byte (TTFB) → audio to Telnyx
```

### The big one — already fixed
`run_bot` used to `await rag_task` **before** building the pipeline, so the greeting
waited for the whole script to be embedded (OpenAI embeddings, ~5-8s on a cold
cache). RAG is now resolved **lazily on the first caller turn** — the greeting no
longer waits for it. *(If you still see ~8-9s, the backend was likely not
restarted, OR what you're timing is the first **answer**, not the greeting.)*

### Remaining delay budget (after the RAG fix)
| # | Source | Approx | In our control? |
|---|--------|--------|-----------------|
| 1 | Supabase round-trips before the bot starts (were sequential) | 0.5–1.5s | Yes — parallelize |
| 2 | `caller_history` fetch before greeting (only needed turn-2+) | 0.3–0.8s | Yes — defer/background |
| 3 | TTS TTFB for the greeting (ElevenLabs WS connect / Uplift first byte) | 0.5–1.5s | Mostly — pre-cache → ~0 |
| 4 | Telnyx bidirectional RTP stream setup | 0.5–1s | No — Telnyx side |
| 5 | VAD / transport warm-up (first Silero inference) | 0.2–0.5s | Partial |

Realistic target after our fixes: **~1–1.5s** greeting (plus the Telnyx RTP floor).

### Fixes, ranked by impact / effort
1. **Pre-cache the greeting audio (biggest win).** The greeting text is identical
   for every call of an agent/language. Synthesize it once, cache the PCM, and push
   raw audio frames on connect instead of hitting the TTS engine each call. Removes
   #3 entirely. *Care:* per-engine (ElevenLabs pcm_8000 vs Uplift 22050→resample),
   and barge-in/interruption of raw frames.
2. **Background the caller-history fetch.** Not needed for the greeting — only when
   the caller asks about past records (and `check_caller_history` tool is a live
   fallback). Removes #2 from the critical path. *Care:* `RAGContextInjector` calls
   `context.set_messages()` each turn, so late mutation of the live `messages` list
   must land before the first `LLMContextFrame` to survive.
3. **Parallelize the WS-handler Supabase calls** — `get_user_settings` +
   `get_agent_by_number` + `get_call_id_by_ccid` are independent → one `gather`.
   Cuts #1 from ~3-4 sequential round-trips to ~1-2. **(Done — see commit.)**
4. **Prewarm RAG** for active agents at startup / on agent-save, so even the first
   call's first **answer** is fast (not just the greeting).
5. **Warm the TTS websocket** before the first frame (minor).

---

## Part 2 — Is it really ready for multi-user / multi-agent concurrent calls?

**Short answer: logically yes, operationally not yet for scale.**

### ✅ Works today
- Multi-tenant routing via `/webhook/{webhook_token}` + per-user Telnyx creds + RLS.
- Multiple agents per user — each bound to a Telnyx number; inbound `to_number` →
  agent. Outbound + inbound both supported.
- Async pipeline — one event loop can host several concurrent calls.

### ⚠️ Blocks real scale
1. **Single process / single event loop** (`uvicorn.run(app)`, 1 worker). Silero
   **VAD runs CPU inference in the event loop on every audio frame, per call**. Past
   ~5-15 concurrent calls that core saturates and audio jitter hits **all** calls.
2. **Process-local in-memory state** — `_rag_cache`, `_active_ws`,
   `_outbound_registry`, `_extraction_done`. Running multiple workers/instances to
   use more cores **breaks** these (outbound correlation fails, dedup is per-worker,
   RAG rebuilt per worker). Horizontal scaling is **not safe** until this state moves
   to a shared store (Redis) or routing is made sticky.
3. **`PUBLIC_HOST = ngrok`** — dev only (connection limits, single tunnel).
4. **No load testing, no autoscaling, no per-call concurrency cap / backpressure.**

### Realistic capacity today
- **One process:** ~**5–15 concurrent calls** with acceptable audio; beyond that,
  VAD/CPU jitter.
- **Users/agents count:** effectively unlimited (DB rows). The real limit is
  **simultaneous active calls**, bound by the single process above.

### What production needs (high level — detailed plan to follow)
- Shared state in Redis (`_outbound_registry`, dedup; RAG cache optional).
- Multiple instances behind a load balancer with `uid`/ccid-sticky routing.
- Real domain + TLS; drop ngrok.
- Per-call concurrency limit + queue + graceful reject.
- Consider Pipecat's per-call-worker / Pipecat Cloud model to move VAD/STT off the
  shared event loop.
- Load test at 10 / 50 / 100 concurrent calls, watch CPU + audio metrics.

---

## Measured breakdown — real first call after restart (from logs)
Call answered → greeting audio ≈ **14.7s**. Where it went:
| Window | Time | Cause |
|--------|------|-------|
| WS connect → bot start | ~1.2s | WS parse + **Silero VAD model load (0.8s)** |
| bot start → services built | ~4s | OpenAI STT/LLM + ElevenLabs constructors — **first-call lazy imports** (warms up on later calls) + **Smart Turn v3 model load** |
| services → pipeline StartFrame | ~5s | **RAG embeddings building mid-call (~7.6s) contending with startup** |
| StartFrame → greeting audio | ~3.7s | **ElevenLabs cold websocket connect + first synthesis (TTFB 2.6s)** |

Key insights:
- It was the **first call after restart**, so RAG was cold (built during the call)
  and Python imports were cold. **Later calls are already faster.**
- Warm-call TTS TTFB dropped to **0.6–0.8s** (vs 2.6s cold) once the WS was open.
- STT (OpenAI Whisper) is slow — **~2-5s per turn** — this drives the *answer*
  latency the caller also feels, separate from the greeting.

## Status
- [x] RAG await removed (greeting no longer blocked by embeddings)
- [x] WS-handler Supabase calls parallelized (Fix #3)
- [x] **RAG prewarm at startup (Fix #4)** — removes the ~5s mid-call RAG build
- [x] **Greeting audio pre-cache (Fix #1)** — `greeting_cache.py`; cached PCM replayed
      on connect, prewarmed at startup. Removes the cold TTS TTFB (~2-3.7s).
- [x] **Preload VAD at startup** — warms onnxruntime so the ~0.8s init is paid once.
- [x] **Dropped semantic Smart Turn → VAD-timeout turn stop**
      (`SpeechTimeoutUserTurnStopStrategy`). Smart Turn added 1-4s of
      "has the caller finished?" latency per turn (INCOMPLETE→COMPLETE waits) and
      loaded a model per call. Turn now triggers ~0.6s after the caller pauses.
      Tunable via `user_speech_timeout` in `bot.py` if it cuts callers off.

### Measured from the live Urdu (Orator) call — what's left
- **Greeting: instant from cache** ✅ (`Greeting played from cache`).
- **Uplift Orator TTS: TTFB 2-5s**, processing up to ~24s under interruptions.
  This is largely **server-side (Uplift)** — not fixable in code. If Urdu latency
  stays unacceptable, the only real options are ElevenLabs for Urdu too (faster,
  different voice) or accepting Orator's latency for its Urdu quality.
- **OpenAI Whisper STT: 2-3s/turn + poor Urdu accuracy** (mis-transcribed Urdu as
  Punjabi/garbage). A faster, more accurate STT (Groq/Deepgram) is the real fix —
  deferred (Groq not available right now).

### Remaining levers (need provider change — deferred)
- [ ] Faster + more accurate STT (Groq/Deepgram) — biggest remaining win
- [ ] Orator latency: evaluate ElevenLabs-for-Urdu vs accept
- [ ] Background caller-history (Fix #2) — minor
- [ ] Concurrency / scaling plan (separate doc, next)
