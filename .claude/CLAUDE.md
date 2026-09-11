# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Mission

Convert this single-tenant voice AI platform into a multi-tenant SaaS. Each user logs in and provides their own Telnyx credentials. OpenAI and UpliftAI keys stay system-level in `.env`.

**Which keys are per-user vs system:**

| Key                                               | Where                                               |
| ------------------------------------------------- | --------------------------------------------------- |
| `TELNYX_API_KEY`, `TELNYX_WEBHOOK_PUBLIC_KEY`     | Per-user — `user_settings` DB table (account-level) |
| `telnyx_app_id`, `telnyx_number`                  | Per-agent — `agents` table (already there)          |
| `OPENAI_API_KEY`, `UPLIFT_API_KEY`, `PUBLIC_HOST` | System — `.env` (platform pays, shared)             |

**Rule: No new features. Only multi-tenancy changes.**

---

## Commands

```bash
# Backend
pip install -r requirements.txt
python main.py                    # port 7860

# Frontend (inside frontend/)
npm install && npm run dev        # port 3000
```

`frontend/.env.local`:

```
NEXT_PUBLIC_API_URL=http://localhost:7860
NEXT_PUBLIC_SUPABASE_URL=...
NEXT_PUBLIC_SUPABASE_ANON_KEY=...
```

---

## Current Architecture (Single-Tenant — as of latest code)

`.env` → `app/core/config.py` (`Settings`) → all keys global

### Backend files

| File                               | Role                                                                                                                                                                                  |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `app/core/config.py`               | Pydantic `Settings` — reads `.env`. Still has Telnyx keys (needs removing for multi-tenancy)                                                                                          |
| `app/core/database.py`             | Supabase async client + all CRUD helpers. Uses `service_role_key`. Has upload/download recording functions. All storage calls use `await` directly (not `asyncio.to_thread`)          |
| `app/core/voice_config.py`         | Global default Urdu voice — persists in `voice_config.json` sidecar                                                                                                                   |
| `app/core/phone.py`                | Phone number normalization utility                                                                                                                                                    |
| `app/api/webhooks.py`              | `POST /webhook`, `WS /ws`, `POST /dial`. Reads `settings.telnyx_api_key` directly                                                                                                     |
| `app/api/agents.py`                | CRUD `/api/agents` — no auth, no user_id filtering                                                                                                                                    |
| `app/api/scripts.py`               | CRUD `/api/scripts` — no auth, no user_id filtering                                                                                                                                   |
| `app/api/calls.py`                 | Read-only calls, stats, conversation. `GET /calls/{filename}` downloads from Supabase Storage and serves directly                                                                     |
| `app/api/extraction.py`            | Post-call extraction. Has `DELETE /api/extraction/row/{call_id}` and `PATCH /api/extraction/row/{call_id}` for manual edits                                                           |
| `app/api/settings.py`              | `GET /api/settings` — currently reads from `settings` object (`.env`). No `PUT` endpoint yet                                                                                          |
| `app/api/voice_config.py`          | `GET/PUT /api/voice-config` — global Urdu voice selector                                                                                                                              |
| `app/services/bot.py`              | Pipecat voice pipeline. Initial greeting via `TTSSpeakFrame` (no LLM delay). RAG built as background task. Number rule injected as system message. Reads Telnyx creds from `settings` |
| `app/services/tts.py`              | UpliftAI streaming TTS (Urdu streaming, Sindhi/Balochi HTTP+miniaudio)                                                                                                                |
| `app/services/rag.py`              | OpenAI `text-embedding-3-small` RAG. `RAGContextInjector` injects conv_state + script context per turn                                                                                |
| `app/extraction/prompt_builder.py` | Extraction LLM prompts. **Values extracted in English only** (rule 8 in system prompt)                                                                                                |

### Voice pipeline (per call)

```
Telnyx WS → OpenAI Whisper STT → STTNoiseFilter → LanguageVoiceSwitcher
→ LLMContextAggregator → RAGContextInjector → GPT-4o → PreTTSLanguageSwitcher
→ UpliftStreamingTTS → Telnyx WS output
```

### Current DB tables (no `user_id` anywhere — this is what needs to change)

| Table              | Key columns                                                                                                                                                    |
| ------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `agents`           | `id`, `name`, `telnyx_number`, `telnyx_app_id`, `system_prompt_override`, `script_id`, `voice_urdu/sindhi/english/balochi`, `default_language`, `is_active`    |
| `scripts`          | `id`, `name`, `content`, `language`, `extraction_fields` (jsonb), `is_active`                                                                                  |
| `calls`            | `id`, `call_control_id`, `agent_id`, `direction`, `from_number`, `to_number`, `status`, `started_at`, `ended_at`, `duration_seconds`, `recording_storage_path` |
| `transcript_turns` | `call_id`, `speaker`, `text`, `turn_index`, `timestamp_in_call`                                                                                                |
| `extracted_data`   | `call_id`, `agent_name`, `extracted_data` (jsonb), `confidence`, `missing_fields`, `extracted_at`                                                              |

### Supabase Storage

