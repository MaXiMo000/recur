"""Carry the currency on each subscription, so totals are never summed across them.

Revision ID: 0003
Revises: 0002
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Without this, /api/summary added a JPY subscription to a USD one and
    # reported the result as a single figure -- not an approximation, a
    # meaningless number presented as a fact.
    op.execute("ALTER TABLE subscription "
               "ADD COLUMN IF NOT EXISTS currency TEXT NOT NULL DEFAULT 'USD'")
    # Backfill from the transactions each subscription was derived from.
    op.execute("""
        UPDATE subscription s SET currency = t.currency
        FROM (SELECT DISTINCT ON (merchant_id, account_id)
                     merchant_id, account_id, currency
              FROM raw_transaction) t
        WHERE s.merchant_id = t.merchant_id AND s.account_id = t.account_id
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE subscription DROP COLUMN IF EXISTS currency")
