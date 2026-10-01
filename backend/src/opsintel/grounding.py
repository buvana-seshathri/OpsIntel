"""Resolve citable IDs to the rows they name. Used by tools that accept evidence and by
the evals that check whether a report's citations are real."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from opsintel.db import models as m

PREFIX_TABLES: dict[str, type[m.Base]] = {
    "evt_": m.Event,
    "dep_": m.Deploy,
    "cfg_": m.ConfigChange,
    "ord_": m.Order,
    "pay_": m.Payment,
    "shp_": m.Shipment,
    "cus_": m.Customer,
    "chk_": m.Chunk,
    "doc_": m.Document,
    "act_": m.ActionProposal,
}
GRAPH_PREFIXES = ("service:", "host:", "team:", "error_code:")


def kind_of(ref: str) -> str | None:
    for prefix in PREFIX_TABLES:
        if ref.startswith(prefix):
            return prefix
    for prefix in GRAPH_PREFIXES:
        if ref.startswith(prefix):
            return prefix
    return None


def existing_ids(session: Session, refs: Iterable[str]) -> set[str]:
    """The subset of `refs` that name a real row or graph entity."""
    by_kind: dict[str, set[str]] = defaultdict(set)
    for ref in refs:
        if (kind := kind_of(ref)) is not None:
            by_kind[kind].add(ref)
    found: set[str] = set()
    for kind, ids in by_kind.items():
        if kind in PREFIX_TABLES:
            model = PREFIX_TABLES[kind]
            id_col = model.__table__.c.id
            found |= set(session.scalars(select(id_col).where(id_col.in_(ids))))
        else:
            found |= set(session.scalars(select(m.Entity.id).where(m.Entity.id.in_(ids))))
    return found
