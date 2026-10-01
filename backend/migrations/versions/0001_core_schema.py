"""core schema

Revision ID: 0001
Revises:
Create Date: 2026-10-01 00:30:19.695496
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # pgvector backs document embeddings (phase 2); enable it up front so every
    # environment proves it has the extension from the first migration.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "customers",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("email", sa.String(length=256), nullable=False),
        sa.Column("phone", sa.String(length=32), nullable=False),
        sa.Column("tier", sa.String(length=16), nullable=False),
        sa.Column("region", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_customers")),
    )
    op.create_table(
        "scenario_runs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("scenario_key", sa.String(length=64), nullable=False),
        sa.Column("seed", sa.Integer(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ground_truth", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scenario_runs")),
    )
    op.create_table(
        "teams",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("slack_channel", sa.String(length=64), nullable=False),
        sa.Column("oncall_handle", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_teams")),
    )
    op.create_table(
        "orders",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("customer_id", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("total_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("failure_reason", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["customer_id"], ["customers.id"], name=op.f("fk_orders_customer_id_customers")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_orders")),
    )
    op.create_index(op.f("ix_orders_created_at"), "orders", ["created_at"], unique=False)
    op.create_index(op.f("ix_orders_customer_id"), "orders", ["customer_id"], unique=False)
    op.create_table(
        "services",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("tier", sa.Integer(), nullable=False),
        sa.Column("owner_team_id", sa.String(length=64), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["owner_team_id"], ["teams.id"], name=op.f("fk_services_owner_team_id_teams")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_services")),
    )
    op.create_table(
        "config_changes",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("service_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=False),
        sa.Column("new_value", sa.Text(), nullable=False),
        sa.Column("changed_by", sa.String(length=64), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["service_id"], ["services.id"], name=op.f("fk_config_changes_service_id_services")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_config_changes")),
    )
    op.create_index(
        op.f("ix_config_changes_changed_at"), "config_changes", ["changed_at"], unique=False
    )
    op.create_index(
        op.f("ix_config_changes_service_id"), "config_changes", ["service_id"], unique=False
    )
    op.create_table(
        "deploys",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("service_id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("previous_version", sa.String(length=32), nullable=False),
        sa.Column("commit_sha", sa.String(length=40), nullable=False),
        sa.Column("author", sa.String(length=64), nullable=False),
        sa.Column("change_summary", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("deployed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["service_id"], ["services.id"], name=op.f("fk_deploys_service_id_services")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deploys")),
    )
    op.create_index(op.f("ix_deploys_deployed_at"), "deploys", ["deployed_at"], unique=False)
    op.create_index(op.f("ix_deploys_service_id"), "deploys", ["service_id"], unique=False)
    op.create_table(
        "hosts",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("service_id", sa.String(length=64), nullable=False),
        sa.Column("region", sa.String(length=32), nullable=False),
        sa.Column("az", sa.String(length=32), nullable=False),
        sa.Column("instance_type", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(
            ["service_id"], ["services.id"], name=op.f("fk_hosts_service_id_services")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_hosts")),
    )
    op.create_index(op.f("ix_hosts_service_id"), "hosts", ["service_id"], unique=False)
    op.create_table(
        "metric_points",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("service_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["service_id"], ["services.id"], name=op.f("fk_metric_points_service_id_services")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_metric_points")),
    )
    op.create_index(
        "ix_metric_points_series", "metric_points", ["service_id", "name", "ts"], unique=False
    )
    op.create_table(
        "payments",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("order_id", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name=op.f("fk_payments_order_id_orders")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payments")),
    )
    op.create_index(op.f("ix_payments_created_at"), "payments", ["created_at"], unique=False)
    op.create_index(op.f("ix_payments_order_id"), "payments", ["order_id"], unique=False)
    op.create_table(
        "service_dependencies",
        sa.Column("caller_id", sa.String(length=64), nullable=False),
        sa.Column("callee_id", sa.String(length=64), nullable=False),
        sa.Column("criticality", sa.String(length=16), nullable=False),
        sa.ForeignKeyConstraint(
            ["callee_id"], ["services.id"], name=op.f("fk_service_dependencies_callee_id_services")
        ),
        sa.ForeignKeyConstraint(
            ["caller_id"], ["services.id"], name=op.f("fk_service_dependencies_caller_id_services")
        ),
        sa.PrimaryKeyConstraint("caller_id", "callee_id", name=op.f("pk_service_dependencies")),
    )
    op.create_table(
        "shipments",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("order_id", sa.String(length=32), nullable=False),
        sa.Column("carrier", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name=op.f("fk_shipments_order_id_orders")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shipments")),
    )
    op.create_index(op.f("ix_shipments_order_id"), "shipments", ["order_id"], unique=False)
    op.create_table(
        "events",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("service_id", sa.String(length=64), nullable=False),
        sa.Column("host_id", sa.String(length=64), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("attributes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(["host_id"], ["hosts.id"], name=op.f("fk_events_host_id_hosts")),
        sa.ForeignKeyConstraint(
            ["service_id"], ["services.id"], name=op.f("fk_events_service_id_services")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_events")),
    )
    op.create_index("ix_events_service_ts", "events", ["service_id", "ts"], unique=False)
    op.create_index(op.f("ix_events_ts"), "events", ["ts"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_events_ts"), table_name="events")
    op.drop_index("ix_events_service_ts", table_name="events")
    op.drop_table("events")
    op.drop_index(op.f("ix_shipments_order_id"), table_name="shipments")
    op.drop_table("shipments")
    op.drop_table("service_dependencies")
    op.drop_index(op.f("ix_payments_order_id"), table_name="payments")
    op.drop_index(op.f("ix_payments_created_at"), table_name="payments")
    op.drop_table("payments")
    op.drop_index("ix_metric_points_series", table_name="metric_points")
    op.drop_table("metric_points")
    op.drop_index(op.f("ix_hosts_service_id"), table_name="hosts")
    op.drop_table("hosts")
    op.drop_index(op.f("ix_deploys_service_id"), table_name="deploys")
    op.drop_index(op.f("ix_deploys_deployed_at"), table_name="deploys")
    op.drop_table("deploys")
    op.drop_index(op.f("ix_config_changes_service_id"), table_name="config_changes")
    op.drop_index(op.f("ix_config_changes_changed_at"), table_name="config_changes")
    op.drop_table("config_changes")
    op.drop_table("services")
    op.drop_index(op.f("ix_orders_customer_id"), table_name="orders")
    op.drop_index(op.f("ix_orders_created_at"), table_name="orders")
    op.drop_table("orders")
    op.drop_table("teams")
    op.drop_table("scenario_runs")
    op.drop_table("customers")