- Bucket: `recordings`
- Upload: `upload_recording(call_control_id, mp3_bytes)` → `rec_{safe_ccid}.mp3`
- Serve: `GET /calls/{filename}` → `download_recording(filename)` → bytes → `audio/mpeg` response
- `get_recording_signed_url` exists but unused — `download_recording` is preferred

### Frontend (`frontend/` — Next.js 16 App Router)

| Path                                          | Status                                                                                                                    |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| `src/app/sign-in/page.tsx`                    | UI exists, **no auth logic wired**                                                                                        |
| `src/app/sign-up/page.tsx`                    | UI exists, **no auth logic wired**                                                                                        |
| `src/app/dashboard/`                          | Full dashboard — agents, scripts, calls, extractions                                                                      |
| `src/app/dashboard/settings/page.tsx`         | Profile tab (dummy) + API/Keys tab (reads from `/api/settings`, no save)                                                  |
| `src/lib/api.ts`                              | All API calls — **no JWT token sent**                                                                                     |
| `src/lib/supabase.ts`                         | **Does not exist yet**                                                                                                    |
| `src/middleware.ts`                           | **Does not exist yet**                                                                                                    |
| `src/components/dashboard/ExtractionGrid.tsx` | AG Grid table with inline cell editing (`valueSetter` + `PATCH /api/extraction/row/{call_id}`), delete rows, export Excel |

**Frontend dependencies added:** `sonner` (toast notifications — `<Toaster>` in `layout.tsx`)

---

## What Must Change for Multi-Tenancy

### 1. Supabase — New table `user_settings`

```sql
create table user_settings (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references auth.users(id) on delete cascade unique not null,
  telnyx_api_key text,
  telnyx_webhook_public_key text,
  webhook_token text unique default gen_random_uuid()::text,
  created_at timestamptz default now()
);
```

### 2. Supabase — Add `user_id` to existing tables

```sql
alter table agents add column user_id uuid references auth.users(id);
alter table scripts add column user_id uuid references auth.users(id);
alter table calls add column user_id uuid references auth.users(id);
```

### 3. Supabase — Row Level Security

```sql
alter table agents enable row level security;
alter table scripts enable row level security;
alter table calls enable row level security;
alter table user_settings enable row level security;

create policy "own_agents"   on agents        for all using (auth.uid() = user_id);
create policy "own_scripts"  on scripts       for all using (auth.uid() = user_id);
create policy "own_calls"    on calls         for all using (auth.uid() = user_id);
create policy "own_settings" on user_settings for all using (auth.uid() = user_id);
```

### 4. Backend — Auth middleware

New file `app/core/auth.py`:

```python
from fastapi import Depends, HTTPException, Header
from jose import jwt

async def get_current_user(authorization: str = Header(...)) -> str:
    token = authorization.removeprefix("Bearer ")
    payload = jwt.decode(token, options={"verify_signature": False})
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(401, "Invalid token")
    return user_id
```

Inject `user_id: str = Depends(get_current_user)` into every route in `agents.py`, `scripts.py`, `calls.py`, `extraction.py`, `settings.py`, `voice_config.py`.

### 5. Backend — `config.py` after migration

Remove Telnyx fields from `Settings` (they move to `user_settings` table):

```python
class Settings(BaseSettings):
    supabase_url: str
    supabase_service_role_key: str
    supabase_anon_key: str
    openai_api_key: str
    uplift_api_key: str
    public_host: str = ""
    port: int = 7860
    # voice IDs stay — they are system defaults
    voice_urdu_default: str = "v_8eelc901"
    voice_sindhi: str = "v_sd0kl3m9"
    voice_balochi: str = "v_bl1de2f7"
    voice_english: str = "v_8eelc901"
```

### 6. Backend — Webhook routing (most critical)

Telnyx sends webhooks to ONE URL. Each user has their own Telnyx account.

**Solution**: Per-user webhook URL via `webhook_token`:

```
POST /webhook/{webhook_token}
```

In `app/api/webhooks.py`:

- `@router.post("/webhook")` → `@router.post("/webhook/{webhook_token}")`
- On receipt: look up user by `webhook_token` → load their Telnyx creds → process
- All `settings.telnyx_api_key` / `settings.telnyx_webhook_public_key` → fetched from `user_settings` by webhook_token
- `_verify_webhook_signature` receives the key from DB, not settings

### 7. Backend — DB functions get `user_id`

Every function touching `agents`, `scripts`, `calls` must filter by `user_id`.

Affected functions in `database.py`:

- `get_all_agents`, `get_agent_by_id`, `get_agent_by_number`, `create_agent`, `update_agent`, `delete_agent`
- `get_all_scripts`, `get_script_by_id`, `create_script`, `update_script`, `delete_script`
- `get_calls_list`, `create_call`, `get_caller_history`, `search_caller_records`
- `get_extracted_data_by_agent`

New functions to add:

- `get_user_by_webhook_token(token: str) -> dict | None`
- `get_user_settings(user_id: str) -> dict | None`
- `save_user_settings(user_id: str, telnyx_api_key: str, telnyx_webhook_public_key: str) -> dict | None`

