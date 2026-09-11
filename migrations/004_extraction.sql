-- Migration: Transcript data extraction system
-- Run once in Supabase SQL editor

-- 1. Add extraction_fields column to scripts table
--    Stores the dynamic schema for each agent/script.
--    Format: [{"name": "customer_name", "description": "...", "expected_type": "string"}, ...]
ALTER TABLE scripts
    ADD COLUMN IF NOT EXISTS extraction_fields JSONB DEFAULT '[]'::jsonb;

-- 2. Create extracted_data table — one row per call
CREATE TABLE IF NOT EXISTS extracted_data (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    call_id         UUID NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    agent_name      TEXT NOT NULL,
    extracted_data  JSONB NOT NULL DEFAULT '{}'::jsonb,
    missing_fields  JSONB NOT NULL DEFAULT '[]'::jsonb,
    confidence      TEXT NOT NULL CHECK (confidence IN ('high', 'partial', 'low')),
    extracted_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_extracted_data_call UNIQUE (call_id)
);

-- Index for fast lookups by call
CREATE INDEX IF NOT EXISTS idx_extracted_data_call_id ON extracted_data (call_id);

-- Index for querying by agent name (useful for dashboards)
CREATE INDEX IF NOT EXISTS idx_extracted_data_agent_name ON extracted_data (agent_name);

-- 3. Enable Row Level Security (inherit your existing RLS policy pattern)
ALTER TABLE extracted_data ENABLE ROW LEVEL SECURITY;

-- Allow service role full access (same pattern as other tables)
CREATE POLICY "service_role_all" ON extracted_data
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);
