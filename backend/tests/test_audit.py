import threading
from collections import Counter
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from opsintel import actions
from opsintel.audit import ToolCallAuditor, append, verify_chain
from opsintel.auth import Principal
from opsintel.context import AuditContext, audit_context
from opsintel.db.models import ActionProposal, AuditRecord, Deploy
from opsintel.investigations import InvestigationService
from opsintel.llm import MockLLM
from opsintel.rag.embeddings import HashingEmbedder
from opsintel.replay import replay_investigation
from opsintel.simulator import SCENARIOS, generate
from opsintel.simulator.loader import load
from opsintel.tools import ToolDenied, ToolRunner
from tests.conftest import ANCHOR
from tests.scripted_sre import ScriptedSRE
from tests.test_tools import DS, load_world, session_factory

pytestmark = pytest.mark.db
RITA = Principal("rita", "responder")
BOB = Principal("bob", "responder")
VIC = Principal("vic", "viewer")


@pytest.fixture
def factory(engine: Engine) -> Any:
    return session_factory(engine)


@pytest.fixture
def runner(engine: Engine, factory: Any) -> ToolRunner:
    load_world(engine)
    return ToolRunner(factory, HashingEmbedder, observers=[ToolCallAuditor(factory)])


@pytest.fixture
def clean_audit(engine: Engine) -> Iterator[None]:
    """Superuser-only escape hatch so tamper tests can leave a clean chain behind."""
    yield
    with engine.begin() as c:
        c.execute(text("ALTER TABLE audit_log DISABLE TRIGGER USER"))
        c.execute(text("DELETE FROM audit_log"))
        c.execute(text("ALTER TABLE audit_log ENABLE TRIGGER USER"))


def test_chain_verifies_and_detects_tampering(
    engine: Engine, factory: Any, clean_audit: None
) -> None:
    with factory() as s:
        for i in range(5):
            append(s, "note", "alice", "admin", {"i": i})
    with factory() as s:
        report = verify_chain(s)
        assert report.ok and report.records >= 5
        seqs = list(s.scalars(select(AuditRecord.seq).order_by(AuditRecord.seq)))

    # Simulate someone with superuser access bypassing the trigger.
    with engine.begin() as c:
        c.execute(text("ALTER TABLE audit_log DISABLE TRIGGER USER"))
        c.execute(
            text("UPDATE audit_log SET payload = '{\"i\": 99}' WHERE seq = :s"), {"s": seqs[-3]}
        )
        c.execute(text("ALTER TABLE audit_log ENABLE TRIGGER USER"))
    with factory() as s:
        r = verify_chain(s)
        assert not r.ok and r.first_bad_seq == seqs[-3] and "modified" in (r.reason or "")

    with engine.begin() as c:
        c.execute(text("ALTER TABLE audit_log DISABLE TRIGGER USER"))
        c.execute(text("DELETE FROM audit_log WHERE seq = :s"), {"s": seqs[-3]})
        c.execute(text("ALTER TABLE audit_log ENABLE TRIGGER USER"))
    with factory() as s:
        r = verify_chain(s)
        assert not r.ok and r.first_bad_seq == seqs[-2] and "removed" in (r.reason or "")


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_log SET actor = 'mallory'",
        "DELETE FROM audit_log",
        "TRUNCATE audit_log",
    ],
)
def test_database_refuses_rewrites(engine: Engine, factory: Any, statement: str) -> None:
    with factory() as s:
        append(s, "note", "alice", "admin", {})
    with pytest.raises(DBAPIError, match="append-only"), engine.begin() as c:
        c.execute(text(statement))


def test_concurrent_appends_keep_one_chain(factory: Any) -> None:
    def writer(n: int) -> None:
        for i in range(15):
            with factory() as s:
                append(s, "note", f"w{n}", "admin", {"i": i})

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with factory() as s:
        assert verify_chain(s).ok


