"""Builds the entity graph from three origins:

- record:   rows in the relational tables (topology, changes, orders ...)
- event:    entities and relations extracted from logs (error codes, failing upstreams)
- document: services, hosts and error codes mentioned in runbooks and postmortems

The graph is derived data. It is rebuilt from scratch after a scenario load or a document
ingest, so it can never drift from its sources.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from opsintel.db import models as m

ERROR_CODE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b|\bENOSPC\b")
# Codes that look like constants but are configuration names, not errors.
NOT_ERROR_CODES = {"PAYFLOW_POOL_MAX"}


@dataclass
class _Builder:
    entities: dict[str, dict[str, Any]] = field(default_factory=dict)
    edges: dict[tuple[str, str, str], dict[str, Any]] = field(default_factory=dict)

    def node(self, id: str, type: str, name: str, **props: Any) -> str:
        self.entities.setdefault(id, {"id": id, "type": type, "name": name, "properties": props})
        return id

    def edge(self, src: str, dst: str, relation: str, origin: str, **props: Any) -> None:
        self.edges.setdefault(
            (src, dst, relation),
            {"src": src, "dst": dst, "relation": relation, "origin": origin, "properties": props},
        )


@dataclass
class _Observation:
    count: int = 0
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    samples: list[str] = field(default_factory=list)

    def add(self, event_id: str, ts: datetime) -> None:
        self.count += 1
        self.first_seen = min(self.first_seen or ts, ts)
        self.last_seen = max(self.last_seen or ts, ts)
        if len(self.samples) < 3:
            self.samples.append(event_id)

    def props(self) -> dict[str, Any]:
        assert self.first_seen and self.last_seen
        return {
            "count": self.count,
            "first_seen": self.first_seen.isoformat(),
            "last_seen": self.last_seen.isoformat(),
            "sample_event_ids": self.samples,
        }


def error_codes_in(text: str) -> set[str]:
    return set(ERROR_CODE.findall(text)) - NOT_ERROR_CODES


def rebuild_graph(session: Session) -> dict[str, int]:
    b = _Builder()
    _from_records(session, b)
    _from_events(session, b)
    _from_documents(session, b)

    session.execute(delete(m.Edge))
    session.execute(delete(m.Entity))
    if b.entities:
        session.execute(insert(m.Entity), list(b.entities.values()))
    if b.edges:
        session.execute(insert(m.Edge), list(b.edges.values()))
    by_type: dict[str, int] = defaultdict(int)
    for e in b.entities.values():
        by_type[e["type"]] += 1
    return {**by_type, "edges": len(b.edges)}


def _from_records(session: Session, b: _Builder) -> None:
    for t in session.scalars(select(m.Team)):
        b.node(
            f"team:{t.id}", "team", t.name, slack_channel=t.slack_channel, oncall=t.oncall_handle
        )
    for s in session.scalars(select(m.Service)):
        b.node(f"service:{s.id}", "service", s.id, kind=s.kind, tier=s.tier)
        if s.owner_team_id:
            b.edge(f"team:{s.owner_team_id}", f"service:{s.id}", "owns", "record")
    for d in session.scalars(select(m.ServiceDependency)):
        b.edge(
            f"service:{d.caller_id}",
            f"service:{d.callee_id}",
            "depends_on",
            "record",
            criticality=d.criticality,
        )
    for h in session.scalars(select(m.Host)):
        b.node(f"host:{h.id}", "host", h.id, az=h.az, instance_type=h.instance_type)
        b.edge(f"service:{h.service_id}", f"host:{h.id}", "runs_on", "record")
    for dep in session.scalars(select(m.Deploy)):
        b.node(
            dep.id,
            "deploy",
            f"{dep.service_id} {dep.version}",
            version=dep.version,
            previous_version=dep.previous_version,
            author=dep.author,
            deployed_at=dep.deployed_at.isoformat(),
            summary=dep.change_summary,
        )
        b.edge(dep.id, f"service:{dep.service_id}", "deployed_to", "record")
    for c in session.scalars(select(m.ConfigChange)):
        b.node(
            c.id,
            "config_change",
            c.key,
            kind=c.kind,
            old=c.old_value,
            new=c.new_value,
            changed_by=c.changed_by,
            changed_at=c.changed_at.isoformat(),
        )
        b.edge(c.id, f"service:{c.service_id}", "changed", "record")
    # Customers carry no PII in the graph; names and emails stay behind RBAC in `customers`.
    for cid, tier, region in session.execute(
        select(m.Customer.id, m.Customer.tier, m.Customer.region)
    ):
        b.node(cid, "customer", cid, tier=tier, region=region)
    for o in session.execute(
        select(m.Order.id, m.Order.customer_id, m.Order.status, m.Order.failure_reason)
    ):
        b.node(o.id, "order", o.id, status=o.status, failure_reason=o.failure_reason)
        b.edge(o.id, o.customer_id, "placed_by", "record")
    for p in session.execute(
        select(m.Payment.id, m.Payment.order_id, m.Payment.status, m.Payment.error_code)
    ):
        b.node(p.id, "payment", p.id, status=p.status, error_code=p.error_code)
        b.edge(p.id, p.order_id, "pays_for", "record")
    for sh in session.execute(
        select(m.Shipment.id, m.Shipment.order_id, m.Shipment.status, m.Shipment.error_code)
    ):
        b.node(sh.id, "shipment", sh.id, status=sh.status, error_code=sh.error_code)
        b.edge(sh.id, sh.order_id, "ships", "record")


def _from_events(session: Session, b: _Builder) -> None:
    emitted: dict[tuple[str, str], _Observation] = defaultdict(_Observation)
    upstream_failures: dict[tuple[str, str], _Observation] = defaultdict(_Observation)
    for e in session.scalars(
        select(m.Event).where(m.Event.severity.in_(["warning", "error", "critical"]))
    ):
        codes = error_codes_in(e.message)
        if code := e.attributes.get("error_code"):
            codes.add(code)
        for code in codes:
            emitted[(f"service:{e.service_id}", code)].add(e.id, e.ts)
            if e.host_id:
                emitted[(f"host:{e.host_id}", code)].add(e.id, e.ts)
        if upstream := e.attributes.get("upstream"):
            upstream_failures[(e.service_id, upstream)].add(e.id, e.ts)

    for (src, code), obs in emitted.items():
        b.node(f"error_code:{code}", "error_code", code)
        b.edge(src, f"error_code:{code}", "emitted", "event", **obs.props())
    for (caller, callee), obs in upstream_failures.items():
        b.edge(
            f"service:{caller}", f"service:{callee}", "saw_failures_from", "event", **obs.props()
        )


def _from_documents(session: Session, b: _Builder) -> None:
    services = {e["name"] for e in b.entities.values() if e["type"] == "service"}
    hosts = {e["name"] for e in b.entities.values() if e["type"] == "host"}
    word = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)+")
    for doc in session.scalars(select(m.Document)):
        b.node(doc.id, "document", doc.title, kind=doc.kind, acl_roles=doc.acl_roles)
        for s in doc.services:
            if s in services:
                b.edge(doc.id, f"service:{s}", "covers", "document")
        mentions: dict[str, set[str]] = defaultdict(set)
        for chunk in session.scalars(select(m.Chunk).where(m.Chunk.document_id == doc.id)):
            text = f"{chunk.heading}\n{chunk.content}"
            for code in error_codes_in(text):
                mentions[f"error_code:{code}"].add(chunk.id)
            for token in set(word.findall(text)):
                if token in services:
                    mentions[f"service:{token}"].add(chunk.id)
                elif token in hosts:
                    mentions[f"host:{token}"].add(chunk.id)
        for target, chunk_ids in mentions.items():
            if target.startswith("error_code:"):
                b.node(target, "error_code", target.removeprefix("error_code:"))
            if target in b.entities:
                b.edge(doc.id, target, "mentions", "document", chunk_ids=sorted(chunk_ids))
