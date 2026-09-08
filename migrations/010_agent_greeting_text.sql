-- Migration 010: Per-agent custom inbound greeting
-- Previously the inbound greeting text (played on every call, and what
-- startup prewarm pre-synthesizes) was hardcoded per-language in
-- app/services/bot.py's INBOUND_GREETINGS, identical for every agent. This
-- lets each agent override it; NULL/empty falls back to the hardcoded
-- default. Run once in Supabase SQL Editor.

ALTER TABLE agents ADD COLUMN IF NOT EXISTS greeting_text TEXT;
