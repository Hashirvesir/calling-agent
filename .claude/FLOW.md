# FLOW.md — Product Flow Guide (Urdu + English only)

> Companion to `CLAUDE.md` (which covers multi-tenancy). This file describes the
> **calling product flow** the user actually sells: create an agent, give it a
> system prompt + script, call it, lock it to ONE language, pull extracted data
> after the call, and let the agent look up a caller's past records mid-call.
>
> **Scope decision (locked in):**
> - Product supports **Urdu** and **English** only.
> - Sindhi / Balochi: **disable UI + usage only.** DB columns and dormant backend
>   code stay (low risk, reversible). No deletion.
> - **TTS engine per language (dual TTS):**
>   - **Urdu → UpliftAI Orator** (existing `UpliftStreamingTTSService`).
>   - **English → ElevenLabs** (`ElevenLabsTTSService`, new third party).
> - Because each call is locked to one language, the TTS engine is chosen **once
>   at call start** from `default_language` — there is no mid-call engine switch.

---

## 1. The product flow (what the user does)

```
1. Sign up / log in                         (Supabase Auth)
2. Settings → paste Telnyx API key + webhook public key   (user_settings table)
3. Create a Script:  name + full script text + language (ur | en)
                     + extraction_fields (what to collect)
4. Create an Agent:  name + system prompt + pick script
                     + default_language (ur | en)  ← THE LANGUAGE LOCK
                     + telnyx_number + telnyx_app_id + voice
5. Point the Telnyx number's webhook at:
                     https://{PUBLIC_HOST}/webhook/{webhook_token}
6. Caller dials the number  →  agent answers, speaks ONLY default_language
7. During call: agent can call check_caller_history to read caller's past records
8. After call: post-call extraction runs → structured data lands in extracted_data
9. Dashboard → Extractions grid: view / edit / export the collected data
```

Everything above **already exists** in the codebase. This guide's job is to make
two things rock-solid and one thing simpler:

- **Rock-solid:** language lock (English agent never drifts to Urdu) + STT
  hearing short replies. *(Both already patched — see §6.)*
- **Simpler:** strip Sindhi/Balochi from the UI and the active language paths so
  the product is cleanly 2-language.

---

## 2. Third-party services (what each one does, what it costs you)

| Service | Used for | Key location | Per-user or system |
|---------|----------|--------------|--------------------|
| **Telnyx** | Phone numbers, inbound/outbound calls, media stream, call recording | `user_settings.telnyx_api_key` + `telnyx_webhook_public_key` | **Per-user** (each tenant brings their own Telnyx account) |
| **OpenAI** | Whisper STT, GPT-4o (conversation), `text-embedding-3-small` (RAG), extraction LLM | `.env` `OPENAI_API_KEY` | System (platform pays) |
| **UpliftAI (Orator)** | **Urdu** text-to-speech | `.env` `UPLIFT_API_KEY` | System (platform pays) |
| **ElevenLabs** *(NEW)* | **English** text-to-speech | `.env` `ELEVENLABS_API_KEY` | System (platform pays) |
| **Supabase** | Auth, Postgres DB, recordings storage bucket | `.env` (service role) + frontend (anon key) | System |

**New third party = ElevenLabs.** Setup required:
- Add `ELEVENLABS_API_KEY` to `.env` and to `Settings` in `app/core/config.py`.
- Install the SDK: add `elevenlabs` to `requirements.txt` (`pip install elevenlabs`).
  The `elevenlabs` Python package is **not currently installed** in `.venv`.
- Pick a default English **voice ID** + **model** (recommend a multilingual /
  turbo model for low call latency, e.g. `eleven_turbo_v2_5`). Pipecat exposes
  `ElevenLabsTTSService` (WebSocket streaming, low latency) in
  `pipecat.services.elevenlabs.tts`.

---

## 3. The voice pipeline (per call)

```
Telnyx WS → OpenAI Whisper STT → STTNoiseFilter
→ user_aggregator → RAGContextInjector → GPT-4o
→ TTS  (UpliftAI Orator if ur  |  ElevenLabs if en)   ← chosen once at call start
→ Telnyx WS output
```

