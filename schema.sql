-- Week 1 tables only. subscription / price_change / resolution_queue /
-- merchant_embedding land in weeks 2-3, when something actually writes to them.

CREATE TABLE IF NOT EXISTS account (
    id          SERIAL PRIMARY KEY,
    label       TEXT NOT NULL UNIQUE,
    institution TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS merchant (
    id             SERIAL PRIMARY KEY,
    canonical_name TEXT NOT NULL UNIQUE,
    category       TEXT
);

-- The deterministic layer. Every resolution (fuzzy, vector, LLM, human) writes a
-- row here, so tier 1 grows and the expensive tiers shrink. Week 2 fills it.
CREATE TABLE IF NOT EXISTS merchant_alias (
    id               SERIAL PRIMARY KEY,
    scrubbed_pattern TEXT NOT NULL UNIQUE,
    merchant_id      INTEGER NOT NULL REFERENCES merchant(id) ON DELETE CASCADE,
    resolved_by      TEXT NOT NULL
                     CHECK (resolved_by IN ('exact','fuzzy','vector','llm','human')),
    confidence       NUMERIC(4,3),
    resolved_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS raw_transaction (
    id             BIGSERIAL PRIMARY KEY,
    account_id     INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    posted_date    DATE NOT NULL,
    -- Integer cents. Never float. Negative = money left the account.
    amount_cents   BIGINT NOT NULL,
    currency       TEXT NOT NULL DEFAULT 'USD',
    raw_descriptor TEXT NOT NULL,
    scrubbed       TEXT NOT NULL,
    merchant_id    INTEGER REFERENCES merchant(id) ON DELETE SET NULL,
    source_file    TEXT,
    -- Re-uploading the same statement is a no-op; two genuine same-day identical
    -- charges still both land (the hash includes an occurrence index).
    dedup_hash     TEXT NOT NULL UNIQUE,
    ingested_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_txn_scrubbed ON raw_transaction (scrubbed);
CREATE INDEX IF NOT EXISTS idx_txn_merchant_date
    ON raw_transaction (merchant_id, posted_date);

-- ---------------------------------------------------------------- week 2 --

-- Anything the deterministic tiers won't commit to. A human resolution here
-- writes a merchant_alias row, so the same string is never asked about twice.
CREATE TABLE IF NOT EXISTS resolution_queue (
    id             SERIAL PRIMARY KEY,
    scrubbed       TEXT NOT NULL UNIQUE,
    txn_count      INTEGER NOT NULL DEFAULT 1,
    candidates     JSONB NOT NULL DEFAULT '[]',
    top_score      NUMERIC(5,2),
    reason         TEXT,
    status         TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending','resolved','ignored')),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_queue_pending
    ON resolution_queue (status) WHERE status = 'pending';
