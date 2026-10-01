"""The tools. Docstrings are what the model reads, so they say when to use each tool.

Results are JSON-friendly dicts. Every row carries its citable ID (evt_..., dep_..., chk_...)
so the agent can ground its report in evidence the evals can verify.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import Field
from sqlalchemy import Float, case, cast, func, select
from sqlalchemy.orm import Session

from opsintel.auth import Permission
from opsintel.context import current
from opsintel.db import models as m
from opsintel.graph import queries as gq
from opsintel.grounding import existing_ids
from opsintel.rag.search import hybrid_search
from opsintel.tools.registry import ToolContext, ToolInputError, tool

SEVERITY_RANK = {"info": 0, "warning": 1, "error": 2, "critical": 3}
METRICS = ("request_rate", "error_rate", "p99_latency_ms")
ACTION_TYPES = {
    "rollback_deploy": "deploy",
    "revert_config_change": "config_change",
    "failover_provider": "service",
    "rotate_certificate": "service",
    "drain_host": "host",
    "scale_service": "service",
    "page_team": "team",
}
Minutes = Annotated[int, Field(ge=1, le=7 * 24 * 60)]


def _window(ctx: ToolContext, since_minutes: int) -> tuple[datetime, datetime]:
    return ctx.now - timedelta(minutes=since_minutes), ctx.now


def _minutes_ago(ctx: ToolContext, ts: datetime) -> float:
    return round((ctx.now - ts).total_seconds() / 60, 1)


def _readable_document_ids(ctx: ToolContext) -> set[str]:
    stmt = select(m.Document.id).where(m.Document.acl_roles.overlap([ctx.principal.role]))
    return set(ctx.session.scalars(stmt))


def _hidden(ctx: ToolContext, entity_id: str, readable_docs: set[str]) -> bool:
    return entity_id.startswith("doc_") and entity_id not in readable_docs


# --- Telemetry ------------------------------------------------------------------------


@tool(Permission.READ_TELEMETRY)
def list_services(ctx: ToolContext) -> dict[str, Any]:
    """List every service with its kind (internal, database, external), tier (1 = revenue
    critical), owning team, host count and direct dependencies. Start here to learn the
    system's names."""
    hosts = {
        sid: n
        for sid, n in ctx.session.execute(
            select(m.Host.service_id, func.count()).group_by(m.Host.service_id)
        )
    }
    deps: dict[str, list[dict[str, str]]] = {}
    for d in ctx.session.scalars(select(m.ServiceDependency)):
        deps.setdefault(d.caller_id, []).append(
            {"service": d.callee_id, "criticality": d.criticality}
        )
    return {
        "services": [
            {
                "id": s.id,
                "kind": s.kind,
                "tier": s.tier,
                "owner_team": s.owner_team_id,
                "hosts": hosts.get(s.id, 0),
                "depends_on": deps.get(s.id, []),
                "description": s.description,
            }
            for s in ctx.session.scalars(select(m.Service).order_by(m.Service.id))
        ]
    }


