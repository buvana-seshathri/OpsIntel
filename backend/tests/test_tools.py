import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session

from opsintel.auth import ROLE_PERMISSIONS, Principal
from opsintel.db.models import ActionProposal, Customer, Document
from opsintel.graph.build import rebuild_graph
from opsintel.rag.embeddings import HashingEmbedder
from opsintel.rag.ingest import ingest_corpus
from opsintel.simulator.loader import load
from opsintel.tools import (
    REGISTRY,
    ToolCallRecord,
    ToolDenied,
    ToolInputError,
    ToolRunner,
    UnknownTool,
)
from tests.test_simulator import DATASETS

pytestmark = pytest.mark.db
DS = DATASETS["bad_deploy_payments"]
GT = DS.ground_truth
CULPRIT = GT.root_cause_entity
VIEWER, RESPONDER, ADMIN = (Principal(r, r) for r in ("viewer", "responder", "admin"))
RESTRICTED_DOC = "doc_customer-data-access-policy"


def load_world(engine: Engine, dataset: Any = DS) -> None:
    with Session(engine) as s, s.begin():
        load(s, dataset)
        s.execute(delete(Document))
        ingest_corpus(s, HashingEmbedder())
        rebuild_graph(s)


def session_factory(engine: Engine) -> Any:
    @contextmanager
    def factory() -> Iterator[Session]:
        with Session(engine) as s, s.begin():
            yield s

    return factory


@pytest.fixture(scope="module")
def world(engine: Engine) -> Engine:
    load_world(engine)
    return engine


@pytest.fixture
def records() -> list[ToolCallRecord]:
    return []


@pytest.fixture
def runner(world: Engine, records: list[ToolCallRecord]) -> ToolRunner:
    factory: Any = session_factory(world)
    return ToolRunner(factory, HashingEmbedder, observers=[records.append])


def minimal_args(engine: Engine) -> dict[str, dict[str, Any]]:
    with Session(engine) as s:
        customer = s.scalars(select(Customer.id)).first()
        evidence = s.scalars(select(Document.id)).first()
    return {
        "list_services": {},
        "query_events": {},
        "get_metrics": {"service": "checkout-svc"},
        "list_changes": {},
        "get_entity": {"entity_id": "service:payments-svc"},
        "trace_dependencies": {"service": "checkout-svc"},
        "blast_radius": {"entity_id": "service:payments-svc"},
        "find_path": {"source": "service:checkout-svc", "target": "service:payments-svc"},
        "search_docs": {"query": "payments"},
        "query_orders": {},
        "get_customer": {"customer_id": customer},
        "propose_action": {
            "type": "page_team",
            "target": "team:payments-team",
            "rationale": "payments errors need the owning team",
            "evidence_ids": [evidence],
        },
    }


@pytest.mark.parametrize("role", ["viewer", "responder", "admin"])
def test_rbac_matrix(runner: ToolRunner, world: Engine, role: str) -> None:
    args = minimal_args(world)
    assert set(args) == set(REGISTRY), "every tool needs a case in the RBAC matrix"
    principal = Principal(f"u-{role}", role)
    for name, definition in REGISTRY.items():
        if definition.permission in ROLE_PERMISSIONS[role]:
            runner.call(name, args[name], principal)
        else:
            with pytest.raises(ToolDenied):
                runner.call(name, args[name], principal)


def test_rejects_unknown_tools_and_bad_arguments(runner: ToolRunner) -> None:
    with pytest.raises(UnknownTool):
        runner.call("drop_tables", {}, ADMIN)
    with pytest.raises(ToolInputError, match="limit"):
        runner.call("query_events", {"limit": 10_000}, VIEWER)
    with pytest.raises(ToolInputError, match="Extra inputs"):
        runner.call("query_events", {"role": "admin"}, VIEWER)  # no smuggled parameters
    with pytest.raises(ToolInputError, match="no metrics"):
        runner.call("get_metrics", {"service": "nope"}, VIEWER)


def test_observer_sees_every_call(runner: ToolRunner, records: list[ToolCallRecord]) -> None:
    runner.call("list_services", {}, VIEWER)
    with pytest.raises(ToolDenied):
        runner.call("get_customer", {"customer_id": "cus_x"}, VIEWER)
    ok, denied = records
    assert ok.ok and ok.result_digest and len(ok.result_digest) == 64
    assert not denied.ok and denied.error and denied.error.startswith("ToolDenied")
    assert (denied.subject, denied.role) == ("viewer", "viewer")