### 8. Backend — bot.py receives Telnyx credentials

Currently bot reads from `settings`. After:

```python
async def run_bot(..., telnyx_api_key: str, ...):
    # telnyx_api_key from user_settings
    # agent["telnyx_app_id"], agent["telnyx_number"] from agents table (unchanged)
    # OpenAI/UpliftAI still from settings
```

RAG cache key must be user-scoped:

```python
# Before: _rag_cache[agent_id]
# After:  _rag_cache[f"{user_id}:{agent_id}"]
```

### 9. Backend — settings.py rewrite

Current `GET /api/settings` reads from `.env`. Replace with:

```
GET /api/settings   → read from user_settings table for current JWT user
PUT /api/settings   → save telnyx_api_key + telnyx_webhook_public_key for current JWT user
```

Response includes `webhook_url: https://{PUBLIC_HOST}/webhook/{webhook_token}`.

### 10. Backend — dial endpoint

Currently uses `settings.telnyx_from_number` and `settings.telnyx_api_key`. After:

- Phone number: `agent["telnyx_number"]` (already per-agent in DB)
- API key: `user_settings["telnyx_api_key"]` (from DB by JWT user)

### 11. Frontend — Supabase Auth client

Create `frontend/src/lib/supabase.ts`:

```ts
import { createClient } from "@supabase/supabase-js";
export const supabase = createClient(
  process.env.NEXT_PUBLIC_SUPABASE_URL!,
  process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,
);
```

### 12. Frontend — Route protection

Create `frontend/src/middleware.ts` — redirect unauthenticated users away from `/dashboard/*`.

### 13. Frontend — Wire sign-in / sign-up

- `sign-in/page.tsx` → `supabase.auth.signInWithPassword({ email, password })`
- `sign-up/page.tsx` → `supabase.auth.signUp({ email, password })`

### 14. Frontend — JWT in every API call

`src/lib/api.ts` — modify `apiFetch` and all direct `fetch` calls:

```ts
const {
  data: { session },
} = await supabase.auth.getSession();
headers: {
  Authorization: `Bearer ${session?.access_token}`;
}
```

### 15. Frontend — Settings page: Telnyx credentials form

`src/app/dashboard/settings/page.tsx` "API & Keys" tab:

- Replace dummy display with editable form
- Fields: **Telnyx API Key**, **Webhook Public Key**
- On load: `GET /api/settings` → show masked values
- On save: `PUT /api/settings` → persist to `user_settings`
- Show user's personal webhook URL

---

## Critical Notes

**Supabase service role key bypasses RLS** — always use it server-side (webhook handler, bot, any lookup by webhook_token). The anon key is for frontend only.

**`server.py` is dead code** — superseded by `main.py`. Do not touch it.

**Extraction grid** — `ExtractionGrid.tsx` uses AG Grid `valueSetter` to call `PATCH /api/extraction/row/{call_id}`. When adding `user_id` to extraction endpoints, this flow must remain intact.

**Recording storage** — `GET /calls/{filename}` downloads from Supabase Storage bucket `recordings` using `download_recording()`. Storage is not user-scoped (single bucket, filename is unique per call_control_id). No change needed for multi-tenancy.

**Number formatting** — `bot.py` injects a system message every call forcing English number pronunciation. Keep this message when refactoring.

**Extraction English-only** — `app/extraction/prompt_builder.py` has CRITICAL LANGUAGE RULE at top of `_SYSTEM_PROMPT`. Do not remove.

**Frontend Next.js 16** — per `frontend/AGENTS.md`: this version has breaking API changes. Check `node_modules/next/dist/docs/` before writing Next.js code.

**`sonner` toast** — already installed. `<Toaster>` is in `layout.tsx`. Use `toast.success()` / `toast.error()` for all mutations.

---

## Implementation Order

1. Supabase SQL — `user_settings` table + `user_id` columns + RLS policies
2. `database.py` — `get_user_by_webhook_token`, `get_user_settings`, `save_user_settings` + `user_id` param on all CRUD functions
3. `app/core/auth.py` — JWT dependency (`get_current_user`)
4. `app/core/config.py` — remove Telnyx fields from `Settings`
5. `app/api/webhooks.py` — `/webhook/{webhook_token}` + per-user Telnyx credentials
6. `app/api/agents.py`, `scripts.py`, `calls.py`, `extraction.py`, `voice_config.py` — inject `user_id`
7. `app/services/bot.py` — accept `telnyx_api_key` param; fix RAG cache key to `{user_id}:{agent_id}`
8. `app/api/settings.py` — rewrite `GET` + add `PUT` using `user_settings` table
9. `frontend/src/lib/supabase.ts` — Supabase client singleton
10. `frontend/src/middleware.ts` — route protection
11. `sign-in/page.tsx` + `sign-up/page.tsx` — wire auth
12. `frontend/src/lib/api.ts` — attach JWT to all requests
13. `frontend/src/app/dashboard/settings/page.tsx` — Telnyx credentials form with save