def test_every_tool_call_is_audited(runner: ToolRunner, factory: Any) -> None:
    token = audit_context.set(AuditContext("inv_test", "m", "pv1"))
    try:
        runner.call("list_services", {}, VIC)
        with pytest.raises(ToolDenied):
            runner.call("get_customer", {"customer_id": "cus_x"}, VIC)
    finally:
        audit_context.reset(token)
    with factory() as s:
        rows = s.scalars(
            select(AuditRecord)
            .where(AuditRecord.investigation_id == "inv_test")
            .order_by(AuditRecord.seq)
        ).all()
        ok, denied = rows[-2:]
        assert (ok.payload["tool"], ok.payload["ok"], ok.actor) == ("list_services", True, "vic")
        assert len(ok.payload["result_digest"]) == 64 and ok.payload["prompt_version"] == "pv1"
        assert denied.payload["ok"] is False and "read:pii" in denied.payload["error"]


async def _investigate(runner: ToolRunner, factory: Any) -> str:
    svc = InvestigationService(
        runner, lambda: MockLLM(ScriptedSRE(), model="scripted"), session_factory=factory
    )
    inv_id = svc.start(DS.question, RITA)
    await svc.wait_all()
    return inv_id


async def test_investigation_leaves_a_complete_trail(runner: ToolRunner, factory: Any) -> None:
    inv_id = await _investigate(runner, factory)
    with factory() as s:
        kinds = Counter(
            s.scalars(select(AuditRecord.kind).where(AuditRecord.investigation_id == inv_id))
        )
        proposal = s.scalars(
            select(ActionProposal).where(ActionProposal.investigation_id == inv_id)
        ).one()
        assert verify_chain(s).ok
        assert proposal.proposed_by == "rita"
    assert kinds["investigation_started"] == kinds["investigation_completed"] == 1
    assert kinds["tool_call"] == 4 and kinds["llm_call"] >= 5


async def test_two_person_approval(runner: ToolRunner, factory: Any) -> None:
    inv_id = await _investigate(runner, factory)
    with factory() as s:
        proposal_id = s.scalars(
            select(ActionProposal.id).where(ActionProposal.investigation_id == inv_id)
        ).one()
    with pytest.raises(actions.Forbidden, match="own investigation"), factory() as s:
        actions.decide(s, RITA, proposal_id, approve=True)
    with pytest.raises(actions.Forbidden), factory() as s:
        actions.decide(s, VIC, proposal_id, approve=True)

    with factory() as s:
        done = actions.decide(s, BOB, proposal_id, approve=True, comment="matches runbook")
        assert done.status == "executed" and "Rolled back payments-svc" in (
            done.execution_result or ""
        )
        assert s.get(Deploy, done.target).status == "rolled_back"  # type: ignore[union-attr]
    with pytest.raises(actions.InvalidTransition), factory() as s:
        actions.decide(s, BOB, proposal_id, approve=False)
    with factory() as s:
        kinds = [
            r.kind
            for r in s.scalars(
                select(AuditRecord)
                .where(AuditRecord.investigation_id == inv_id)
                .order_by(AuditRecord.seq)
            )
        ]
        assert kinds[-2:] == ["action_decision", "action_executed"]
        assert verify_chain(s).ok


async def test_replay_reproduces_then_detects_changed_data(
    runner: ToolRunner, factory: Any, engine: Engine
) -> None:
    inv_id = await _investigate(runner, factory)
    same = replay_investigation(factory, runner, inv_id)
    assert same["calls"] == 4
    assert (same["reproduced"], same["differs"], same["skipped (write)"]) == (3, 0, 1)

    # Different seed: same scenario shape, different events, so evidence no longer matches.
    with Session(engine) as s, s.begin():
        load(s, generate(SCENARIOS["bad_deploy_payments"], 7, ANCHOR))
    changed = replay_investigation(factory, runner, inv_id)
    assert changed["differs"] >= 1
