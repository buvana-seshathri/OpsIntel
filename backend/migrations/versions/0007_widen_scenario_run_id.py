"""widen scenario run id

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-01 03:30:26.364577
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "scenario_runs",
        "id",
        existing_type=sa.VARCHAR(length=32),
        type_=sa.String(length=96),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "scenario_runs",
        "id",
        existing_type=sa.String(length=96),
        type_=sa.VARCHAR(length=32),
        existing_nullable=False,
    )
