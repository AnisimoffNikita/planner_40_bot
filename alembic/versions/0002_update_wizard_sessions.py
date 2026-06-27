"""Add update wizard sessions."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_update_wizard_sessions"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "update_wizard_sessions",
        sa.Column("user_id", sa.BigInteger(), primary_key=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(length=64), nullable=False),
        sa.Column("context_json", sa.Text(), nullable=False),
        sa.Column("options_json", sa.Text(), nullable=False),
        sa.Column("message_id", sa.BigInteger()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_update_wizard_expiry", "update_wizard_sessions", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_update_wizard_expiry", table_name="update_wizard_sessions")
    op.drop_table("update_wizard_sessions")
