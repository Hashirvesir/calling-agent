-- Migration 012: Scope the telnyx_number uniqueness check to active agents
-- Previously telnyx_number had a plain UNIQUE constraint, so a deactivated
-- agent (is_active = false) still permanently held its number — creating a
-- new agent (or reassigning the number to another agent) with that same
-- number failed with "duplicate key value violates unique constraint
-- agents_telnyx_number_key", even though an inactive agent never matches in
-- call routing (get_agent_by_number already filters is_active = true).
-- Replacing it with a partial unique index means only ACTIVE agents compete
-- for a number; inactive agents can keep a stale number value without
-- blocking anyone else from claiming it. Run once in Supabase SQL Editor.

ALTER TABLE agents DROP CONSTRAINT IF EXISTS agents_telnyx_number_key;

CREATE UNIQUE INDEX IF NOT EXISTS agents_telnyx_number_active_key
    ON agents(telnyx_number)
    WHERE is_active = true;
