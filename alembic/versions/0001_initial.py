"""Initial production schema."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "schemas",
        sa.Column("schema_hash", sa.String(length=64), primary_key=True),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("schema_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "meeting_cards",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("week_start_date", sa.String(length=10), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column(
            "schema_hash",
            sa.String(length=64),
            sa.ForeignKey("schemas.schema_hash"),
            nullable=False,
        ),
        sa.Column("data_json", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("week_start_date", name="uq_meeting_cards_week_start"),
    )
    op.create_index("ix_meeting_cards_archived", "meeting_cards", ["archived_at"])
    op.create_table(
        "users",
        sa.Column("telegram_user_id", sa.BigInteger(), primary_key=True),
        sa.Column("username", sa.String(length=255)),
        sa.Column("full_name", sa.String(length=512)),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("approved_by", sa.BigInteger()),
        sa.Column("approved_at", sa.DateTime()),
    )
    op.create_table(
        "chats",
        sa.Column("chat_id", sa.BigInteger(), primary_key=True),
        sa.Column("chat_type", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=512)),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("read_only", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "pending_changes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("created_by_user_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("week_start_date", sa.String(length=10), nullable=False),
        sa.Column("precondition_revision", sa.Integer(), nullable=False),
        sa.Column("patch_json", sa.Text(), nullable=False),
        sa.Column("preview_text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("telegram_message_id", sa.BigInteger()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("resolved_at", sa.DateTime()),
    )
    op.create_index(
        "ix_pending_creator_status",
        "pending_changes",
        ["created_by_user_id", "status"],
    )
    op.create_index("ix_pending_week_status", "pending_changes", ["week_start_date", "status"])
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("actor_user_id", sa.BigInteger()),
        sa.Column("chat_id", sa.BigInteger()),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=32)),
        sa.Column("target_id", sa.String(length=128)),
        sa.Column("details_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_audit_created_at", "audit_log", ["created_at"])
    op.create_table(
        "notification_state",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("card_id", sa.Integer(), sa.ForeignKey("meeting_cards.id"), nullable=False),
        sa.Column("block_id", sa.String(length=128), nullable=False),
        sa.Column("entry_key", sa.String(length=64), nullable=False),
        sa.Column("field_id", sa.String(length=128), nullable=False),
        sa.Column("deadline_at", sa.DateTime(), nullable=False),
        sa.Column("reminder_kind", sa.String(length=32), nullable=False),
        sa.Column("sent_to_user_id", sa.BigInteger(), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "card_id",
            "block_id",
            "entry_key",
            "field_id",
            "deadline_at",
            "reminder_kind",
            "sent_to_user_id",
            name="uq_notification_dedup",
        ),
    )
    op.create_table(
        "clarification_sessions",
        sa.Column("user_id", sa.BigInteger(), primary_key=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("original_text", sa.Text(), nullable=False),
        sa.Column("context_json", sa.Text(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_clarification_expiry", "clarification_sessions", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_clarification_expiry", table_name="clarification_sessions")
    op.drop_table("clarification_sessions")
    op.drop_table("notification_state")
    op.drop_index("ix_audit_created_at", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_index("ix_pending_week_status", table_name="pending_changes")
    op.drop_index("ix_pending_creator_status", table_name="pending_changes")
    op.drop_table("pending_changes")
    op.drop_table("chats")
    op.drop_table("users")
    op.drop_index("ix_meeting_cards_archived", table_name="meeting_cards")
    op.drop_table("meeting_cards")
    op.drop_table("schemas")
