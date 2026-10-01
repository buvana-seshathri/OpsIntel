"""Writes a generated dataset to Postgres, replacing whatever scenario was loaded."""

from __future__ import annotations

from sqlalchemy import insert, text
from sqlalchemy.orm import Session

from opsintel.db import models as m
from opsintel.simulator.engine import Dataset

# Insertion order respects foreign keys.
TABLES: list[tuple[str, type[m.Base]]] = [
    ("teams", m.Team),
    ("services", m.Service),
    ("service_dependencies", m.ServiceDependency),
    ("hosts", m.Host),
    ("customers", m.Customer),
    ("deploys", m.Deploy),
    ("config_changes", m.ConfigChange),
    ("orders", m.Order),
    ("payments", m.Payment),
    ("shipments", m.Shipment),
    ("events", m.Event),
    ("metric_points", m.MetricPoint),
    ("scenario_runs", m.ScenarioRun),
]

BATCH = 5000


def reset(session: Session) -> None:
    names = ", ".join(name for name, _ in TABLES)
    session.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))


def load(session: Session, ds: Dataset) -> dict[str, int]:
    reset(session)
    counts: dict[str, int] = {}
    for name, model in TABLES:
        rows = ds.tables.get(name, [])
        for i in range(0, len(rows), BATCH):
            session.execute(insert(model), rows[i : i + BATCH])
        counts[name] = len(rows)
    return counts
