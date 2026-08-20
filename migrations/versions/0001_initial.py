"""Initial schema: tenants, identity, RLS, and the unprivileged app role.

Revision ID: 0001
Revises:
"""
import os
import pathlib

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA = pathlib.Path(__file__).resolve().parents[2] / "schema.sql"

APP_ROLE = "recur_app"


def upgrade() -> None:
    # The first revision executes schema.sql rather than restating it. There is
    # no SQLAlchemy model layer here, so autogenerate was never available, and
    # two hand-maintained copies of the same DDL is how they drift apart.
    # Revisions from 0002 onward are written by hand as real ALTERs.
    op.execute(SCHEMA.read_text())

    password = os.environ.get("RECUR_APP_PASSWORD", "recur_app_local_dev")
    op.execute(
        f"""
        DO $role$ BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                CREATE ROLE {APP_ROLE} LOGIN;
            END IF;
        END $role$;
        """
    )
    # NOSUPERUSER NOBYPASSRLS is what makes every policy in schema.sql real.
    # A superuser ignores row-level security entirely.
    op.execute(
        f"ALTER ROLE {APP_ROLE} WITH LOGIN NOSUPERUSER NOBYPASSRLS "
        f"NOCREATEDB NOCREATEROLE PASSWORD {_literal(password)}"
    )
    for stmt in (
        f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}",
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}",
        f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}",
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}",
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT USAGE, SELECT ON SEQUENCES TO {APP_ROLE}",
    ):
        op.execute(stmt)


def downgrade() -> None:
    # Deliberately not implemented. "Downgrade" here means dropping every
    # table that holds a user's financial history; a mistyped command should
    # not be able to do that.
    raise NotImplementedError(
        "Downgrading the initial revision would drop all user data. "
        "Restore from a backup instead."
    )


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
