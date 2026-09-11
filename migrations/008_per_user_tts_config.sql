-- Migration 008: Per-user TTS model selection
-- Adds a TTS provider/model pair to user_settings, same pattern as migration
-- 007's llm_provider/llm_model. ElevenLabs is the only provider today (Uplift
-- was dropped for reliability — see app/services/bot.py's TTS engine note),
-- but the column is a provider+model pair so a future provider is a data
-- change, not a schema change. Run once in Supabase SQL Editor.

ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS tts_provider TEXT NOT NULL DEFAULT 'elevenlabs';
ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS tts_model    TEXT NOT NULL DEFAULT 'eleven_turbo_v2_5';
