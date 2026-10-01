"""Relational model for the simulated e-commerce company.

Every row the agent can cite has a human-readable, prefixed text ID (evt_..., dep_...,
ord_...). The prefix tells the grounding checker which table a citation points to.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

EMBEDDING_DIM = 384

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)
    type_annotation_map = {datetime: DateTime(timezone=True), dict[str, Any]: JSONB}


# --- Org and infrastructure -------------------------------------------------------


class Team(Base):
    __tablename__ = "teams"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    slack_channel: Mapped[str] = mapped_column(String(64))
    oncall_handle: Mapped[str] = mapped_column(String(64))


class Service(Base):
    __tablename__ = "services"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # e.g. "payments-svc"
    kind: Mapped[str] = mapped_column(String(16))  # internal | database | external
    tier: Mapped[int] = mapped_column(Integer)  # 1 = revenue critical
    owner_team_id: Mapped[str | None] = mapped_column(ForeignKey("teams.id"))
    description: Mapped[str] = mapped_column(Text)


class ServiceDependency(Base):
    """caller -> callee. Phase 3 lifts these into the general entity graph."""

    __tablename__ = "service_dependencies"
    caller_id: Mapped[str] = mapped_column(ForeignKey("services.id"), primary_key=True)
    callee_id: Mapped[str] = mapped_column(ForeignKey("services.id"), primary_key=True)
    criticality: Mapped[str] = mapped_column(String(16))  # hard | soft


class Host(Base):
    __tablename__ = "hosts"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    service_id: Mapped[str] = mapped_column(ForeignKey("services.id"), index=True)
    region: Mapped[str] = mapped_column(String(32))
    az: Mapped[str] = mapped_column(String(32))
    instance_type: Mapped[str] = mapped_column(String(32))


# --- Changes ----------------------------------------------------------------------


class Deploy(Base):
    __tablename__ = "deploys"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    service_id: Mapped[str] = mapped_column(ForeignKey("services.id"), index=True)
    version: Mapped[str] = mapped_column(String(32))
    previous_version: Mapped[str] = mapped_column(String(32))
    commit_sha: Mapped[str] = mapped_column(String(40))
    author: Mapped[str] = mapped_column(String(64))
    change_summary: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16))  # succeeded | rolled_back | failed
    deployed_at: Mapped[datetime] = mapped_column(index=True)


class ConfigChange(Base):
    __tablename__ = "config_changes"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    service_id: Mapped[str] = mapped_column(ForeignKey("services.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # feature_flag | config | infra
    key: Mapped[str] = mapped_column(String(128))
    old_value: Mapped[str] = mapped_column(Text)
    new_value: Mapped[str] = mapped_column(Text)
    changed_by: Mapped[str] = mapped_column(String(64))
    changed_at: Mapped[datetime] = mapped_column(index=True)


# --- Business data (customers are PII) ---------------------------------------------


class Customer(Base):
    __tablename__ = "customers"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    email: Mapped[str] = mapped_column(String(256))
    phone: Mapped[str] = mapped_column(String(32))
    tier: Mapped[str] = mapped_column(String(16))  # standard | plus | enterprise
    region: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime]


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    status: Mapped[str] = mapped_column(String(16))  # completed | failed | pending
    total_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    failure_reason: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(index=True)


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    amount_cents: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))  # captured | declined | error
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(index=True)


class Shipment(Base):
    __tablename__ = "shipments"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    carrier: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))  # label_created | error
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime]


# --- Telemetry --------------------------------------------------------------------


class Event(Base):
    """Logs, alerts and change notifications on one timeline."""

    __tablename__ = "events"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    ts: Mapped[datetime] = mapped_column(index=True)
    service_id: Mapped[str] = mapped_column(ForeignKey("services.id"))
    host_id: Mapped[str | None] = mapped_column(ForeignKey("hosts.id"))
    kind: Mapped[str] = mapped_column(String(16))  # log | alert | deploy | config_change
    severity: Mapped[str] = mapped_column(String(16))  # info | warning | error | critical
    message: Mapped[str] = mapped_column(Text)
    attributes: Mapped[dict[str, Any]] = mapped_column(default=dict)

    __table_args__ = (Index("ix_events_service_ts", "service_id", "ts"),)


class MetricPoint(Base):
    __tablename__ = "metric_points"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    service_id: Mapped[str] = mapped_column(ForeignKey("services.id"))
    name: Mapped[str] = mapped_column(String(64))  # request_rate | error_rate | p99_latency_ms
    ts: Mapped[datetime]
    value: Mapped[float] = mapped_column(Float)

    __table_args__ = (Index("ix_metric_points_series", "service_id", "name", "ts"),)


# --- Eval ground truth (never exposed through MCP tools) ---------------------------


class ScenarioRun(Base):
    __tablename__ = "scenario_runs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    scenario_key: Mapped[str] = mapped_column(String(64))
    seed: Mapped[int] = mapped_column(Integer)
    window_start: Mapped[datetime]
    window_end: Mapped[datetime]
    ground_truth: Mapped[dict[str, Any]]


# --- Documents (runbooks, postmortems, policies) -------------------------------------


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[str] = mapped_column(String(96), primary_key=True)  # doc_<slug>
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    title: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(16))  # runbook | postmortem | policy
    services: Mapped[list[str]] = mapped_column(ARRAY(String(64)))
    acl_roles: Mapped[list[str]] = mapped_column(ARRAY(String(32)))
    content_hash: Mapped[str] = mapped_column(String(64))
    embedding_model: Mapped[str] = mapped_column(String(128))
    chunks: Mapped[list[Chunk]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )


class Chunk(Base):
    __tablename__ = "chunks"
    id: Mapped[str] = mapped_column(String(112), primary_key=True)  # chk_<slug>_<nn>
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    heading: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))
    tsv: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', heading || ' ' || content)", persisted=True),
    )
    document: Mapped[Document] = relationship(back_populates="chunks")

    __table_args__ = (
        Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
        Index(
            "ix_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


# --- Entity graph ---------------------------------------------------------------------


class Entity(Base):
    """A typed node. IDs are "<type>:<natural id>" for infrastructure (service:payments-svc,
    host:payments-svc-02, error_code:ENOSPC) and the row ID for records (dep_..., ord_...)."""

    __tablename__ = "entities"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    type: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(Text)
    properties: Mapped[dict[str, Any]] = mapped_column(default=dict)


class Edge(Base):
    __tablename__ = "edges"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    src: Mapped[str] = mapped_column(ForeignKey("entities.id", ondelete="CASCADE"))
    dst: Mapped[str] = mapped_column(ForeignKey("entities.id", ondelete="CASCADE"))
    relation: Mapped[str] = mapped_column(String(32))
    origin: Mapped[str] = mapped_column(String(16))  # record | event | document
    properties: Mapped[dict[str, Any]] = mapped_column(default=dict)

    __table_args__ = (
        Index("ix_edges_src_relation", "src", "relation"),
        Index("ix_edges_dst_relation", "dst", "relation"),
        Index("uq_edges_src_dst_relation", "src", "dst", "relation", unique=True),
    )


# --- Human-in-the-loop actions ----------------------------------------------------------


class ActionProposal(Base):
    """A state-changing action the agent recommends. Nothing executes until a human with
    `decide:action` approves it (phase 6)."""

    __tablename__ = "action_proposals"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    type: Mapped[str] = mapped_column(String(32))
    target: Mapped[str] = mapped_column(String(128))
    rationale: Mapped[str] = mapped_column(Text)
    evidence_ids: Mapped[list[str]] = mapped_column(ARRAY(String(128)))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    proposed_by: Mapped[str] = mapped_column(String(64))
    proposed_role: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime]
    decided_by: Mapped[str | None] = mapped_column(String(64))
    decided_at: Mapped[datetime | None]
    decision_comment: Mapped[str | None] = mapped_column(Text)
    investigation_id: Mapped[str | None] = mapped_column(String(32))
    executed_at: Mapped[datetime | None]
    execution_result: Mapped[str | None] = mapped_column(Text)


# --- Investigations ---------------------------------------------------------------------


class Investigation(Base):
    __tablename__ = "investigations"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    question: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16))  # running | completed | failed
    subject: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(16))
    model: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
    report: Mapped[dict[str, Any] | None]
    grounding: Mapped[dict[str, Any] | None]
    trace: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    stats: Mapped[dict[str, Any] | None]  # tokens, tool calls, latency, stop reason
    error: Mapped[str | None] = mapped_column(Text)


# --- Audit log ------------------------------------------------------------------------------


class AuditRecord(Base):
    """Append-only and hash-chained: each row's hash covers its content and the previous
    row's hash. A database trigger rejects UPDATE and DELETE (migration 0006)."""

    __tablename__ = "audit_log"
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime]
    kind: Mapped[str] = mapped_column(String(32))  # tool_call | llm_call | investigation | ...
    actor: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(16))
    investigation_id: Mapped[str | None] = mapped_column(String(32), index=True)
    payload: Mapped[dict[str, Any]]
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64), unique=True)
