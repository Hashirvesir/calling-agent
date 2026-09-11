-- Migration 011: Track which LLM provider generated a call's tokens
-- Cost calculation previously priced every call's llm_prompt_tokens/
-- llm_completion_tokens as Groq usage unconditionally (see app/api/calls.py,
-- app/api/billing.py) — harmless while Groq/Cerebras were the only two
-- options (similar pricing), but wrong and misleading now that OpenAI
-- Realtime is a third option with a completely different cost structure.
-- Run once in Supabase SQL Editor.

ALTER TABLE call_metrics ADD COLUMN IF NOT EXISTS llm_provider TEXT;
