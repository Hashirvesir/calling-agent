-- Migration 009: Per-user voice pipeline mode (cascaded vs. OpenAI Realtime)
-- Adds an opt-in speech-to-speech mode alongside the existing cascaded
-- STT/LLM/TTS pipeline, same per-user pattern as migrations 007/008. Run once
-- in Supabase SQL Editor.

ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS voice_pipeline_mode TEXT NOT NULL DEFAULT 'cascaded';
ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS realtime_voice      TEXT NOT NULL DEFAULT 'marin';
