-- Migration 014: Track which STT provider transcribed a call
-- Cost calculation previously priced every call's STT time as Groq Whisper
-- usage unconditionally (see app/api/calls.py, app/api/billing.py) —
-- already wrong for Deepgram calls, and now also wrong for Together AI
-- calls, each with a different per-minute rate. Run once in Supabase SQL
-- Editor.

ALTER TABLE call_metrics ADD COLUMN IF NOT EXISTS stt_provider TEXT;
