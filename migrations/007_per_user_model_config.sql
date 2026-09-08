-- Migration 007: Per-user AI model selection
-- Moves LLM/STT provider selection from a global JSON sidecar (llm_config.json,
-- stt_config.json) to per-user columns on user_settings, so each account can
-- pick its own model independently. Run once in Supabase SQL Editor.

ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS llm_provider TEXT NOT NULL DEFAULT 'groq';
ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS llm_model    TEXT NOT NULL DEFAULT 'openai/gpt-oss-120b';
ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS stt_provider TEXT NOT NULL DEFAULT 'groq';
