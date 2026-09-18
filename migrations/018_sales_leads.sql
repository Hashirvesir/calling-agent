-- Migration 018: sales_leads table for Talk to Sales inquiries
CREATE TABLE IF NOT EXISTS sales_leads (
    id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    name             TEXT        NOT NULL,
    email            TEXT        NOT NULL,
    phone_number     TEXT        NOT NULL,
    company_name     TEXT,
    use_case         TEXT        NOT NULL,
    call_volume      TEXT        NOT NULL,
    notes            TEXT,
    status           TEXT        NOT NULL DEFAULT 'new',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sales_leads_email ON sales_leads(email);
CREATE INDEX IF NOT EXISTS idx_sales_leads_created_at ON sales_leads(created_at DESC);

-- Enable RLS
ALTER TABLE sales_leads ENABLE ROW LEVEL SECURITY;

-- Allow inserts
CREATE POLICY "Allow public insert to sales_leads"
    ON sales_leads FOR INSERT
    TO anon, authenticated
    WITH CHECK (true);

-- Allow service role full access
CREATE POLICY "Allow service role full access to sales_leads"
    ON sales_leads FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);
