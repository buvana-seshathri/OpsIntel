"""Runs eval cases end to end: load the scenario, investigate as the case's role through
the real service (MCP, RBAC, audit), then score against ground truth."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from opsintel.agent.investigator import AgentConfig
from opsintel.audit import ToolCallAuditor
from opsintel.auth import Principal
from opsintel.db.models import Investigation
from opsintel.evals.cases import EvalCase
from opsintel.evals.metrics import CaseScore, score_case
from opsintel.graph.build import rebuild_graph
from opsintel.investigations import InvestigationService
from opsintel.llm import LLMClient
from opsintel.rag.embeddings import Embedder
from opsintel.rag.ingest import ingest_corpus
from opsintel.simulator import generate
from opsintel.simulator.loader import load
from opsintel.tools import ToolRunner

SessionFactory = Callable[[], AbstractContextManager[Session]]


async def run_cases(
    cases: list[EvalCase],
    llm_factory: Callable[[], LLMClient],
    session_factory: SessionFactory,
    embedder: Embedder,
    config: AgentConfig | None = None,
    seed: int = 42,
    on_case: Callable[[CaseScore], None] | None = None,
) -> list[CaseScore]:
    with session_factory() as s:
        ingest_corpus(s, embedder)
    runner = ToolRunner(
        session_factory, lambda: embedder, observers=[ToolCallAuditor(session_factory)]
    )
    scores = []
    for case in cases:
        ds = generate(case.scenario, seed, datetime.now(UTC))
        with session_factory() as s:
            load(s, ds)
            rebuild_graph(s)
        svc = InvestigationService(
            runner, llm_factory, config=config, session_factory=session_factory
        )
        inv_id = svc.start(case.scenario.question, Principal(f"eval-{case.role}", case.role))
        await svc.wait_all()
        with session_factory() as s:
            inv = s.get(Investigation, inv_id)
            assert inv is not None
            score = score_case(s, case, ds.ground_truth, inv)
        scores.append(score)
        if on_case:
            on_case(score)
    return scores
