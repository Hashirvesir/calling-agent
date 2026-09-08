-- Migration 013: Per-user STT model (for providers with more than one model)
-- Groq and Deepgram each use exactly one fixed model (see app/core/stt_config.py's
-- FIXED_MODELS), so stt_provider alone was enough to pick a call's STT. Together
-- AI hosts multiple transcription models (Whisper, and others) under one
-- account, so its choice needs a model column too — same shape as
-- llm_model/tts_model. NULL is fine for Groq/Deepgram rows (ignored). Run
-- once in Supabase SQL Editor.

ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS stt_model TEXT;