- The TTS engine is selected in `run_bot` from `agent.default_language`:
  - `ur` → `UpliftStreamingTTSService` (Orator voice id from `agent.voice_urdu`).
  - `en` → `ElevenLabsTTSService` (English voice id from `agent.voice_english`).
- Selection is **static for the whole call** because the language is locked, so
  there is no cross-engine switching to manage at runtime.
- `LanguageVoiceSwitcher` / `PreTTSLanguageSwitcher` existed to swap *Uplift voice
  IDs* mid-call for sd/bal. With a locked single language they are **no-ops** and
  should be **removed from the pipeline** (cleanup) — they cannot switch between
  two different engines anyway. The language lock now lives entirely in:
  (1) the STT language seed, (2) the LLM `LANGUAGE RULE`, (3) the RAG reply-language
  clause — i.e. the **prompt + STT layer**, not a voice-switch layer.
- `agent.voice_english` now holds an **ElevenLabs voice ID** (not an Uplift
  `v_...` ID). This is a meaning change for that column — see §7.

---

## 4. How the language lock works (the important part)

The agent's `default_language` is authoritative. Three layers enforce it:

1. **STT language** — `OpenAISTTService(language=...)` seeded from
   `default_language` so Whisper transcribes in the right script from turn 1.
2. **LLM system messages** (`bot.py` `run_bot`): a hard `LANGUAGE RULE` system
   message — *"You MUST speak and reply ONLY in {English|Urdu} for the entire
   call… Never switch languages."*
3. **Per-turn RAG injection** (`rag.py` `RAGContextInjector`): the retrieved
   script context is wrapped with a reply-language clause **in the same
   language** as `response_language`. *(This was the drift bug — see §6.)*

Greetings, history fillers, and outbound greetings all have `ur` and `en`
variants keyed by `default_language`, so an English agent never emits a stray
Urdu sentence.

---

## 5. Extraction flows (the two data features)

### 5a. Post-call extraction (automatic, after hangup)
- Trigger: `webhooks.py` `_trigger_extraction_after_call(call_control_id)` fires
  on `call.hangup` / `streaming.stopped` / `streaming.failed`.
- It waits ~5s, loads the agent + its `extraction_fields`, builds an
  `ExtractionSchema`, and runs `ExtractionService.extract(...)` over the saved
  transcript turns.
- Result is stored in `extracted_data` and shown in the dashboard Extractions grid.
- **Language note:** `prompt_builder._SYSTEM_PROMPT` forces every extracted value
  to **English** regardless of transcript language. Keep that rule.

### 5b. Mid-call history lookup (the agent checks past records)
- Tool `check_caller_history` is registered on the LLM (`bot.py`).
- When the caller asks "did I book before / what's my last record", GPT-4o calls
  the tool → `search_caller_records(phone, agent_id)` → returns prior
  `extracted_data` rows → the agent answers from them.
- A spoken filler ("one moment, let me check…") plays immediately in the agent's
  locked language so the caller isn't met with silence during the DB lookup.
- Caller phone is normalized (`app/core/phone.py`) before lookup.

---

## 6. Bugs already fixed in this session

These two were the user's original complaints and are **already patched**:

1. **"Awaz kabhi kabhi sahi se nahi sunta"** — `STTNoiseFilter.MIN_CHARS` was 3,
   silently dropping short replies like *ji / ok / no / ہا*. Lowered to 2 + added
   a short-reply allow-list + digit exemption. (`app/services/bot.py`)
2. **English agent sometimes replied in Urdu** — `RAGContextInjector` wrapped the
   per-turn RAG context with a hardcoded **Urdu** instruction even for English
   agents, nudging GPT-4o into Urdu. Now the wrapper matches `response_language`.
   (`app/services/rag.py`)

---

## 7. Remaining work (the cleanup to make it cleanly 2-language)

> Approach = **disable UI + usage only.** Do NOT delete DB columns, normalizers,
> the HTTP/MP3 TTS path, or miniaudio. Just stop offering/using sd & bal.

### Frontend (remove the options users can pick)
- `frontend/src/app/dashboard/scripts/create/page.tsx` — drop `sd` & `bal` from
  the language option list (keep `ur`, `en`).
