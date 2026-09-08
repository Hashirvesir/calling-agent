-- Migration 015: Per-agent LLM/STT/TTS/Pipeline-Mode overrides
-- Mirrors user_settings' llm_provider/llm_model/stt_provider/stt_model/
-- tts_provider/tts_model/voice_pipeline_mode/realtime_voice, but on agents
-- instead of user_settings. NULL (no default) is the sentinel: an agent with
-- all eight columns NULL inherits the owning account's user_settings
-- selection unchanged (see app/core/{llm,stt,tts,pipeline}_config.py's
-- get_*_config functions). Run once in Supabase SQL Editor.

ALTER TABLE agents ADD COLUMN IF NOT EXISTS llm_provider        TEXT;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS llm_model           TEXT;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS stt_provider        TEXT;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS stt_model           TEXT;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS tts_provider        TEXT;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS tts_model           TEXT;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS voice_pipeline_mode TEXT;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS realtime_voice      TEXT;
