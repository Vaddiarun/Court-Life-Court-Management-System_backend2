"""voice_messages: scripts read back by call flows (Exotel Greeting applet)

Revision ID: a3f1c9e2b7d4
Revises: 89d2347b3133
Create Date: 2026-11-18
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "a3f1c9e2b7d4"
down_revision = "89d2347b3133"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "voice_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("call_sid", sa.String(length=100), nullable=True),
        sa.Column("language", sa.String(length=10), nullable=False, server_default="en"),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_voice_messages_call_sid", "voice_messages", ["call_sid"])


def downgrade() -> None:
    op.drop_index("ix_voice_messages_call_sid", table_name="voice_messages")
    op.drop_table("voice_messages")
