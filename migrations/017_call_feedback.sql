-- Migration 017: call_feedback table for public & user demo ratings
CREATE TABLE IF NOT EXISTS call_feedback (
    id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    call_id          UUID        REFERENCES calls(id) ON DELETE SET NULL,
    call_control_id  TEXT,
    phone_number     TEXT,
    rating           INTEGER     NOT NULL CHECK (rating >= 1 AND rating <= 5),
    comment          TEXT,
    tags             TEXT[],
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_call_feedback_ccid ON call_feedback(call_control_id);
CREATE INDEX IF NOT EXISTS idx_call_feedback_phone ON call_feedback(phone_number);
