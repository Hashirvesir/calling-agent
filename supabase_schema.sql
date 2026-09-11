-- =============================================================================
-- Invenco Call Agent — Supabase Schema
-- Run this once in Supabase SQL Editor (Dashboard → SQL Editor → New query)
-- =============================================================================

-- ─── Extensions ──────────────────────────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- =============================================================================
-- CORE TABLES
-- =============================================================================

-- ─── 1. Scripts ──────────────────────────────────────────────────────────────
-- Sales / call scripts that agents use to answer questions.
CREATE TABLE IF NOT EXISTS scripts (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    name         TEXT        NOT NULL,
    content      TEXT        NOT NULL,
    language     TEXT        NOT NULL DEFAULT 'ur',   -- ur | sd | en | bal
    is_active    BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─── 2. Agents ───────────────────────────────────────────────────────────────
-- Each agent has a name, a Telnyx number, and an assigned script.
CREATE TABLE IF NOT EXISTS agents (
    id                      UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    name                    TEXT        NOT NULL,
    script_id               UUID        REFERENCES scripts(id) ON DELETE SET NULL,
    telnyx_number           TEXT        UNIQUE NOT NULL,   -- E.164 e.g. +923001234567
    telnyx_app_id           TEXT,                          -- connection ID for outbound
    system_prompt_override  TEXT,                          -- replaces default SYS_PROMPT when set
    voice_urdu              TEXT        NOT NULL DEFAULT 'v_8eelc901',
    voice_sindhi            TEXT        NOT NULL DEFAULT 'v_sd0kl3m9',
    voice_english           TEXT        NOT NULL DEFAULT 'v_8eelc901',
    voice_balochi           TEXT        NOT NULL DEFAULT 'v_bl1de2f7',
    is_active               BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─── 3. Calls ────────────────────────────────────────────────────────────────
-- One row per call, created at call.initiated and updated throughout.
CREATE TABLE IF NOT EXISTS calls (
    id                      UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    call_control_id         TEXT        UNIQUE,            -- Telnyx call_control_id
    agent_id                UUID        REFERENCES agents(id) ON DELETE SET NULL,
    direction               TEXT        CHECK(direction IN ('inbound', 'outbound')),
    from_number             TEXT,
    to_number               TEXT,
    status                  TEXT        NOT NULL DEFAULT 'initiated'
                                        CHECK(status IN (
                                            'initiated', 'dialing', 'in_progress',
                                            'ended', 'stream_failed', 'unknown'
                                        )),
    started_at              TIMESTAMPTZ,
    ended_at                TIMESTAMPTZ,
    duration_seconds        INTEGER,
    recording_storage_path  TEXT,                          -- Supabase Storage path
    turn_count              INTEGER     NOT NULL DEFAULT 0,
    detected_language       TEXT,                          -- last detected: ur | sd | en | bal
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─── 4. Call Events ──────────────────────────────────────────────────────────
-- Raw Telnyx webhook events log — every event persisted for auditability.
CREATE TABLE IF NOT EXISTS call_events (
    id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    call_control_id  TEXT,
    call_id          UUID        REFERENCES calls(id) ON DELETE CASCADE,
    event_type       TEXT        NOT NULL,
    direction        TEXT,
    from_number      TEXT,
    to_number        TEXT,
    call_leg_id      TEXT,
    occurred_at      TIMESTAMPTZ,
    logged_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    raw_payload      JSONB,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─── 5. Transcript Turns ─────────────────────────────────────────────────────
-- Every USER and BOT turn, ordered by turn_index within a call.
CREATE TABLE IF NOT EXISTS transcript_turns (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    call_id             UUID        NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    speaker             TEXT        NOT NULL CHECK(speaker IN ('USER', 'BOT')),
    text                TEXT        NOT NULL,
    turn_index          INTEGER     NOT NULL,
    timestamp_in_call   TEXT,                              -- HH:MM:SS wall-clock
    detected_language   TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- =============================================================================
-- INDEXES
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_calls_ccid         ON calls(call_control_id);
CREATE INDEX IF NOT EXISTS idx_calls_agent        ON calls(agent_id);
CREATE INDEX IF NOT EXISTS idx_calls_status       ON calls(status);
CREATE INDEX IF NOT EXISTS idx_calls_started      ON calls(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_call_id     ON call_events(call_id);
CREATE INDEX IF NOT EXISTS idx_events_ccid        ON call_events(call_control_id);
CREATE INDEX IF NOT EXISTS idx_events_type        ON call_events(event_type);
CREATE INDEX IF NOT EXISTS idx_turns_call         ON transcript_turns(call_id);
CREATE INDEX IF NOT EXISTS idx_turns_order        ON transcript_turns(call_id, turn_index);
CREATE INDEX IF NOT EXISTS idx_agents_number      ON agents(telnyx_number);

-- =============================================================================
-- TRIGGERS — auto-update updated_at
-- =============================================================================

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER scripts_updated_at      BEFORE UPDATE ON scripts      FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER agents_updated_at       BEFORE UPDATE ON agents       FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER calls_updated_at        BEFORE UPDATE ON calls        FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- =============================================================================
-- RPC FUNCTIONS
-- =============================================================================

-- Atomically increment turn_count on a call (called after each transcript turn)
CREATE OR REPLACE FUNCTION increment_call_turn_count(p_call_control_id TEXT)
RETURNS VOID AS $$
BEGIN
    UPDATE calls
    SET turn_count = turn_count + 1,
        updated_at = NOW()
    WHERE call_control_id = p_call_control_id;
END;
$$ LANGUAGE plpgsql;

-- =============================================================================
-- SUPABASE STORAGE — create bucket for call recordings
-- Run this separately if the bucket doesn't exist yet:
--
--   INSERT INTO storage.buckets (id, name, public)
--   VALUES ('recordings', 'recordings', false)
--   ON CONFLICT (id) DO NOTHING;
--
-- =============================================================================
INSERT INTO storage.buckets (id, name, public)
VALUES ('recordings', 'recordings', false)
ON CONFLICT (id) DO NOTHING;

-- =============================================================================
-- ROW LEVEL SECURITY (optional — enable if using anon key from frontend)
-- =============================================================================
-- ALTER TABLE scripts            ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE agents             ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE calls              ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE call_events        ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE transcript_turns   ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE lead_profiles      ENABLE ROW LEVEL SECURITY;
