from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, delete, func, select
from sqlalchemy.orm import Session

from opsintel.db.models import Document, Edge, Entity
from opsintel.graph.build import error_codes_in, rebuild_graph
from opsintel.graph.queries import (
    EntityNotFound,
    blast_radius,
    find_path,
    get_entity,
    trace_dependencies,
)
from opsintel.rag.embeddings import HashingEmbedder
from opsintel.rag.ingest import ingest_corpus
from opsintel.simulator.loader import load
from tests.test_simulator import DATASETS

pytestmark = pytest.mark.db
DS = DATASETS["bad_deploy_payments"]
CULPRIT = DS.ground_truth.root_cause_entity
assert CULPRIT is not None


@pytest.fixture(scope="module")
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine) as s:
        load(s, DS)
        s.execute(delete(Document))
        ingest_corpus(s, HashingEmbedder())
        rebuild_graph(s)
        s.commit()
        yield s


def test_error_code_extraction() -> None:
    assert error_codes_in("pool exhausted: PAYFLOW_POOL_EXHAUSTED; ENOSPC; HTTP 503") == {
        "PAYFLOW_POOL_EXHAUSTED",
        "ENOSPC",
    }
    assert error_codes_in("set PAYFLOW_POOL_MAX from config") == set()


def test_rebuild_is_repeatable(session: Session) -> None:
    before = session.scalar(select(func.count()).select_from(Edge))
    rebuild_graph(session)
    assert session.scalar(select(func.count()).select_from(Edge)) == before


def test_customers_carry_no_pii(session: Session) -> None:
    customer = session.scalars(select(Entity).where(Entity.type == "customer")).first()
    assert customer is not None
    assert set(customer.properties) == {"tier", "region"}
    assert "@" not in customer.name


def test_downstream_trace_marks_hard_paths(session: Session) -> None:
    rows = {r["service"]: r for r in trace_dependencies(session, "checkout-svc", max_depth=3)}
    assert rows["service:payments-svc"]["depth"] == 1
    assert rows["service:payflow-api"] == {
        "service": "service:payflow-api",
        "depth": 2,
        "hard_path": True,
        "path": ["service:checkout-svc", "service:payments-svc", "service:payflow-api"],
    }
    assert rows["service:shipping-svc"]["hard_path"] is False
    assert rows["service:carrier-api"]["hard_path"] is False  # reached through a soft hop
    assert rows["service:paysecure-api"]["hard_path"] is False


def test_upstream_trace_and_depth_limit(session: Session) -> None:
    up = trace_dependencies(session, "inventory-db", "upstream", max_depth=3)
    assert [(r["service"], r["depth"]) for r in up] == [
        ("service:inventory-svc", 1),
        ("service:checkout-svc", 2),
        ("service:api-gateway", 3),
    ]
    assert len(trace_dependencies(session, "inventory-db", "upstream", max_depth=1)) == 1
    with pytest.raises(EntityNotFound):
        trace_dependencies(session, "nope-svc")


def test_blast_radius_of_culprit_deploy(session: Session) -> None:
    br = blast_radius(session, CULPRIT)
    assert br["origin_services"] == ["service:payments-svc"]
    impacted = {i["service"]: (i["impact"], i["owner_team"]) for i in br["impacted"]}
    assert impacted == {
        "service:payments-svc": ("fails", "team:payments-team"),
        "service:checkout-svc": ("fails", "team:checkout-team"),
        "service:api-gateway": ("fails", "team:platform-team"),
    }


def test_soft_dependency_only_degrades(session: Session) -> None:
    impacted = {
        i["service"]: i["impact"] for i in blast_radius(session, "service:carrier-api")["impacted"]
    }
    assert impacted["service:shipping-svc"] == "fails"
    assert impacted["service:checkout-svc"] == "degraded"


def test_entities_extracted_from_events(session: Session) -> None:
    edge = session.scalars(
        select(Edge).where(
            Edge.src == "service:payments-svc",
            Edge.dst == "error_code:PAYFLOW_POOL_EXHAUSTED",
        )
    ).one()
    assert edge.origin == "event" and edge.properties["count"] > 10
    assert edge.properties["first_seen"] >= DS.ground_truth.fault_start.isoformat()
    observed = get_entity(session, "service:checkout-svc")["neighbors"]
    assert any(
        n["relation"] == "saw_failures_from" and n["entity_id"] == "service:payments-svc"
        for n in observed
    )


def test_documents_link_error_codes_to_runbooks(session: Session) -> None:
    mention = session.scalars(
        select(Edge).where(
            Edge.src == "doc_payments-error-rate",
            Edge.relation == "mentions",
            Edge.dst == "error_code:PAYFLOW_POOL_EXHAUSTED",
        )
    ).one()
    assert mention.properties["chunk_ids"][0].startswith("chk_payments-error-rate_")
    path = find_path(session, CULPRIT, "doc_postmortem-2025-03-payflow-pool")
    assert path is not None and len(path) <= 3
    assert path[0]["from"] == CULPRIT and path[-1]["to"] == "doc_postmortem-2025-03-payflow-pool"


def test_find_path_bounds(session: Session) -> None:
    assert find_path(session, "service:payments-svc", "service:payments-svc") == []
    assert find_path(session, CULPRIT, "doc_certificate-rotation", max_depth=1) is None
    with pytest.raises(EntityNotFound):
        find_path(session, CULPRIT, "doc_missing")
