"""OAuth 2.1 authorization server tables, for the remote MCP endpoint.

Revision ID: 0002
Revises: 0001
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Public clients only. An MCP client is a desktop app or a CLI: it cannot
    # keep a secret, so there is no client_secret column to leak. PKCE is what
    # binds the authorization code to the client instead, and it is mandatory
    # rather than optional -- see the CHECK below.
    op.execute("""
        CREATE TABLE IF NOT EXISTS oauth_client (
            client_id     TEXT PRIMARY KEY,
            client_name   TEXT NOT NULL,
            redirect_uris TEXT[] NOT NULL,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)

    # Authorization codes: single use, short lived, and stored only as a hash.
    op.execute("""
        CREATE TABLE IF NOT EXISTS oauth_code (
            code_hash             TEXT PRIMARY KEY,
            client_id             TEXT NOT NULL
                                  REFERENCES oauth_client(client_id) ON DELETE CASCADE,
            user_id               BIGINT NOT NULL
                                  REFERENCES app_user(id) ON DELETE CASCADE,
            redirect_uri          TEXT NOT NULL,
            code_challenge        TEXT NOT NULL,
            code_challenge_method TEXT NOT NULL
                                  CHECK (code_challenge_method = 'S256'),
            scope                 TEXT NOT NULL,
            resource              TEXT,
            expires_at            TIMESTAMPTZ NOT NULL
        )
    """)

    # Access tokens carry an audience. A token minted for this server must not
    # be replayable against a different one -- that is the confused-deputy
    # problem the MCP auth spec exists to close.
    op.execute("""
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
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_oauth_token_user "
               "ON oauth_token (user_id) WHERE revoked_at IS NULL")
    op.execute("CREATE INDEX IF NOT EXISTS idx_oauth_code_expiry "
               "ON oauth_code (expires_at)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS oauth_token")
    op.execute("DROP TABLE IF EXISTS oauth_code")
    op.execute("DROP TABLE IF EXISTS oauth_client")
