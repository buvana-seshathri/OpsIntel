"""audit log and approvals

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-01 01:53:40.702534
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("seq", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("investigation_id", sa.String(length=32), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("prev_hash", sa.String(length=64), nullable=False),
        sa.Column("hash", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("seq", name=op.f("pk_audit_log")),
        sa.UniqueConstraint("hash", name=op.f("uq_audit_log_hash")),
    )
    op.create_index(
        op.f("ix_audit_log_investigation_id"), "audit_log", ["investigation_id"], unique=False
    )
    op.add_column("action_proposals", sa.Column("decision_comment", sa.Text(), nullable=True))
    op.add_column(
        "action_proposals", sa.Column("investigation_id", sa.String(length=32), nullable=True)
    )
    op.add_column(
        "action_proposals", sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("action_proposals", sa.Column("execution_result", sa.Text(), nullable=True))
    # Append-only: refuse UPDATE, DELETE and TRUNCATE at the database level, so even code
    # with a bug (or an attacker with app credentials) cannot rewrite history quietly.
    op.execute("""
        CREATE FUNCTION audit_log_append_only() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_log is append-only (% refused)', TG_OP;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER audit_log_no_update_delete
        BEFORE UPDATE OR DELETE ON audit_log
        FOR EACH ROW EXECUTE FUNCTION audit_log_append_only()
    """)
    op.execute("""
        CREATE TRIGGER audit_log_no_truncate
        BEFORE TRUNCATE ON audit_log
        FOR EACH STATEMENT EXECUTE FUNCTION audit_log_append_only()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_truncate ON audit_log")
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_update_delete ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS audit_log_append_only()")
    op.drop_column("action_proposals", "execution_result")
    op.drop_column("action_proposals", "executed_at")
    op.drop_column("action_proposals", "investigation_id")
    op.drop_column("action_proposals", "decision_comment")
    op.drop_index(op.f("ix_audit_log_investigation_id"), table_name="audit_log")
    op.drop_table("audit_log")
