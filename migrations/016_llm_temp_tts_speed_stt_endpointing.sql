-- Migration 016: LLM temperature, TTS speaking rate, STT/VAD endpointing sensitivity
-- Per-agent AND per-account (default) — same nullable-inherits pattern as
-- migration 015. NULL means "use the platform default" (see
-- app/core/{llm,tts,stt}_config.py's DEFAULT_TEMPERATURE/DEFAULT_SPEED/
-- DEFAULT_ENDPOINTING_MS). Run once in Supabase SQL Editor.

ALTER TABLE agents ADD COLUMN IF NOT EXISTS llm_temperature    DOUBLE PRECISION;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS tts_speed          DOUBLE PRECISION;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS stt_endpointing_ms DOUBLE PRECISION;

ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS llm_temperature    DOUBLE PRECISION;
ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS tts_speed          DOUBLE PRECISION;
ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS stt_endpointing_ms DOUBLE PRECISION;
