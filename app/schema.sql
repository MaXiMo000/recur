-- Recur: multi-tenant schema.
--
-- Tenant isolation is enforced by Postgres row-level security, not by
-- remembering a WHERE clause. Every data table carries user_id, every one has a
-- policy keyed on `recur.user_id`, and FORCE is set so the policies apply to the
-- table owner too -- otherwise the application role, which owns these tables,
-- would silently bypass every one of them.
--
-- The consequence worth stating plainly: a query that forgets its tenant filter
-- returns zero rows. It cannot return someone else's.

CREATE EXTENSION IF NOT EXISTS citext;

-- --------------------------------------------------------------- identity --

CREATE TABLE IF NOT EXISTS app_user (
    id                BIGSERIAL PRIMARY KEY,
    email             CITEXT NOT NULL UNIQUE,
    -- argon2id. Never a raw password, never a reversible transform.
    password_hash     TEXT NOT NULL,
    email_verified_at TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    disabled_at       TIMESTAMPTZ
);

-- Only the SHA-256 of a session token is stored, so a database dump does not
-- hand the reader a working set of live sessions.
CREATE TABLE IF NOT EXISTS session (
    token_hash   TEXT PRIMARY KEY,
    user_id      BIGINT NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL,
    user_agent   TEXT
);
CREATE INDEX IF NOT EXISTS idx_session_user ON session (user_id);
CREATE INDEX IF NOT EXISTS idx_session_expiry ON session (expires_at);

-- Email verification and password reset: same table, same single-use rule.
CREATE TABLE IF NOT EXISTS email_token (
    token_hash TEXT PRIMARY KEY,
    user_id    BIGINT NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    purpose    TEXT NOT NULL CHECK (purpose IN ('verify', 'reset')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    used_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_email_token_user ON email_token (user_id, purpose);

-- ------------------------------------------------------------------- data --

CREATE TABLE IF NOT EXISTS account (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    label       TEXT NOT NULL,
    institution TEXT,
    currency    TEXT NOT NULL DEFAULT 'USD',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, label)
);

-- Merchants are per-tenant. Two users both having "NETFLIX" is ordinary, and a
-- globally unique canonical_name would have made one of them fail to insert --
-- or worse, quietly share a row.
CREATE TABLE IF NOT EXISTS merchant (
    id             BIGSERIAL PRIMARY KEY,
    user_id        BIGINT NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    canonical_name TEXT NOT NULL,
    category       TEXT,
    UNIQUE (user_id, canonical_name)
);

CREATE TABLE IF NOT EXISTS merchant_alias (
    id               BIGSERIAL PRIMARY KEY,
    user_id          BIGINT NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    scrubbed_pattern TEXT NOT NULL,
    merchant_id      BIGINT NOT NULL REFERENCES merchant(id) ON DELETE CASCADE,
    resolved_by      TEXT NOT NULL
                     CHECK (resolved_by IN ('exact','fuzzy','vector','llm','human')),
    confidence       NUMERIC(4,3),
    resolved_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, scrubbed_pattern)
);

CREATE TABLE IF NOT EXISTS raw_transaction (
    id             BIGSERIAL PRIMARY KEY,
    user_id        BIGINT NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    account_id     BIGINT NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    posted_date    DATE NOT NULL,
    -- Integer cents. Never float.
    amount_cents   BIGINT NOT NULL,
    currency       TEXT NOT NULL DEFAULT 'USD',
    raw_descriptor TEXT NOT NULL,
    scrubbed       TEXT NOT NULL,
    merchant_id    BIGINT REFERENCES merchant(id) ON DELETE SET NULL,
    source_file    TEXT,
    dedup_hash     TEXT NOT NULL,
    ingested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Scoped per user: two tenants uploading the same statement must not
    -- collide, and re-uploading your own must still be a no-op.
    UNIQUE (user_id, dedup_hash)
);
CREATE INDEX IF NOT EXISTS idx_txn_scrubbed ON raw_transaction (user_id, scrubbed);
CREATE INDEX IF NOT EXISTS idx_txn_merchant_date
    ON raw_transaction (user_id, merchant_id, posted_date);

CREATE TABLE IF NOT EXISTS resolution_queue (
    id         BIGSERIAL PRIMARY KEY,
    user_id    BIGINT NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    scrubbed   TEXT NOT NULL,
    txn_count  INTEGER NOT NULL DEFAULT 1,
    candidates JSONB NOT NULL DEFAULT '[]',
    top_score  NUMERIC(5,2),
    reason     TEXT,
    status     TEXT NOT NULL DEFAULT 'pending'
               CHECK (status IN ('pending','resolved','ignored')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, scrubbed)
);
CREATE INDEX IF NOT EXISTS idx_queue_pending
    ON resolution_queue (user_id, status) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS subscription (
    id                   BIGSERIAL PRIMARY KEY,
    user_id              BIGINT NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    merchant_id          BIGINT NOT NULL REFERENCES merchant(id) ON DELETE CASCADE,
    account_id           BIGINT NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    cadence              TEXT NOT NULL,
    period_days          NUMERIC(7,2) NOT NULL,
    anchor_day           SMALLINT,
    current_amount_cents BIGINT NOT NULL,
    amount_cv            NUMERIC(5,3) NOT NULL,
    charge_count         INTEGER NOT NULL,
    first_seen           DATE NOT NULL,
    last_seen            DATE NOT NULL,
    next_due             DATE,
    status               TEXT NOT NULL
                         CHECK (status IN ('active','lapsed','cancelled')),
    confidence           NUMERIC(4,3) NOT NULL,
    UNIQUE (user_id, merchant_id, account_id)
);