def test_events_point_at_the_culprit(runner: ToolRunner) -> None:
    grouped = runner.call(
        "query_events",
        {
            "service": "payments-svc",
            "min_severity": "error",
            "since_minutes": 30,
            "group_by": "error_code",
        },
        RESPONDER,
    )
    assert grouped["groups"][0]["error_code"] == "PAYFLOW_POOL_EXHAUSTED"
    first = runner.call(
        "query_events", {"error_code": "PAYFLOW_POOL_EXHAUSTED", "limit": 1}, RESPONDER
    )["events"][0]
    deploy = next(
        c for c in runner.call("list_changes", {}, RESPONDER)["changes"] if c["id"] == CULPRIT
    )
    assert 0 <= deploy["minutes_ago"] - first["minutes_ago"] <= 3
    assert {c["id"] for c in runner.call("list_changes", {}, RESPONDER)["changes"]} >= {
        CULPRIT,
        *GT.red_herrings,
    }


def test_metrics_summary(runner: ToolRunner) -> None:
    out = runner.call("get_metrics", {"service": "checkout-svc", "metric": "error_rate"}, VIEWER)
    summary = out["metrics"]["error_rate"]
    assert 6 <= summary["ratio"] <= 10
    assert summary["first_bucket_above_2x_baseline"] is not None


def test_orders_never_expose_pii(runner: ToolRunner, world: Engine) -> None:
    out = runner.call(
        "query_orders", {"since_minutes": 30, "group_by": "customer_region"}, RESPONDER
    )
    assert out["failed_orders"] > 0 and {g["customer_region"] for g in out["groups"]}
    with Session(world) as s:
        emails = set(s.scalars(select(Customer.email)))
    text = json.dumps(out, default=str)
    assert "@" not in text and not any(e in text for e in emails)
    customer = out["sample_orders"][0]["customer_id"]
    assert "@" in runner.call("get_customer", {"customer_id": customer}, ADMIN)["email"]


def test_restricted_documents_are_invisible_to_viewers(runner: ToolRunner) -> None:
    hits = runner.call("search_docs", {"query": "customer data access policy", "k": 10}, VIEWER)
    assert RESTRICTED_DOC not in {h["document_id"] for h in hits["results"]}
    with pytest.raises(ToolInputError):
        runner.call("get_entity", {"entity_id": RESTRICTED_DOC}, VIEWER)
    assert runner.call("get_entity", {"entity_id": RESTRICTED_DOC}, ADMIN)["type"] == "document"
    neighbors = runner.call("get_entity", {"entity_id": "service:payments-svc"}, VIEWER)
    assert RESTRICTED_DOC not in {n["entity_id"] for n in neighbors["neighbors"]}
    with pytest.raises(ToolInputError):
        runner.call(
            "find_path", {"source": "service:payments-svc", "target": RESTRICTED_DOC}, VIEWER
        )


def test_propose_action_validates_and_queues(runner: ToolRunner, world: Engine) -> None:
    evidence = runner.call(
        "query_events", {"error_code": "PAYFLOW_POOL_EXHAUSTED", "limit": 2}, RESPONDER
    )["events"]
    ids = [e["id"] for e in evidence] + [CULPRIT]
    out = runner.call(
        "propose_action",
        {
            "type": "rollback_deploy",
            "target": CULPRIT,
            "evidence_ids": ids,
            "rationale": "Pool exhaustion errors began two minutes after this deploy.",
        },
        RESPONDER,
    )
    assert out["status"] == "pending_approval"
    with Session(world) as s:
        saved = s.get(ActionProposal, out["proposal_id"])
        assert saved is not None and saved.status == "pending" and saved.proposed_by == "responder"

    with pytest.raises(ToolInputError, match="needs a deploy target"):
        runner.call(
            "propose_action",
            {
                "type": "rollback_deploy",
                "target": "service:payments-svc",
                "rationale": "roll it back now",
                "evidence_ids": ids,
            },
            RESPONDER,
        )
    with pytest.raises(ToolInputError, match="unknown evidence"):
        runner.call(
            "propose_action",
            {
                "type": "rollback_deploy",
                "target": CULPRIT,
                "rationale": "roll it back now",
                "evidence_ids": ["evt_999999", "chk_made_up_00"],
            },
            RESPONDER,
        )


def test_clock_is_the_scenario_window(runner: ToolRunner) -> None:
    out = runner.call("query_events", {"since_minutes": 5}, VIEWER)
    assert out["window"]["end"] == DS.window_end.isoformat().replace("+00:00", "Z")
