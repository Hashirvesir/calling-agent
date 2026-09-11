-- =============================================================================
-- Migration 005: Multi-tenancy
-- Run once in Supabase SQL Editor (Dashboard → SQL Editor → New query)
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. user_settings — per-user Telnyx credentials + unique webhook token
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_settings (
    id                          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                     UUID        NOT NULL UNIQUE REFERENCES auth.users(id) ON DELETE CASCADE,
    telnyx_api_key              TEXT,
    telnyx_webhook_public_key   TEXT,
    webhook_token               TEXT        NOT NULL UNIQUE DEFAULT gen_random_uuid()::text,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- -----------------------------------------------------------------------------
-- 2. Add user_id to existing tables
-- -----------------------------------------------------------------------------
ALTER TABLE agents  ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES auth.users(id);
ALTER TABLE scripts ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES auth.users(id);
ALTER TABLE calls   ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES auth.users(id);

-- default_language was added for multi-language agents — add if missing
ALTER TABLE agents  ADD COLUMN IF NOT EXISTS default_language TEXT NOT NULL DEFAULT 'ur';

-- -----------------------------------------------------------------------------
-- 3. Performance indexes
-- -----------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_agents_user_id              ON agents(user_id);
CREATE INDEX IF NOT EXISTS idx_scripts_user_id             ON scripts(user_id);
CREATE INDEX IF NOT EXISTS idx_calls_user_id               ON calls(user_id);
CREATE INDEX IF NOT EXISTS idx_user_settings_user_id       ON user_settings(user_id);
CREATE INDEX IF NOT EXISTS idx_user_settings_webhook_token ON user_settings(webhook_token);

-- -----------------------------------------------------------------------------
-- 4. Row Level Security
-- The backend always uses the service_role key (bypasses RLS).
-- These policies protect direct anon-key access as a defence-in-depth layer.
-- -----------------------------------------------------------------------------
ALTER TABLE user_settings ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "own_settings" ON user_settings;
CREATE POLICY "own_settings" ON user_settings
    FOR ALL USING (auth.uid() = user_id);

ALTER TABLE agents ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "own_agents" ON agents;
CREATE POLICY "own_agents" ON agents
    FOR ALL USING (auth.uid() = user_id);

ALTER TABLE scripts ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "own_scripts" ON scripts;
CREATE POLICY "own_scripts" ON scripts
    FOR ALL USING (auth.uid() = user_id);

ALTER TABLE calls ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "own_calls" ON calls;
CREATE POLICY "own_calls" ON calls
    FOR ALL USING (auth.uid() = user_id);
