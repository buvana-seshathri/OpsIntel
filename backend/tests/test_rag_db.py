import os
import shutil
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, delete, func, select
from sqlalchemy.orm import Session

from opsintel.db.models import Chunk, Document
from opsintel.rag.documents import CORPUS_DIR
from opsintel.rag.embeddings import HashingEmbedder
from opsintel.rag.ingest import ingest_corpus
from opsintel.rag.search import EmbeddingModelMismatch, hybrid_search
from opsintel.simulator import SCENARIOS, Dataset
from tests.test_simulator import DATASETS

pytestmark = pytest.mark.db
EMB = HashingEmbedder()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine) as s:
        s.execute(delete(Document))
        ingest_corpus(s, EMB)
        s.commit()
        yield s


def evidence_query(ds: Dataset) -> str:
    """What an investigator searches after triage: the culprit service, its dominant
    error code and the most frequent error message."""
    gt = ds.ground_truth
    assert gt.fault_start is not None
    events = [
        e
        for e in ds.tables["events"]
        if e["ts"] >= gt.fault_start
        and e["kind"] == "log"
        and e["severity"] in ("warning", "error", "critical")
        and gt.culprit_service in (e["service_id"], e["attributes"].get("upstream"))
    ]
    codes = Counter(
        e["attributes"]["error_code"] for e in events if "error_code" in e["attributes"]
    )
    message = Counter(e["message"] for e in events).most_common(1)[0][0]
    return f"{gt.culprit_service} {codes.most_common(1)[0][0] if codes else ''} {message}"


def test_ingest_is_idempotent(session: Session) -> None:
    again = ingest_corpus(session, EMB)
    assert again.added == again.updated == again.removed == 0
    assert again.unchanged == len(list(CORPUS_DIR.glob("*.md")))


def test_ingest_updates_and_prunes(engine: Engine, tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    shutil.copytree(CORPUS_DIR, corpus)
    with Session(engine) as s, s.begin():
        s.execute(delete(Document))
        ingest_corpus(s, EMB, corpus)
    (corpus / "oncall-handbook.md").unlink()
    runbook = corpus / "memory-leak.md"
    runbook.write_text(runbook.read_text() + "\n## Extra\nHeap dumps live in S3.\n")
    with Session(engine) as s, s.begin():
        report = ingest_corpus(s, EMB, corpus)
        assert (report.updated, report.removed) == (1, 1)
        assert s.get(Document, "doc_oncall-handbook") is None
        assert (
            s.scalar(
                select(func.count()).select_from(Chunk).where(Chunk.content.contains("Heap dumps"))
            )
            == 1
        )


def test_viewer_never_sees_restricted_documents(session: Session) -> None:
    query = "card data logging incident customer emails"
    viewer = hybrid_search(session, EMB, query, roles=["viewer"], k=20)
    admin = hybrid_search(session, EMB, query, roles=["admin"], k=5)
    restricted = {"doc_postmortem-2024-09-card-data-incident", "doc_customer-data-access-policy"}
    assert not restricted & {h.document_id for h in viewer}
    assert admin[0].document_id in restricted
    assert hybrid_search(session, EMB, query, roles=[], k=5) == []


def test_kind_filter(session: Session) -> None:
    hits = hybrid_search(
        session, EMB, "payflow connection pool", ["responder"], kinds=["postmortem"]
    )
    assert hits and {h.kind for h in hits} == {"postmortem"}


@pytest.mark.parametrize(
    "key", [k for k in SCENARIOS if DATASETS[k].ground_truth.relevant_runbooks]
)
def test_evidence_query_retrieves_relevant_runbook(session: Session, key: str) -> None:
    ds = DATASETS[key]
    relevant = {f"doc_{slug}" for slug in ds.ground_truth.relevant_runbooks}
    hits = hybrid_search(session, EMB, evidence_query(ds), ["responder"], k=5)
    assert relevant & {h.document_id for h in hits[:3]}, [h.chunk_id for h in hits]


def test_symptom_questions_mostly_retrieve_runbook(session: Session) -> None:
    """Raw symptom questions are vague on purpose; the agent is expected to search again
    after triage. Guard against regressions, not perfection."""
    keys = [k for k in SCENARIOS if DATASETS[k].ground_truth.relevant_runbooks]
    found = 0
    for key in keys:
        relevant = {f"doc_{s}" for s in DATASETS[key].ground_truth.relevant_runbooks}
        hits = hybrid_search(session, EMB, SCENARIOS[key].question, ["responder"], k=5)
        found += bool(relevant & {h.document_id for h in hits})
    assert found / len(keys) >= 0.75


def test_refuses_mismatched_embedder(session: Session) -> None:
    class Other(HashingEmbedder):
        name = "other-model"

    with pytest.raises(EmbeddingModelMismatch):
        hybrid_search(session, Other(), "anything", ["responder"])


@pytest.mark.skipif(
    not os.environ.get("RUN_FASTEMBED"),
    reason="downloads a model from HuggingFace; set RUN_FASTEMBED=1 (CI does)",
)
def test_real_embedder_retrieval(engine: Engine) -> None:
    from opsintel.rag.embeddings import FastEmbedEmbedder

    real = FastEmbedEmbedder()
    with Session(engine) as s, s.begin():
        s.execute(delete(Document))
        ingest_corpus(s, real)
        keys = [k for k in SCENARIOS if DATASETS[k].ground_truth.relevant_runbooks]
        for key in keys:
            relevant = {f"doc_{x}" for x in DATASETS[key].ground_truth.relevant_runbooks}
            hits = hybrid_search(s, real, evidence_query(DATASETS[key]), ["responder"], k=5)
            assert relevant & {h.document_id for h in hits}, key
        s.execute(delete(Document))
