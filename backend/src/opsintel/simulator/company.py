"""Static description of the simulated company: teams, services, topology, hosts.

Numbers are per-minute baselines tuned so the healthy system sits at 40-60% capacity
and checkout's effective error rate is about 1%, which makes "8x" a realistic symptom.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TeamSpec:
    id: str
    name: str
    slack_channel: str
    oncall_handle: str


@dataclass(frozen=True)
class ServiceSpec:
    id: str
    kind: str  # internal | database | external
    tier: int
    owner_team: str | None
    description: str
    traffic_share: float  # calls per minute relative to api-gateway requests
    capacity_rpm: float
    base_error_rate: float
    base_p99_ms: float
    hosts: int = 0


TEAMS = [
    TeamSpec("checkout-team", "Checkout", "#team-checkout", "priya.raman"),
    TeamSpec("payments-team", "Payments", "#team-payments", "dmitri.volkov"),
    TeamSpec("fulfillment-team", "Fulfillment", "#team-fulfillment", "grace.mensah"),
    TeamSpec("platform-team", "Platform", "#team-platform", "lucas.ferreira"),
]

GATEWAY_BASE_RPM = 1000.0

SERVICES = [
    ServiceSpec(
        "api-gateway",
        "internal",
        1,
        "platform-team",
        "Public edge. Routes storefront and mobile traffic to backend services.",
        1.0,
        4000,
        0.002,
        80,
        hosts=3,
    ),
    ServiceSpec(
        "checkout-svc",
        "internal",
        1,
        "checkout-team",
        "Orchestrates checkout: reserves stock, computes tax, charges payment, "
        "writes the order and requests a shipping label.",
        0.3,
        900,
        0.004,
        250,
        hosts=3,
    ),
    ServiceSpec(
        "payments-svc",
        "internal",
        1,
        "payments-team",
        "Charges cards through the primary processor (payflow) with paysecure "
        "as a standby processor.",
        0.3,
        1000,
        0.003,
        300,
        hosts=3,
    ),
    ServiceSpec(
        "inventory-svc",
        "internal",
        1,
        "fulfillment-team",
        "Stock levels and reservations.",
        0.6,
        1100,
        0.002,
        60,
        hosts=3,
    ),
    ServiceSpec(
        "shipping-svc",
        "internal",
        2,
        "fulfillment-team",
        "Creates shipping labels with the carrier over mTLS. Called "
        "asynchronously after an order is written.",
        0.3,
        1200,
        0.003,
        200,
        hosts=2,
    ),
    ServiceSpec(
        "orders-db",
        "database",
        1,
        "platform-team",
        "Postgres cluster holding orders and payments.",
        0.9,
        5000,
        0.0005,
        15,
        hosts=2,
    ),
    ServiceSpec(
        "inventory-db",
        "database",
        1,
        "platform-team",
        "Postgres cluster holding stock levels.",
        0.6,
        2000,
        0.0005,
        12,
        hosts=2,
    ),
    ServiceSpec(
        "payflow-api",
        "external",
        1,
        None,
        "Primary third-party card processor.",
        0.3,
        1e9,
        0.002,
        350,
    ),
    ServiceSpec(
        "paysecure-api",
        "external",
        2,
        None,
        "Standby third-party card processor; receives health checks only.",
        0.01,
        1e9,
        0.002,
        400,
    ),
    ServiceSpec(
        "carrier-api",
        "external",
        2,
        None,
        "Shipping carrier label API (mTLS client certificate auth).",
        0.3,
        1e9,
        0.002,
        400,
    ),
]

# (caller, callee, criticality). Hard dependencies fail the caller's request;
# soft ones are async or have a fallback, so they never propagate errors.
DEPENDENCIES = [
    ("api-gateway", "checkout-svc", "hard"),
    ("checkout-svc", "payments-svc", "hard"),
    ("checkout-svc", "inventory-svc", "hard"),
    ("checkout-svc", "orders-db", "hard"),
    ("checkout-svc", "shipping-svc", "soft"),
    ("payments-svc", "payflow-api", "hard"),
    ("payments-svc", "paysecure-api", "soft"),
    ("payments-svc", "orders-db", "hard"),
    ("inventory-svc", "inventory-db", "hard"),
    ("shipping-svc", "carrier-api", "hard"),
]

AZS = ["us-east-1a", "us-east-1b", "us-east-1c"]

ENGINEERS = ["alice.ng", "raj.patel", "mei.chen", "tom.okafor", "sara.lindqvist", "jonas.berg"]

SERVICE_BY_ID = {s.id: s for s in SERVICES}


def host_ids(service_id: str) -> list[str]:
    return [f"{service_id}-{i:02d}" for i in range(1, SERVICE_BY_ID[service_id].hosts + 1)]


def hard_callees(service_id: str) -> list[str]:
    return [callee for caller, callee, c in DEPENDENCIES if caller == service_id and c == "hard"]


def callers_of(service_id: str) -> list[str]:
    return [caller for caller, callee, _ in DEPENDENCIES if callee == service_id]


def topological_order() -> list[str]:
    """Callees before callers, so propagation can read finished downstream values."""
    order: list[str] = []
    seen: set[str] = set()

    def visit(sid: str) -> None:
        if sid in seen:
            return
        seen.add(sid)
        for callee in hard_callees(sid):
            visit(callee)
        order.append(sid)

    for s in SERVICES:
        visit(s.id)
    return order
