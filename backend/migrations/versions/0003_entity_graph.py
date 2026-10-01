"""entity graph

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-01 00:46:58.570349
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "entities",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("properties", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_entities")),
    )
    op.create_index(op.f("ix_entities_type"), "entities", ["type"], unique=False)
    op.create_table(
        "edges",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("src", sa.String(length=128), nullable=False),
        sa.Column("dst", sa.String(length=128), nullable=False),
        sa.Column("relation", sa.String(length=32), nullable=False),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("properties", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["dst"], ["entities.id"], name=op.f("fk_edges_dst_entities"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["src"], ["entities.id"], name=op.f("fk_edges_src_entities"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_edges")),
    )
    op.create_index("ix_edges_dst_relation", "edges", ["dst", "relation"], unique=False)
    op.create_index("ix_edges_src_relation", "edges", ["src", "relation"], unique=False)
    op.create_index("uq_edges_src_dst_relation", "edges", ["src", "dst", "relation"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_edges_src_dst_relation", table_name="edges")
    op.drop_index("ix_edges_src_relation", table_name="edges")
    op.drop_index("ix_edges_dst_relation", table_name="edges")
    op.drop_table("edges")
    op.drop_index(op.f("ix_entities_type"), table_name="entities")
    op.drop_table("entities")
