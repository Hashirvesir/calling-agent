-- Migration: Per-call latency/token/cost metrics
-- Run once in Supabase SQL editor

CREATE TABLE IF NOT EXISTS call_metrics (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    call_id                     UUID NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    llm_prompt_tokens           INTEGER NOT NULL DEFAULT 0,
    llm_completion_tokens       INTEGER NOT NULL DEFAULT 0,
    tts_uplift_characters       INTEGER NOT NULL DEFAULT 0,
    tts_elevenlabs_characters   INTEGER NOT NULL DEFAULT 0,
    turn_latencies              JSONB NOT NULL DEFAULT '[]'::jsonb,
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_call_metrics_call UNIQUE (call_id)
);

-- Index for fast lookups by call
CREATE INDEX IF NOT EXISTS idx_call_metrics_call_id ON call_metrics (call_id);

-- Enable Row Level Security (inherit existing RLS policy pattern)
ALTER TABLE call_metrics ENABLE ROW LEVEL SECURITY;

-- Allow service role full access (same pattern as other tables)
CREATE POLICY "service_role_all" ON call_metrics
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);