CREATE TABLE IF NOT EXISTS price_change (
    id               BIGSERIAL PRIMARY KEY,
    user_id          BIGINT NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    subscription_id  BIGINT NOT NULL REFERENCES subscription(id) ON DELETE CASCADE,
    effective_date   DATE NOT NULL,
    old_amount_cents BIGINT NOT NULL,
    new_amount_cents BIGINT NOT NULL,
    pct_change       NUMERIC(6,2) NOT NULL,
    UNIQUE (subscription_id, effective_date)
);

-- ---------------------------------------------------------- rate limiting --

CREATE TABLE IF NOT EXISTS auth_attempt (
    id   BIGSERIAL PRIMARY KEY,
    key  TEXT NOT NULL,          -- email or client IP. Never a password.
    kind TEXT NOT NULL,
    at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_auth_attempt ON auth_attempt (key, kind, at DESC);


-- ----------------------------------------------------------------- oauth --
--
-- These live here as well as in migration 0002 on purpose. schema.sql is the
-- *current* schema, used to bootstrap an empty database; migrations move an
-- existing one forward. Keeping oauth only in 0002 meant a fresh database --
-- a CI runner, a new deploy -- came up without these tables and crashed on
-- startup, which is what CI caught on its first run.

CREATE TABLE IF NOT EXISTS oauth_client (
    client_id     TEXT PRIMARY KEY,
    client_name   TEXT NOT NULL,
    redirect_uris TEXT[] NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Public clients only: an MCP client is a desktop app and cannot keep a
-- secret, so there is no client_secret to leak. PKCE binds the code instead,
-- and S256 is enforced by the CHECK rather than by convention.
CREATE TABLE IF NOT EXISTS oauth_code (
    code_hash             TEXT PRIMARY KEY,
    client_id             TEXT NOT NULL
                          REFERENCES oauth_client(client_id) ON DELETE CASCADE,
    user_id               BIGINT NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    redirect_uri          TEXT NOT NULL,
    code_challenge        TEXT NOT NULL,
    code_challenge_method TEXT NOT NULL CHECK (code_challenge_method = 'S256'),
    scope                 TEXT NOT NULL,
    resource              TEXT,
    expires_at            TIMESTAMPTZ NOT NULL
);

-- Tokens carry an audience: one minted here must not be replayable against a
-- different server. That is the confused-deputy problem the MCP auth spec
-- exists to close.
CREATE TABLE IF NOT EXISTS oauth_token (
    token_hash   TEXT PRIMARY KEY,
    client_id    TEXT NOT NULL,
    user_id      BIGINT NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    scope        TEXT NOT NULL,
    audience     TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL,
    last_used_at TIMESTAMPTZ,
    revoked_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_oauth_token_user
    ON oauth_token (user_id) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_oauth_code_expiry ON oauth_code (expires_at);

-- ------------------------------------------------------------------- RLS --
--
-- The application does NOT connect as the role that owns these tables.
--
-- A superuser bypasses row-level security unconditionally -- FORCE only binds
-- the table owner, and nothing binds a superuser. The Postgres Docker image
-- makes POSTGRES_USER a superuser and managed providers hand out a privileged
-- role by default, so connecting the app as the obvious user leaves every
-- policy below inert while pg_class still cheerfully reports
-- relrowsecurity = true. NOBYPASSRLS on a dedicated login role is what actually
-- turns the policies on.

-- One definition of "who is the current tenant", rather than the same
-- expression escaped through two layers of format() in seven policies.
--
-- NULLIF is load-bearing. RESET leaves the setting as an empty string rather
-- than NULL, and ''::bigint raises rather than matching nothing -- and a policy
-- that errors is not a policy that denies, it is an outage. Returning NULL
-- makes every comparison false, which is the correct closed default.
CREATE OR REPLACE FUNCTION recur_current_user_id() RETURNS BIGINT
LANGUAGE sql STABLE AS $fn$
    SELECT NULLIF(current_setting('recur.user_id', true), '')::BIGINT
$fn$;

DO $rls$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'account', 'merchant', 'merchant_alias', 'raw_transaction',
        'resolution_queue', 'subscription', 'price_change'
    ] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        -- FORCE binds the table owner as well. It does not bind a superuser --
        -- nothing does -- which is why the application connects as a
        -- NOBYPASSRLS role instead.
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I'
            '  USING (user_id = recur_current_user_id())'
            '  WITH CHECK (user_id = recur_current_user_id())', t);
    END LOOP;
END
$rls$;
