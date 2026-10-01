"""Graph traversal. Dependency walks are recursive CTEs over `edges`; path finding is a
bounded breadth-first search that fetches one frontier per query."""

from __future__ import annotations

from typing import Any

from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from opsintel.db.models import Edge, Entity

# Fan-out relations that connect thousands of commerce records. Path finding skips them
# unless asked, otherwise every path would detour through some customer.
BULK_RELATIONS = {"placed_by", "pays_for", "ships"}

_TRACE_SQL = """
WITH RECURSIVE walk(node, depth, path, all_hard) AS (
    SELECT CAST(:start AS text), 0, ARRAY[CAST(:start AS text)], true
  UNION ALL
    SELECT e.{next}, w.depth + 1, w.path || e.{next},
           w.all_hard AND e.properties->>'criticality' = 'hard'
    FROM walk w
    JOIN edges e ON e.relation = 'depends_on' AND e.{this} = w.node
    WHERE w.depth < :max_depth AND NOT e.{next} = ANY(w.path)
)
SELECT DISTINCT ON (node) node, depth, path, all_hard
FROM walk WHERE depth > 0
ORDER BY node, depth, all_hard DESC
"""


class EntityNotFound(LookupError):
    pass


def get_entity(session: Session, entity_id: str, neighbor_limit: int = 25) -> dict[str, Any]:
    entity = session.get(Entity, entity_id)
    if entity is None:
        raise EntityNotFound(entity_id)
    return {
        "id": entity.id,
        "type": entity.type,
        "name": entity.name,
        "properties": entity.properties,
        "neighbors": neighbors(session, entity_id, limit=neighbor_limit),
    }


def neighbors(
    session: Session,
    entity_id: str,
    relation: str | None = None,
    direction: str = "both",
    limit: int = 50,
) -> list[dict[str, Any]]:
    conds = []
    if direction in ("out", "both"):
        conds.append(Edge.src == entity_id)
    if direction in ("in", "both"):
        conds.append(Edge.dst == entity_id)
    stmt = select(Edge).where(or_(*conds))
    if relation:
        stmt = stmt.where(Edge.relation == relation)
    stmt = stmt.order_by(Edge.relation, Edge.src, Edge.dst).limit(limit)
    out = []
    for e in session.scalars(stmt):
        outgoing = e.src == entity_id
        out.append(
            {
                "relation": e.relation,
                "direction": "out" if outgoing else "in",
                "entity_id": e.dst if outgoing else e.src,
                "origin": e.origin,
                "properties": e.properties,
            }
        )
    return out


def trace_dependencies(
    session: Session, service: str, direction: str = "downstream", max_depth: int = 3
) -> list[dict[str, Any]]:
    """downstream = what `service` calls (transitively); upstream = who calls it.
    `hard_path` is true when every hop is a hard dependency, i.e. a failure propagates."""
    start = service if service.startswith("service:") else f"service:{service}"
    if session.get(Entity, start) is None:
        raise EntityNotFound(start)
    this, nxt = ("src", "dst") if direction == "downstream" else ("dst", "src")
    rows = session.execute(
        text(_TRACE_SQL.format(this=this, next=nxt)),
        {"start": start, "max_depth": max_depth},
    )
    result = [
        {"service": r.node, "depth": r.depth, "path": list(r.path), "hard_path": r.all_hard}
        for r in rows
    ]
    return sorted(result, key=lambda r: (r["depth"], r["service"]))


def services_for(session: Session, entity_id: str) -> list[str]:
    """Map any entity to the services it lives on or acts on."""
    entity = session.get(Entity, entity_id)
    if entity is None:
        raise EntityNotFound(entity_id)
    if entity.type == "service":
        return [entity_id]
    relation, column = {
        "host": ("runs_on", Edge.src),
        "deploy": ("deployed_to", Edge.dst),
        "config_change": ("changed", Edge.dst),
        "error_code": ("emitted", Edge.src),
    }.get(entity.type, (None, None))
    if relation is None or column is None:
        return []
    other = Edge.dst if column is Edge.src else Edge.src
    stmt = select(column).where(Edge.relation == relation, other == entity_id)
    return sorted(s for s in session.scalars(stmt) if s.startswith("service:"))


def blast_radius(session: Session, entity_id: str, max_depth: int = 4) -> dict[str, Any]:
    """Services that fail (hard path) or degrade (soft path) if `entity_id` is broken,
    with the teams that own them."""
    origins = services_for(session, entity_id)
    impact: dict[str, dict[str, Any]] = {}
    for origin in origins:
        impact.setdefault(origin, {"service": origin, "depth": 0, "impact": "fails"})
        for row in trace_dependencies(session, origin, "upstream", max_depth):
            level = "fails" if row["hard_path"] else "degraded"
            seen = impact.get(row["service"])
            if seen is None or (seen["impact"] == "degraded" and level == "fails"):
                impact[row["service"]] = {
                    "service": row["service"],
                    "depth": row["depth"],
                    "impact": level,
                }
    owners = {
        e.dst: e.src
        for e in session.scalars(select(Edge).where(Edge.relation == "owns", Edge.dst.in_(impact)))
    }
    for item in impact.values():
        item["owner_team"] = owners.get(item["service"])
    return {
        "entity_id": entity_id,
        "origin_services": origins,
        "impacted": sorted(impact.values(), key=lambda i: (i["depth"], i["service"])),
    }


def find_path(
    session: Session,
    source: str,
    target: str,
    max_depth: int = 4,
    include_bulk: bool = False,
) -> list[dict[str, Any]] | None:
    """Shortest undirected path as a list of hops, or None if none within max_depth."""
    for endpoint in (source, target):
        if session.get(Entity, endpoint) is None:
            raise EntityNotFound(endpoint)
    if source == target:
        return []
    parent: dict[str, tuple[str, Edge]] = {}
    frontier = {source}
    seen = {source}
    for _ in range(max_depth):
        stmt = select(Edge).where(or_(Edge.src.in_(frontier), Edge.dst.in_(frontier)))
        if not include_bulk:
            stmt = stmt.where(Edge.relation.not_in(BULK_RELATIONS))
        nxt: set[str] = set()
        for e in session.scalars(stmt.order_by(Edge.id)):
            for here, there in ((e.src, e.dst), (e.dst, e.src)):
                if here in frontier and there not in seen:
                    seen.add(there)
                    parent[there] = (here, e)
                    nxt.add(there)
        if target in seen:
            hops = []
            node = target
            while node != source:
                prev, edge = parent[node]
                hops.append(
                    {
                        "from": prev,
                        "relation": edge.relation,
                        "to": node,
                        "forward": edge.src == prev,
                        "origin": edge.origin,
                    }
                )
                node = prev
            return list(reversed(hops))
        if not nxt:
            return None
        frontier = nxt
    return None
