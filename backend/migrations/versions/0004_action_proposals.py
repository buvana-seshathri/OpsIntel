"""action proposals

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-01 01:02:47.204493
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "action_proposals",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("target", sa.String(length=128), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("evidence_ids", postgresql.ARRAY(sa.String(length=128)), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("proposed_by", sa.String(length=64), nullable=False),
        sa.Column("proposed_role", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_by", sa.String(length=64), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_action_proposals")),
    )


def downgrade() -> None:
    op.drop_table("action_proposals")
