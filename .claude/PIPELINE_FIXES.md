# Call Pipeline Fixes — Tracking List

Generated from a full pipeline analysis (`app/services/bot.py`, `app/api/webhooks.py`,
`app/services/tts.py`, `app/services/rag.py`, `app/services/conversation_logger.py`).
Each item is fixed one at a time, checked off here as it lands, backend
restarted and smoke-tested after each fix (or logical group of fixes).

**Explicitly out of scope / not touching:**
- STT fallback (Groq is the sole STT provider) — user said to leave this alone.
- Silero VAD running inline on the event loop — lives inside the `pipecat` library, not our code.
- Single-process/no-`workers=` scaling — an infra decision (multi-worker would break the in-memory RAG/greeting/`_active_ws` caches unless moved to a shared store like Redis), not a quick fix. Flag for a separate conversation, not part of this list.
- Urdu-specific Whisper hallucination phrases — no concrete list of what Whisper actually mis-hears in Urdu exists yet; needs real call logs to build, not guesswork.

---

## Step 1 — TTS: no timeout, no retry
**File:** `app/services/tts.py` (`UpliftStreamingTTSService.run_tts`)
**Problem:** the aiohttp POST to Uplift's streaming endpoint has no per-request timeout. A hung UpliftAI connection stalls that turn for aiohttp's long default before erroring — dead air on a live call. On failure there's also no single retry before giving up.
**Fix:** add an explicit `aiohttp.ClientTimeout` (short — a few seconds is enough for a streaming TTS first-byte) to the POST call, and retry once on timeout/connection error before yielding `ErrorFrame`.
**Status:** [x] done — `connect=5s, sock_connect=5s, sock_read=10s, total=30s` timeout added; retries once if the failure happens before any audio was yielded (never retries mid-stream, to avoid replaying part of an utterance).

## Step 2 — Recording start has no retry
**File:** `app/api/webhooks.py` (`_start_recording`)
**Problem:** one `record_start` attempt; a transient Telnyx hiccup means the whole call goes unrecorded with only a warning log, no retry, no user-facing signal.
**Fix:** retry `record_start` 2–3 times with a short backoff before giving up.
**Status:** [x] done — 3 attempts, 3s backoff between them, clear final error log if all fail.

## Step 3 — Recording download has no retry
**File:** `app/api/webhooks.py` (`_download_and_store_recording`)
**Problem:** one GET attempt against Telnyx's recording URL; a transient failure permanently loses that call's recording.
**Fix:** retry the download 2–3 times with a short backoff before giving up.
**Status:** [x] done — 3 attempts, 3s backoff, only uploads to Supabase Storage once a download actually succeeds.

## Step 4 — Auto-extraction has no retry
**File:** `app/api/webhooks.py` (`_trigger_extraction_after_call`)
**Problem:** fires once, 5s after hangup; if the extraction call throws (transient OpenAI/DB hiccup), that call is never auto-extracted again — needs a manual re-run via `/api/extraction/test`.
**Fix:** retry the extraction call 1–2 times with a short backoff before giving up and logging a clear final failure.
**Status:** [x] done — 2 attempts, 5s backoff; DB lookup for call/agent data stays single-attempt (not a transient-failure case), only `service.extract()` itself retries.

## Step 5 — ConversationLogger blocking file I/O
**File:** `app/services/conversation_logger.py` (`_append`)
**Problem:** every single turn does a synchronous `open()/write()/close()` directly on the event loop — blocks all concurrent calls for the duration of that disk write.
**Fix:** move the file write into `asyncio.to_thread` so it no longer blocks the event loop.
**Status:** [x] done — `_append` is now `async`, awaits `asyncio.to_thread(_write_line, ...)`; both call sites in `on_push_frame` updated to `await`.

## Step 6 — Webhook signature verification silently skips when no public key is set
**File:** `app/api/webhooks.py` (`_verify_webhook_signature`)
**Problem:** if a user hasn't set `telnyx_webhook_public_key` in Settings, signature verification is skipped entirely (fail-open) — anyone who obtains their webhook URL can POST fake Telnyx events.
**Decision (user):** keep fail-open (never lock out a new user), but surface a clear warning in the dashboard Settings page until the key is set — `GET /api/settings` already returns `has_webhook_key`, frontend just needs to act on it.
**Fix:** add a warning banner/badge on the Settings → API & Keys tab when `has_webhook_key` is false.
**Status:** [x] done — amber warning banner in `frontend/src/app/dashboard/settings/page.tsx`, shown above the status badges when `platformSettings.has_webhook_key` is false.

---

## Already fixed earlier this session (for reference, not re-doing)
- RAG cache invalidation on `script_id` change (`app/api/agents.py`)
- Dead `LLMRunFrame` import removed (`app/services/bot.py`)
- Language-aware `end_call`/`check_caller_history` tool descriptions (`app/services/bot.py`)
- Bounded `_rag_cache` and greeting `_cache` with FIFO eviction
- Outbound call webhook delivery (`webhook_url` explicit in `/dial`)
- Restaurant agent system prompt rewritten for payment/total completeness
- ElevenLabs/Groq cost-rate accuracy in `app/core/config.py` + `app/api/calls.py`
- Sindhi/Balochi full removal (backend + frontend)
- Multi-tenancy: `proxy.ts` real edge-level auth check (via `@supabase/ssr` cookie-based session)
- Backend signup/forgot-password code flows (`app/api/auth_flows.py`, Resend + existing `password_reset_codes`/`email_verification_codes` tables)
