"""Retrieval eval: does hybrid search put the scenario's runbook near the top? Runs without
an LLM, so it gates every CI build."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from opsintel.rag.embeddings import Embedder
from opsintel.rag.search import hybrid_search
from opsintel.simulator import SCENARIOS, Dataset, generate


def evidence_query(ds: Dataset) -> str:
    """What an investigator searches after triage: the culprit service, its dominant error
    code and its most frequent error message."""
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


def _rank(session: Session, embedder: Embedder, query: str, relevant: set[str]) -> int | None:
    hits = hybrid_search(session, embedder, query, roles=["responder"], k=10)
    docs: list[str] = []
    for h in hits:
        if h.document_id not in docs:
            docs.append(h.document_id)
    return next((i + 1 for i, d in enumerate(docs) if d in relevant), None)


def retrieval_eval(session: Session, embedder: Embedder, seed: int = 42) -> dict[str, Any]:
    anchor = datetime(2026, 1, 1, 12, tzinfo=UTC)
    per_case: list[dict[str, Any]] = []
    for key, scenario in SCENARIOS.items():
        ds = generate(scenario, seed, anchor)
        relevant = {f"doc_{s}" for s in ds.ground_truth.relevant_runbooks}
        if not relevant:
            continue
        per_case.append(
            {
                "scenario": key,
                "evidence_rank": _rank(session, embedder, evidence_query(ds), relevant),
                "symptom_rank": _rank(session, embedder, scenario.question, relevant),
            }
        )

    def recall(field: str, k: int) -> float:
        return sum(1 for c in per_case if c[field] and c[field] <= k) / len(per_case)

    def mrr(field: str) -> float:
        return sum(1.0 / int(c[field]) for c in per_case if c[field]) / len(per_case)

    return {
        "embedder": embedder.name,
        "cases": per_case,
        "evidence_recall_at_1": recall("evidence_rank", 1),
        "evidence_recall_at_3": recall("evidence_rank", 3),
        "evidence_mrr": round(mrr("evidence_rank"), 3),
        "symptom_recall_at_5": recall("symptom_rank", 5),
        "symptom_mrr": round(mrr("symptom_rank"), 3),
    }
