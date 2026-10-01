"""Deterministic telemetry generator.

A scenario declares changes (deploys, config changes), faults and traffic surges on a
120-minute window. The engine then simulates every service minute by minute, propagates
errors and latency up the hard-dependency graph, and derives everything else from that
single model: metrics, alerts, logs, orders, payments and shipments. Because symptoms
are computed from causes rather than written by hand, the data stays self-consistent,
and the scenario's ground truth is known exactly.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel

from opsintel.simulator.company import (
    AZS,
    DEPENDENCIES,
    ENGINEERS,
    GATEWAY_BASE_RPM,
    SERVICE_BY_ID,
    SERVICES,
    TEAMS,
    hard_callees,
    host_ids,
    topological_order,
)

WINDOW_MINUTES = 120
CUSTOMER_SEED = 1234  # customers are identical across scenarios and seeds
N_CUSTOMERS = 400

BASELINE_CODES = {
    "api-gateway": "GATEWAY_ERROR",
    "checkout-svc": "CHECKOUT_INTERNAL_ERROR",
    "payments-svc": "PAYMENT_INTERNAL_ERROR",
    "inventory-svc": "INVENTORY_UNAVAILABLE",
    "shipping-svc": "LABEL_CREATE_FAILED",
    "orders-db": "DB_WRITE_FAILED",
    "inventory-db": "DB_TIMEOUT",
    "payflow-api": "PAYFLOW_5XX",
    "paysecure-api": "PAYSECURE_5XX",
    "carrier-api": "CARRIER_5XX",
}

START_VERSIONS = {
    "api-gateway": (3, 8, 0),
    "checkout-svc": (5, 2, 0),
    "payments-svc": (2, 11, 0),
    "inventory-svc": (1, 19, 0),
    "shipping-svc": (4, 4, 0),
}

INFO_LOGS = {
    "internal": [
        "request batch processed: {n} requests, {e} errors",
        "health check passed",
        "connection pool stats: active={a} idle={i}",
    ],
    "database": [
        "checkpoint complete: wrote {n} buffers",
        "autovacuum finished on table public.{t}",
    ],
}

NOISE_WARNINGS = {
    "api-gateway": ["rate limit applied to client ip 203.0.113.{x} (burst)"],
    "checkout-svc": ["cart {x} expired before checkout; releasing reservation"],
    "payments-svc": ["payflow request retried once and succeeded (idempotency key reused)"],
    "inventory-svc": ["stock level for SKU-{x} below reorder threshold"],
    "shipping-svc": ["carrier rate quote cache miss for zone {x}"],
    "orders-db": ["slow query 1.{x}s: SELECT * FROM orders WHERE customer_id = $1"],
    "inventory-db": ["slow query 0.9s: UPDATE stock SET reserved = reserved + $1"],
}


# --- Scenario-facing types -----------------------------------------------------------


@dataclass
class Fault:
    service_id: str
    start: int
    error_rate: float = 0.0  # added to the service's own error rate at full strength
    latency_ms: float = 0.0  # added to its own p99 at full strength
    ramp: int = 0  # minutes to reach full strength
    end: int | None = None
    error_code: str = "INTERNAL_ERROR"
    log_messages: tuple[str, ...] = ()
    host_ids: tuple[str, ...] = ()  # hosts the fault lives on; default is all hosts
    log_service_id: str | None = None  # external services log through their caller
    customer_region: str | None = None  # only orders from this region fail

    def strength(self, minute: int) -> float:
        if minute < self.start or (self.end is not None and minute >= self.end):
            return 0.0
        if self.ramp <= 0:
            return 1.0
        return min(1.0, (minute - self.start + 1) / self.ramp)


@dataclass
class Surge:
    start: int
    multiplier: float
    ramp: int = 3

    def factor(self, minute: int) -> float:
        if minute < self.start:
            return 1.0
        progress = min(1.0, (minute - self.start + 1) / max(self.ramp, 1))
        return 1.0 + (self.multiplier - 1.0) * progress


class ExpectedAction(BaseModel):
    type: str  # rollback_deploy | revert_config_change | failover_provider | ...
    target: str  # entity reference, e.g. "dep_1a2b3c4d" or "service:checkout-svc"


class GroundTruth(BaseModel):
    root_cause_kind: Literal[
        "deploy", "config_change", "third_party", "certificate", "host", "capacity", "none"
    ]
    root_cause_entity: str | None
    culprit_service: str | None
    summary: str
    expected_actions: list[ExpectedAction] = []
    red_herrings: list[str] = []
    affected_services: list[str] = []
    fault_start: datetime | None = None
    relevant_runbooks: list[str] = []


@dataclass
class Scenario:
    key: str
    title: str
    question: str  # what the on-call engineer asks the agent
    build: Callable[[ScenarioBuilder], GroundTruth]


@dataclass
class _Change:
    id: str
    service_id: str
    minute: float
    summary: str
    author: str


@dataclass
class _ConfigChange:
    id: str
    service_id: str
    minute: float
    kind: str
    key: str
    old: str
    new: str
    by: str


@dataclass
class _Log:
    minute: float
    service_id: str
    severity: str
    message: str
    host_id: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    kind: str = "log"


class ScenarioBuilder:
    def __init__(self, rng: random.Random, window_start: datetime) -> None:
        self.rng = rng
        self.window_start = window_start
        self.deploys: list[_Change] = []
        self.config_changes: list[_ConfigChange] = []
        self.faults: list[Fault] = []
        self.surges: list[Surge] = []
        self.logs: list[_Log] = []

    def ts(self, minute: float) -> datetime:
        return self.window_start + timedelta(minutes=minute)

    def _id(self, prefix: str) -> str:
        return f"{prefix}_{self.rng.getrandbits(32):08x}"

    def deploy(
        self, service_id: str, minute: float, summary: str, author: str | None = None
    ) -> str:
        dep = _Change(
            self._id("dep"), service_id, minute, summary, author or self.rng.choice(ENGINEERS)
        )
        self.deploys.append(dep)
        return dep.id

    def config_change(
        self, service_id: str, minute: float, kind: str, key: str, old: str, new: str, by: str
    ) -> str:
        cfg = _ConfigChange(self._id("cfg"), service_id, minute, kind, key, old, new, by)
        self.config_changes.append(cfg)
        return cfg.id

    def fault(self, fault: Fault) -> None:
        self.faults.append(fault)

    def surge(self, surge: Surge) -> None:
        self.surges.append(surge)

    def log(
        self,
        service_id: str,
        minute: float,
        severity: str,
        message: str,
        host_id: str | None = None,
        **attributes: Any,
    ) -> None:
        self.logs.append(_Log(minute, service_id, severity, message, host_id, attributes))


# --- Output ----------------------------------------------------------------------------


@dataclass
class Dataset:
    scenario_key: str
    question: str
    seed: int
    window_start: datetime
    window_end: datetime
    ground_truth: GroundTruth
    tables: dict[str, list[dict[str, Any]]]


@dataclass
class _Minute:
    rate: float
    own_err: float
    eff_err: float
    eff_lat: float
    code: str
    util: float


# --- Engine ----------------------------------------------------------------------------


def generate(scenario: Scenario, seed: int, anchor: datetime) -> Dataset:
    """Build the full dataset for `scenario`. `anchor` is the end of the window ("now")."""
    anchor = anchor.replace(second=0, microsecond=0)
    window_start = anchor - timedelta(minutes=WINDOW_MINUTES)
    rng = random.Random(f"{scenario.key}:{seed}")
    b = ScenarioBuilder(rng, window_start)

    _baseline_deploy_history(b)
    truth = scenario.build(b)

    customers = _customers(window_start)
    sim = _simulate(b)

    tables: dict[str, list[dict[str, Any]]] = {
        "teams": [t.__dict__.copy() for t in TEAMS],
        "services": [
            {
                "id": s.id,
                "kind": s.kind,
                "tier": s.tier,
                "owner_team_id": s.owner_team,
                "description": s.description,
            }
            for s in SERVICES
        ],
        "service_dependencies": [
            {"caller_id": a, "callee_id": c, "criticality": k} for a, c, k in DEPENDENCIES
        ],
        "hosts": [
            {
                "id": h,
                "service_id": s.id,
                "region": "us-east-1",
                "az": AZS[i % len(AZS)],
                "instance_type": "r6g.large" if s.kind == "database" else "c6g.xlarge",
            }
            for s in SERVICES
            for i, h in enumerate(host_ids(s.id))
        ],
        "customers": customers,
    }
    tables["deploys"], deploy_logs = _deploy_rows(b)
    tables["config_changes"], config_logs = _config_rows(b)
    tables["metric_points"] = _metric_rows(b, sim)
    logs = b.logs + deploy_logs + config_logs + _alert_logs(sim) + _telemetry_logs(b, sim)
    tables["events"] = _event_rows(b, logs)
    orders, payments, shipments = _commerce_rows(b, sim, customers)
    tables["orders"], tables["payments"], tables["shipments"] = orders, payments, shipments
    tables["scenario_runs"] = [
        {
            "id": f"run_{scenario.key}_{seed}",
            "scenario_key": scenario.key,
            "seed": seed,
            "window_start": window_start,
            "window_end": anchor,
            "ground_truth": truth.model_dump(mode="json"),
        }
    ]
    return Dataset(scenario.key, scenario.question, seed, window_start, anchor, truth, tables)


def _baseline_deploy_history(b: ScenarioBuilder) -> None:
    """Routine deploys over the previous week, so a deploy existing is never a giveaway."""
    summaries = [
        "Bump dependencies (security patch)",
        "Add structured logging fields",
        "Refactor request validation",
        "Increase cache TTL for product lookups",
        "Fix flaky retry in client wrapper",
        "Add metrics for p95 latency per route",
    ]
    internal = [s for s in START_VERSIONS]
    for _ in range(10):
        minute = -b.rng.uniform(3 * 60, 7 * 24 * 60)
        b.deploy(b.rng.choice(internal), minute, b.rng.choice(summaries))


def _customers(window_start: datetime) -> list[dict[str, Any]]:
    rng = random.Random(CUSTOMER_SEED)
    first = [
        "Ava",
        "Liam",
        "Noah",
        "Emma",
        "Mia",
        "Leo",
        "Zoe",
        "Omar",
        "Ines",
        "Kenji",
        "Fatima",
        "Lucas",
        "Nora",
        "Arjun",
        "Sofia",
        "Mateo",
        "Hana",
        "Ethan",
    ]
    last = [
        "Smith",
        "Garcia",
        "Kim",
        "Nguyen",
        "Muller",
        "Rossi",
        "Silva",
        "Khan",
        "Dubois",
        "Tanaka",
        "Okoro",
        "Novak",
        "Haddad",
        "Larsen",
        "Iyer",
        "Costa",
    ]
    regions = [("us-east", 0.45), ("us-west", 0.25), ("eu-west", 0.30)]
    rows = []
    for i in range(N_CUSTOMERS):
        f, l_ = rng.choice(first), rng.choice(last)
        region = rng.choices([r for r, _ in regions], [w for _, w in regions])[0]
        rows.append(
            {
                "id": f"cus_{rng.getrandbits(32):08x}",
                "name": f"{f} {l_}",
                "email": f"{f.lower()}.{l_.lower()}{i}@example.com",
                "phone": f"+1-555-01{rng.randint(0, 99):02d}",
                "tier": rng.choices(["standard", "plus", "enterprise"], [0.7, 0.25, 0.05])[0],
                "region": region,
                "created_at": window_start - timedelta(days=rng.randint(10, 900)),
            }
        )
    return rows


def _baseline_eff_err() -> dict[str, float]:
    eff: dict[str, float] = {}
    for sid in topological_order():
        ok = 1.0 - SERVICE_BY_ID[sid].base_error_rate
        for c in hard_callees(sid):
            ok *= 1.0 - eff[c]
        eff[sid] = 1.0 - ok
    return eff


def _simulate(b: ScenarioBuilder) -> dict[str, list[_Minute]]:
    rng = b.rng
    base_eff = _baseline_eff_err()
    out: dict[str, list[_Minute]] = {s.id: [] for s in SERVICES}
    order = topological_order()
    for m in range(WINDOW_MINUTES):
        surge = math.prod(s.factor(m) for s in b.surges)
        diurnal = 1.0 + 0.05 * math.sin(m / WINDOW_MINUTES * math.pi)
        for sid in order:
            spec = SERVICE_BY_ID[sid]
            load = 1.0 if sid == "paysecure-api" else surge  # health checks only
            rate = GATEWAY_BASE_RPM * spec.traffic_share * load * diurnal * rng.uniform(0.97, 1.03)
            util = rate / spec.capacity_rpm

            own_err = spec.base_error_rate * rng.uniform(0.7, 1.3)
            own_lat = spec.base_p99_ms * rng.uniform(0.92, 1.08)
            contributions: list[tuple[float, str]] = []
            for f in b.faults:
                if f.service_id != sid:
                    continue
                s = f.strength(m)
                own_err += f.error_rate * s
                own_lat += f.latency_ms * s
                if f.error_rate * s > 0:
                    contributions.append((f.error_rate * s, f.error_code))
            if util > 0.85:
                own_lat += spec.base_p99_ms * (util - 0.85) * 12
                cap_err = max(0.0, util - 1.0) * 0.4
                own_err += cap_err
                if cap_err > 0:
                    contributions.append((cap_err, "QUEUE_SATURATED"))

            ok = 1.0 - min(own_err, 1.0)
            lat = own_lat
            for c in hard_callees(sid):
                cm = out[c][m]
                ok *= 1.0 - cm.eff_err
                lat += 0.8 * max(0.0, cm.eff_lat - SERVICE_BY_ID[c].base_p99_ms)
                contributions.append((cm.eff_err - base_eff[c], cm.code))
            eff_err = min(1.0, 1.0 - ok)

            top = max(contributions, default=(0.0, ""))
            code = top[1] if top[0] > 0.005 else BASELINE_CODES[sid]
            out[sid].append(_Minute(rate, own_err, eff_err, lat, code, util))
    return out


def _bump(v: tuple[int, int, int], rng: random.Random) -> tuple[int, int, int]:
    return (v[0], v[1] + 1, 0) if rng.random() < 0.6 else (v[0], v[1], v[2] + 1)


def _deploy_rows(b: ScenarioBuilder) -> tuple[list[dict[str, Any]], list[_Log]]:
    versions = dict(START_VERSIONS)
    rows, logs = [], []
    for d in sorted(b.deploys, key=lambda d: d.minute):
        prev = versions[d.service_id]
        new = _bump(prev, b.rng)
        versions[d.service_id] = new
        prev_s, new_s = "v{}.{}.{}".format(*prev), "v{}.{}.{}".format(*new)
        rows.append(
            {
                "id": d.id,
                "service_id": d.service_id,
                "version": new_s,
                "previous_version": prev_s,
                "commit_sha": f"{b.rng.getrandbits(160):040x}",
                "author": d.author,
                "change_summary": d.summary,
                "status": "succeeded",
                "deployed_at": b.ts(d.minute),
            }
        )
        logs.append(
            _Log(
                d.minute,
                d.service_id,
                "info",
                f"Deploy {d.id}: {d.service_id} {prev_s} -> {new_s} by {d.author}: {d.summary}",
                attributes={"deploy_id": d.id, "version": new_s, "previous_version": prev_s},
                kind="deploy",
            )
        )
    return rows, logs


def _config_rows(b: ScenarioBuilder) -> tuple[list[dict[str, Any]], list[_Log]]:
    rows, logs = [], []
    for c in sorted(b.config_changes, key=lambda c: c.minute):
        rows.append(
            {
                "id": c.id,
                "service_id": c.service_id,
                "kind": c.kind,
                "key": c.key,
                "old_value": c.old,
                "new_value": c.new,
                "changed_by": c.by,
                "changed_at": b.ts(c.minute),
            }
        )
        logs.append(
            _Log(
                c.minute,
                c.service_id,
                "info",
                f"Config change {c.id} on {c.service_id}: {c.kind} {c.key} "
                f"{c.old!r} -> {c.new!r} by {c.by}",
                attributes={"config_change_id": c.id, "key": c.key},
                kind="config_change",
            )
        )
    return rows, logs


def _metric_rows(b: ScenarioBuilder, sim: dict[str, list[_Minute]]) -> list[dict[str, Any]]:
    rows = []
    for sid, minutes in sim.items():
        for m, mm in enumerate(minutes):
            ts = b.ts(m)
            rows.append(
                {"service_id": sid, "name": "request_rate", "ts": ts, "value": round(mm.rate, 1)}
            )
            rows.append(
                {"service_id": sid, "name": "error_rate", "ts": ts, "value": round(mm.eff_err, 5)}
            )
            rows.append(
                {
                    "service_id": sid,
                    "name": "p99_latency_ms",
                    "ts": ts,
                    "value": round(mm.eff_lat, 1),
                }
            )
    return rows


def _alert_logs(sim: dict[str, list[_Minute]]) -> list[_Log]:
    """Threshold alerts with a 3-minute 'for' clause, like a Prometheus rule."""
    base_eff = _baseline_eff_err()
    logs = []
    for sid, minutes in sim.items():
        spec = SERVICE_BY_ID[sid]
        if spec.kind == "external":
            continue
        err_threshold = max(4 * base_eff[sid], 0.02)
        lat_threshold = 2.5 * spec.base_p99_ms
        for metric, threshold, severity in (
            ("error_rate", err_threshold, "critical"),
            ("p99_latency_ms", lat_threshold, "warning"),
        ):
            streak, firing = 0, False
            for m, mm in enumerate(minutes):
                value = mm.eff_err if metric == "error_rate" else mm.eff_lat
                breached = value > threshold
                streak = streak + 1 if breached == (not firing) else 0
                if streak >= 3:
                    firing, streak = not firing, 0
                    shown = f"{value:.1%}" if metric == "error_rate" else f"{value:.0f}ms"
                    limit = f"{threshold:.1%}" if metric == "error_rate" else f"{threshold:.0f}ms"
                    state = "FIRING" if firing else "RESOLVED"
                    logs.append(
                        _Log(
                            m + 0.5,
                            sid,
                            severity if firing else "info",
                            f"[{state}] {sid} {metric} {shown} (threshold {limit} for 3m)",
                            attributes={
                                "alert": f"{sid}:{metric}",
                                "state": state.lower(),
                                "value": round(value, 5),
                                "threshold": round(threshold, 5),
                            },
                            kind="alert",
                        )
                    )
    return logs


def _telemetry_logs(b: ScenarioBuilder, sim: dict[str, list[_Minute]]) -> list[_Log]:
    rng = b.rng
    base_eff = _baseline_eff_err()
    logs: list[_Log] = []
    for m in range(WINDOW_MINUTES):
        for spec in SERVICES:
            if spec.kind == "external":
                continue
            sid, mm = spec.id, sim[spec.id][m]
            hosts = host_ids(sid)
            if rng.random() < 0.25:
                tmpl = rng.choice(INFO_LOGS[spec.kind])
                msg = tmpl.format(
                    n=int(mm.rate),
                    e=int(mm.rate * mm.eff_err),
                    a=rng.randint(4, 12),
                    i=rng.randint(2, 8),
                    t=rng.choice(["orders", "payments", "stock"]),
                )
                logs.append(_Log(m + rng.random(), sid, "info", msg, rng.choice(hosts)))
            if sid in NOISE_WARNINGS and rng.random() < 0.015:
                msg = rng.choice(NOISE_WARNINGS[sid]).format(x=rng.randint(1, 999))
                logs.append(_Log(m + rng.random(), sid, "warning", msg, rng.choice(hosts)))
            if mm.util > 1.0:
                logs.append(
                    _Log(
                        m + rng.random(),
                        sid,
                        "error",
                        f"request queue saturated: inflight={int(mm.rate / 60 * 4)} "
                        f"limit={int(spec.capacity_rpm / 60 * 4)}; shedding load",
                        rng.choice(hosts),
                        {"error_code": "QUEUE_SATURATED", "utilization": round(mm.util, 2)},
                    )
                )
            elif mm.util > 0.9 and rng.random() < 0.5:
                logs.append(
                    _Log(
                        m + rng.random(),
                        sid,
                        "warning",
                        f"worker pool utilization {mm.util:.0%}",
                        rng.choice(hosts),
                        {"utilization": round(mm.util, 2)},
                    )
                )
            # Downstream failures as seen by this caller.
            for c in hard_callees(sid):
                cm = sim[c][m]
                if SERVICE_BY_ID[c].kind == "external":
                    continue
                if cm.eff_err > max(3 * base_eff[c], 0.01) and rng.random() < 0.6:
                    logs.append(
                        _Log(
                            m + rng.random(),
                            sid,
                            "error",
                            f"call to {c} failed: {cm.code}",
                            rng.choice(hosts),
                            {"error_code": cm.code, "upstream": c},
                        )
                    )

        for f in b.faults:
            s = f.strength(m)
            if s <= 0 or not f.log_messages:
                continue
            log_sid = f.log_service_id or f.service_id
            hosts = list(f.host_ids) or host_ids(log_sid)
            if f.error_rate > 0:
                expected = f.error_rate * s * sim[f.service_id][m].rate
                n = (
                    min(3, math.ceil(expected / 15))
                    if expected >= 1
                    else int(rng.random() < expected)
                )
                severity = "error"
            else:
                n = int(rng.random() < 0.3 * s)
                severity = "warning"
            for _ in range(n):
                attrs: dict[str, Any] = {"error_code": f.error_code}
                if log_sid != f.service_id:
                    attrs["upstream"] = f.service_id
                logs.append(
                    _Log(
                        m + rng.random(),
                        log_sid,
                        severity,
                        rng.choice(f.log_messages),
                        rng.choice(hosts),
                        attrs,
                    )
                )
    return logs


def _event_rows(b: ScenarioBuilder, logs: list[_Log]) -> list[dict[str, Any]]:
    rows = []
    for i, lg in enumerate(sorted(logs, key=lambda x: (x.minute, x.service_id, x.message))):
        rows.append(
            {
                "id": f"evt_{i + 1:06d}",
                "ts": b.ts(lg.minute),
                "service_id": lg.service_id,
                "host_id": lg.host_id,
                "kind": lg.kind,
                "severity": lg.severity,
                "message": lg.message,
                "attributes": lg.attributes,
            }
        )
    return rows


def _commerce_rows(
    b: ScenarioBuilder, sim: dict[str, list[_Minute]], customers: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    rng = b.rng
    base_eff = _baseline_eff_err()
    region_share = {
        r: sum(c["region"] == r for c in customers) / len(customers)
        for r in {c["region"] for c in customers}
    }
    orders: list[dict[str, Any]] = []
    payments: list[dict[str, Any]] = []
    shipments: list[dict[str, Any]] = []
    checkout_faults = [f for f in b.faults if f.service_id == "checkout-svc"]

    for m in range(WINDOW_MINUTES):
        n_orders = round(sim["checkout-svc"][m].rate / 15 * rng.uniform(0.85, 1.15))
        for _ in range(n_orders):
            cust = rng.choice(customers)
            ts = b.ts(m + rng.random())
            order_id = f"ord_{rng.getrandbits(40):010x}"
            amount = rng.randint(1500, 25000)
            reason: str | None = None

            def fails(sid: str, m: int = m) -> bool:
                return rng.random() < sim[sid][m].eff_err

            # Checkout's own failures, with region-scoped faults applied per customer.
            own = SERVICE_BY_ID["checkout-svc"].base_error_rate
            own_code = BASELINE_CODES["checkout-svc"]
            for f in checkout_faults:
                p = f.error_rate * f.strength(m)
                if f.customer_region is not None:
                    p = (
                        p / region_share[f.customer_region]
                        if cust["region"] == f.customer_region
                        else 0.0
                    )
                if p > 0 and rng.random() < p:
                    reason = f.error_code
                    break
            util = sim["checkout-svc"][m].util
            if reason is None and util > 1.0 and rng.random() < (util - 1.0) * 0.4:
                reason = "QUEUE_SATURATED"
            if reason is None and rng.random() < own:
                reason = own_code
            if reason is None and fails("inventory-svc"):
                reason = sim["inventory-svc"][m].code
            if reason is None and fails("orders-db"):
                reason = sim["orders-db"][m].code

            if reason is None:
                pay_id = f"pay_{rng.getrandbits(40):010x}"
                if fails("payments-svc"):
                    code = sim["payments-svc"][m].code
                    payments.append(
                        {
                            "id": pay_id,
                            "order_id": order_id,
                            "provider": "payflow",
                            "amount_cents": amount,
                            "status": "error",
                            "error_code": code,
                            "created_at": ts,
                        }
                    )
                    reason = code
                elif rng.random() < 0.03:
                    payments.append(
                        {
                            "id": pay_id,
                            "order_id": order_id,
                            "provider": "payflow",
                            "amount_cents": amount,
                            "status": "declined",
                            "error_code": "CARD_DECLINED",
                            "created_at": ts,
                        }
                    )
                    reason = "CARD_DECLINED"
                else:
                    payments.append(
                        {
                            "id": pay_id,
                            "order_id": order_id,
                            "provider": "payflow",
                            "amount_cents": amount,
                            "status": "captured",
                            "error_code": None,
                            "created_at": ts,
                        }
                    )

            orders.append(
                {
                    "id": order_id,
                    "customer_id": cust["id"],
                    "status": "failed" if reason else "completed",
                    "total_cents": amount,
                    "currency": "USD",
                    "failure_reason": reason,
                    "created_at": ts,
                }
            )

            if reason is None:
                ship_err = sim["shipping-svc"][m].eff_err
                failed = rng.random() < ship_err
                code = (
                    sim["shipping-svc"][m].code
                    if ship_err > base_eff["shipping-svc"] * 3
                    else "LABEL_CREATE_FAILED"
                )
                shipments.append(
                    {
                        "id": f"shp_{rng.getrandbits(40):010x}",
                        "order_id": order_id,
                        "carrier": "carrierx",
                        "status": "error" if failed else "label_created",
                        "error_code": code if failed else None,
                        "created_at": ts + timedelta(seconds=rng.randint(5, 50)),
                    }
                )
    return orders, payments, shipments


__all__ = [
    "Dataset",
    "ExpectedAction",
    "Fault",
    "GroundTruth",
    "Scenario",
    "ScenarioBuilder",
    "Surge",
    "WINDOW_MINUTES",
    "generate",
]