@tool(Permission.READ_TELEMETRY)
def query_events(
    ctx: ToolContext,
    service: Annotated[str | None, Field(description="Service ID, e.g. payments-svc")] = None,
    host: Annotated[str | None, Field(description="Host ID, e.g. payments-svc-02")] = None,
    kind: Literal["log", "alert", "deploy", "config_change"] | None = None,
    min_severity: Literal["info", "warning", "error", "critical"] = "info",
    error_code: str | None = None,
    contains: Annotated[str | None, Field(description="Case-insensitive substring")] = None,
    since_minutes: Minutes = 60,
    group_by: Annotated[
        Literal["service", "host", "error_code", "kind", "severity"] | None,
        Field(description="Return counts per group instead of individual events"),
    ] = None,
    order: Literal["oldest", "newest"] = "oldest",
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    """Search logs, alerts, deploy and config-change events in a time window ending now.
    Use group_by="error_code" or "host" to see what dominates; use order="oldest" with a
    filter to find when a problem started."""
    start, end = _window(ctx, since_minutes)
    conds = [m.Event.ts >= start, m.Event.ts <= end]
    if service:
        conds.append(m.Event.service_id == service)
    if host:
        conds.append(m.Event.host_id == host)
    if kind:
        conds.append(m.Event.kind == kind)
    if min_severity != "info":
        allowed = [s for s, r in SEVERITY_RANK.items() if r >= SEVERITY_RANK[min_severity]]
        conds.append(m.Event.severity.in_(allowed))
    if error_code:
        conds.append(m.Event.attributes["error_code"].astext == error_code)
    if contains:
        conds.append(m.Event.message.icontains(contains, autoescape=True))
    window = {"start": start, "end": end}

    if group_by:
        key = (
            m.Event.attributes["error_code"].astext
            if group_by == "error_code"
            else {
                "service": m.Event.service_id,
                "host": m.Event.host_id,
                "kind": m.Event.kind,
                "severity": m.Event.severity,
            }[group_by]
        )
        rows = ctx.session.execute(
            select(
                key.label("key"),
                func.count().label("n"),
                func.min(m.Event.ts),
                func.max(m.Event.ts),
                func.min(m.Event.id),
            )
            .where(*conds)
            .group_by(key)
            .order_by(func.count().desc())
            .limit(limit)
        ).all()
        return {
            "window": window,
            "group_by": group_by,
            "groups": [
                {group_by: k, "count": n, "first_seen": f, "last_seen": la, "sample_event_id": ev}
                for k, n, f, la, ev in rows
            ],
        }

    total = ctx.session.scalar(select(func.count()).select_from(m.Event).where(*conds)) or 0
    ordering = m.Event.ts.asc() if order == "oldest" else m.Event.ts.desc()
    events = ctx.session.scalars(select(m.Event).where(*conds).order_by(ordering).limit(limit))
    return {
        "window": window,
        "total": total,
        "returned": min(total, limit),
        "events": [
            {
                "id": e.id,
                "ts": e.ts,
                "minutes_ago": _minutes_ago(ctx, e.ts),
                "service": e.service_id,
                "host": e.host_id,
                "kind": e.kind,
                "severity": e.severity,
                "message": e.message[:400],
                "attributes": e.attributes,
            }
            for e in events
        ],
    }


@tool(Permission.READ_TELEMETRY)
def get_metrics(
    ctx: ToolContext,
    service: str,
    metric: Literal["request_rate", "error_rate", "p99_latency_ms", "all"] = "all",
    since_minutes: Minutes = 120,
    step_minutes: Annotated[int, Field(ge=1, le=60)] = 5,
) -> dict[str, Any]:
    """Per-minute metrics for one service, averaged into buckets of step_minutes, plus a
    summary comparing the last 10 minutes with the earlier baseline and the first bucket
    where the metric exceeded twice its baseline (a rough change point)."""
    start, end = _window(ctx, since_minutes)
    names = list(METRICS) if metric == "all" else [metric]
    out: dict[str, Any] = {
        "service": service,
        "window": {"start": start, "end": end},
        "step_minutes": step_minutes,
        "metrics": {},
    }
    for name in names:
        points = ctx.session.execute(
            select(m.MetricPoint.ts, m.MetricPoint.value)
            .where(
                m.MetricPoint.service_id == service,
                m.MetricPoint.name == name,
                m.MetricPoint.ts >= start,
                m.MetricPoint.ts <= end,
            )
            .order_by(m.MetricPoint.ts)
        ).all()
        if not points:
            continue
        buckets: dict[datetime, list[float]] = {}
        for ts, v in points:
            offset = int((ts - start).total_seconds() // 60) // step_minutes * step_minutes
            buckets.setdefault(start + timedelta(minutes=offset), []).append(v)
        averaged = [(ts, round(sum(v) / len(v), 5)) for ts, v in buckets.items()]
        series = [{"ts": ts, "value": v} for ts, v in averaged]
        recent = [v for ts, v in points if ts > end - timedelta(minutes=10)]
        earlier = [v for ts, v in points if ts <= end - timedelta(minutes=30)] or recent
        baseline = sum(earlier) / len(earlier)
        current = sum(recent) / len(recent) if recent else baseline
        change = next((ts for ts, v in averaged if baseline > 0 and v > 2 * baseline), None)
        out["metrics"][name] = {
            "series": series,
            "baseline": round(baseline, 5),
            "last_10m": round(current, 5),
            "ratio": round(current / baseline, 2) if baseline else None,
            "first_bucket_above_2x_baseline": change,
        }
    if not out["metrics"]:
        raise ToolInputError(f"no metrics for service {service!r}; see list_services")
    return out


@tool(Permission.READ_TELEMETRY)
def list_changes(
    ctx: ToolContext,
    service: str | None = None,
    since_minutes: Minutes = 180,
) -> dict[str, Any]:
    """Deploys and config changes (including feature flags and cron schedules), newest
    first. A change shortly before symptoms is a suspect, not proof: check that it touches
    the failing path."""
    start, _ = _window(ctx, since_minutes)
    deploys = select(m.Deploy).where(m.Deploy.deployed_at >= start)
    configs = select(m.ConfigChange).where(m.ConfigChange.changed_at >= start)
    if service:
        deploys = deploys.where(m.Deploy.service_id == service)
        configs = configs.where(m.ConfigChange.service_id == service)
    changes: list[dict[str, Any]] = [
        {
            "id": d.id,
            "type": "deploy",
            "service": d.service_id,
            "at": d.deployed_at,
            "minutes_ago": _minutes_ago(ctx, d.deployed_at),
            "author": d.author,
            "version": d.version,
            "previous_version": d.previous_version,
            "summary": d.change_summary,
            "status": d.status,
        }
        for d in ctx.session.scalars(deploys)
    ] + [
        {
            "id": c.id,
            "type": "config_change",
            "service": c.service_id,
            "at": c.changed_at,
            "minutes_ago": _minutes_ago(ctx, c.changed_at),
            "author": c.changed_by,
            "kind": c.kind,
            "key": c.key,
            "old_value": c.old_value,
            "new_value": c.new_value,
        }
        for c in ctx.session.scalars(configs)
    ]
    changes.sort(key=lambda c: c["at"], reverse=True)
    return {"since": start, "changes": changes}


# --- Entity graph -----------------------------------------------------------------------


def _not_found(e: gq.EntityNotFound) -> ToolInputError:
    return ToolInputError(f"no entity {e.args[0]!r}")


@tool(Permission.READ_GRAPH)
def get_entity(ctx: ToolContext, entity_id: str) -> dict[str, Any]:
    """Look up any entity (service:<id>, host:<id>, error_code:<CODE>, team:<id>, dep_...,
    cfg_..., ord_..., doc_...) with its properties and neighbours in the graph, e.g. which
    services and hosts emitted an error code and which runbooks mention it."""
    readable = _readable_document_ids(ctx)
    if _hidden(ctx, entity_id, readable):
        raise ToolInputError(f"no entity {entity_id!r}")
    try:
        entity = gq.get_entity(ctx.session, entity_id, neighbor_limit=60)
    except gq.EntityNotFound as e:
        raise _not_found(e) from e
    entity["neighbors"] = [
        n for n in entity["neighbors"] if not _hidden(ctx, n["entity_id"], readable)
    ]
    return entity


@tool(Permission.READ_GRAPH)
def trace_dependencies(
    ctx: ToolContext,
    service: str,
    direction: Literal["downstream", "upstream"] = "downstream",
    max_depth: Annotated[int, Field(ge=1, le=6)] = 3,
) -> dict[str, Any]:
    """Walk the service dependency graph. downstream = what the service calls; upstream =
    who calls it. hard_path=true means every hop is a hard dependency, so a failure at the
    far end propagates to the start."""
    try:
        rows = gq.trace_dependencies(ctx.session, service, direction, max_depth)
    except gq.EntityNotFound as e:
        raise _not_found(e) from e
    return {"service": service, "direction": direction, "dependencies": rows}


@tool(Permission.READ_GRAPH)
def blast_radius(ctx: ToolContext, entity_id: str) -> dict[str, Any]:
    """Which services fail or degrade if this entity (service, host, deploy, config change
    or error code) is broken, and which teams own them."""
    try:
        return gq.blast_radius(ctx.session, entity_id)
    except gq.EntityNotFound as e:
        raise _not_found(e) from e


@tool(Permission.READ_GRAPH)
def find_path(
    ctx: ToolContext,
    source: str,
    target: str,
    max_depth: Annotated[int, Field(ge=1, le=6)] = 4,
) -> dict[str, Any]:
    """Shortest chain of relationships between two entities, e.g. from a deploy to an
    error code or to a runbook. Returns null if none within max_depth hops."""
    readable = _readable_document_ids(ctx)
    for endpoint in (source, target):
        if _hidden(ctx, endpoint, readable):
            raise ToolInputError(f"no entity {endpoint!r}")
    try:
        path = gq.find_path(ctx.session, source, target, max_depth)
    except gq.EntityNotFound as e:
        raise _not_found(e) from e
    if path is not None:
        for hop in path:
            for side in ("from", "to"):
                if _hidden(ctx, hop[side], readable):
                    hop[side] = "<restricted document>"
    return {"source": source, "target": target, "path": path}


# --- Documents ---------------------------------------------------------------------------


@tool(Permission.READ_DOCS)
def search_docs(
    ctx: ToolContext,
    query: Annotated[str, Field(min_length=2, max_length=500)],
    k: Annotated[int, Field(ge=1, le=10)] = 5,
    kind: Literal["runbook", "postmortem", "policy"] | None = None,
) -> dict[str, Any]:
    """Hybrid (semantic + keyword) search over runbooks, postmortems and policies you are
    allowed to read. Search with concrete terms you found: service names, error codes,
    log phrases. Cite results by chunk_id."""
    hits = hybrid_search(
        ctx.session,
        ctx.embedder,
        query,
        roles=[ctx.principal.role],
        k=k,
        kinds=[kind] if kind else None,
    )
    return {
        "query": query,
        "results": [
            {
                "chunk_id": h.chunk_id,
                "document_id": h.document_id,
                "title": h.title,
                "kind": h.kind,
                "heading": h.heading,
                "content": h.content,
                "score": h.score,
            }
            for h in hits
        ],
    }


# --- Business data ------------------------------------------------------------------------


@tool(Permission.READ_ORDERS)
def query_orders(
    ctx: ToolContext,
    since_minutes: Minutes = 60,
    status: Literal["completed", "failed"] | None = None,
    failure_reason: str | None = None,
    group_by: Literal[
        "failure_reason", "customer_region", "customer_tier", "status", "payment_error_code"
    ] = "failure_reason",
    sample: Annotated[int, Field(ge=0, le=20)] = 5,
) -> dict[str, Any]:
    """Order outcomes in a window, grouped (by failure_reason, customer_region,
    customer_tier, status or payment_error_code) with failure rates. Shows customer IDs
    only; personal details need get_customer and the read:pii permission."""
    start, end = _window(ctx, since_minutes)
    conds = [m.Order.created_at >= start, m.Order.created_at <= end]
    if status:
        conds.append(m.Order.status == status)
    if failure_reason:
        conds.append(m.Order.failure_reason == failure_reason)
    key = {
        "failure_reason": m.Order.failure_reason,
        "customer_region": m.Customer.region,
        "customer_tier": m.Customer.tier,
        "status": m.Order.status,
        "payment_error_code": m.Payment.error_code,
    }[group_by]
    failed = func.sum(case((m.Order.status == "failed", 1), else_=0))
    stmt = (
        select(
            key.label("key"),
            func.count().label("orders"),
            failed.label("failed"),
            cast(failed, Float) / func.count(),
        )
        .select_from(m.Order)
        .join(m.Customer, m.Customer.id == m.Order.customer_id)
        .outerjoin(m.Payment, m.Payment.order_id == m.Order.id)
        .where(*conds)
        .group_by(key)
        .order_by(func.count().desc())
    )
    groups = [
        {group_by: k, "orders": n, "failed": f, "failure_rate": round(rate, 4)}
        for k, n, f, rate in ctx.session.execute(stmt)
    ]
    samples = ctx.session.execute(
        select(
            m.Order.id,
            m.Order.customer_id,
            m.Order.status,
            m.Order.failure_reason,
            m.Order.created_at,
        )
        .where(*conds)
        .order_by(m.Order.created_at.desc())
        .limit(sample)
    ).all()
    total = sum(g["orders"] for g in groups)
    return {
        "window": {"start": start, "end": end},
        "total_orders": total,
        "failed_orders": sum(g["failed"] for g in groups),
        "groups": groups,
        "sample_orders": [
            {"id": i, "customer_id": c, "status": s, "failure_reason": r, "created_at": t}
            for i, c, s, r, t in samples
        ],
    }


@tool(Permission.READ_PII)
def get_customer(ctx: ToolContext, customer_id: str) -> dict[str, Any]:
    """Customer contact details (name, email, phone). Personal data: admins only, and only
    when an investigation genuinely needs to contact a customer."""
    c = ctx.session.get(m.Customer, customer_id)
    if c is None:
        raise ToolInputError(f"no customer {customer_id!r}")
    return {
        "id": c.id,
        "name": c.name,
        "email": c.email,
        "phone": c.phone,
        "tier": c.tier,
        "region": c.region,
    }


# --- Actions --------------------------------------------------------------------------------


@tool(Permission.PROPOSE_ACTION)
def propose_action(
    ctx: ToolContext,
    type: Annotated[
        Literal[
            "rollback_deploy",
            "revert_config_change",
            "failover_provider",
            "rotate_certificate",
            "drain_host",
            "scale_service",
            "page_team",
        ],
        Field(description="Action to propose; it runs only after a human approves it"),
    ],
    target: Annotated[
        str,
        Field(
            description=(
                "Entity ID exactly as tools return it. rollback_deploy: the dep_ ID; "
                "revert_config_change: the cfg_ ID; failover_provider: the standby provider "
                "to switch TO (service:...); rotate_certificate and scale_service: the "
                "service:... that needs it; drain_host: the host:...; page_team: the team:..."
            )
        ),
    ],
    rationale: Annotated[str, Field(min_length=10, max_length=2000)],
    evidence_ids: Annotated[list[str], Field(min_length=1, max_length=20)],
) -> dict[str, Any]:
    """Queue a state-changing action for human approval. Nothing changes until a responder
    approves it. The target must match the action (rollback_deploy needs a dep_ ID, and so
    on) and every evidence ID must exist."""
    session: Session = ctx.session
    expected = ACTION_TYPES[type]
    entity = session.get(m.Entity, target)
    if entity is None or entity.type != expected:
        raise ToolInputError(f"{type} needs a {expected} target; {target!r} is not one")
    missing = sorted(set(evidence_ids) - existing_ids(session, evidence_ids))
    if missing:
        raise ToolInputError(f"unknown evidence IDs: {missing}")
    proposal = m.ActionProposal(
        id=f"act_{secrets.token_hex(4)}",
        type=type,
        target=target,
        rationale=rationale,
        evidence_ids=list(dict.fromkeys(evidence_ids)),
        status="pending",
        proposed_by=ctx.principal.subject,
        proposed_role=ctx.principal.role,
        investigation_id=current().investigation_id,
        created_at=ctx.now,
    )
    session.add(proposal)
    session.flush()
    return {
        "proposal_id": proposal.id,
        "status": "pending_approval",
        "type": type,
        "target": target,
    }