- `frontend/src/app/dashboard/scripts/[id]/edit/page.tsx` — same.
- `frontend/src/app/dashboard/scripts/page.tsx` — language label map: keep ur/en.
- `frontend/src/app/dashboard/agents/create/page.tsx` — `default_language`
  selector limited to ur/en; stop sending `voice_sindhi` / `voice_balochi`
  (let DB defaults stand) OR keep sending the defaults silently. Hide the
  Sindhi/Balochi voice pickers.
- `frontend/src/app/dashboard/agents/[id]/edit/page.tsx` — same as create.
- `frontend/src/app/dashboard/agents/[id]/page.tsx` — remove the
  "Voice (Sindhi)" / "Voice (Balochi)" detail rows from the display.

### Backend — dual TTS routing (the real new work)
- `app/core/config.py` — add `elevenlabs_api_key: str` and English voice defaults
  (`voice_english_default` = an ElevenLabs voice ID, `elevenlabs_model` =
  e.g. `eleven_turbo_v2_5`). `.env` / `.env.example` get `ELEVENLABS_API_KEY`.
- `requirements.txt` — add `elevenlabs`.
- `app/services/bot.py` `run_bot` — build the TTS engine from `default_language`:
  - `en` → `ElevenLabsTTSService(api_key=settings.elevenlabs_api_key,
    voice_id=agent.voice_english, model=...)`.
  - `ur` → existing `UpliftStreamingTTSService(... voice_id=agent.voice_urdu)`.
  - Remove `LanguageVoiceSwitcher` + `PreTTSLanguageSwitcher` from the pipeline
    (no-ops under the lock). Keep the STT-language seed + LLM `LANGUAGE RULE`.
- `app/services/bot.py` — `LANGUAGE_NAMES`, greetings, history fillers: keep only
  `ur`/`en`. `_detect_lang`, `LANGUAGE_VOICE_MAP`, regional branches become dead
  once the switchers are gone — safe to delete or leave dormant (your call).
- `app/extraction/prompt_builder.py` — `_SYSTEM_PROMPT` mentions
  "Sindhi/Balochi". Harmless. Optional: trim to "Urdu / Roman Urdu / English".

### DB / column meaning change
- `agents.voice_english` previously held an Uplift `v_...` ID (unused). It now
  holds an **ElevenLabs voice ID**. Frontend agent create/edit must let the user
  pick/enter an ElevenLabs voice for English agents. Provide a sensible default
  so existing rows still work.

### What NOT to touch
- `app/services/sindhi_normalizer.py`, `app/services/balochi_normalizer.py` — keep.
- `UpliftStreamingTTSService` HTTP/MP3 path + `miniaudio` — keep (dormant).
- DB `agents.voice_sindhi` / `voice_balochi` columns — keep.

---

## 8. Open items to confirm before implementation

1. **Work mode** — start implementation now, or wait for explicit go-ahead?
2. **ElevenLabs account** — need a valid `ELEVENLABS_API_KEY` and a chosen English
   **voice ID** + **model** before English calls can be tested end-to-end.
3. **ElevenLabs billing model** — system-level key in `.env` (platform pays for
   all tenants' English TTS), same as OpenAI/Uplift. Confirm that's intended
   (vs. per-user, which would mean another `user_settings` column).

---

## 9. Implementation order (when greenlit)

1. **ElevenLabs wiring (backend):** `requirements.txt` + `pip install elevenlabs`;
   `ELEVENLABS_API_KEY` in `.env`/`.env.example`; `elevenlabs_api_key` + English
   voice/model defaults in `config.py`.
2. **Dual TTS in `bot.py`:** pick engine from `default_language`; drop the
   `LanguageVoiceSwitcher` / `PreTTSLanguageSwitcher` stages.
3. **Frontend:** remove sd/bal from script + agent language selectors and detail
   views; make the English agent voice field an ElevenLabs voice picker/input.
4. **Smoke test (Urdu):** create an Urdu agent → call → Urdu-only replies, short
   answers heard, Orator voice.
5. **Smoke test (English):** create an English agent → call → English-only replies
   via ElevenLabs, short answers heard.
6. Verify post-call extraction populates `extracted_data` and the grid renders it.
7. Verify `check_caller_history` returns a prior record on a repeat caller.
```
