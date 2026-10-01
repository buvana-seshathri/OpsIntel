from typing import Any

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from opsintel.agent.investigator import AgentConfig, Investigator
from opsintel.auth import Principal
from opsintel.db.models import ActionProposal
from opsintel.llm import Message, MockLLM, ToolCall
from opsintel.mcp_server.server import create_server
from opsintel.rag.embeddings import HashingEmbedder
from opsintel.tools import ToolRunner
from tests.scripted_sre import ScriptedSRE
from tests.test_tools import DS, load_world, session_factory

pytestmark = pytest.mark.db
RESPONDER = Principal("rita", "responder")
VIEWER = Principal("vic", "viewer")


@pytest.fixture(scope="module")
def runner(engine: Engine) -> ToolRunner:
    load_world(engine)
    factory: Any = session_factory(engine)
    return ToolRunner(factory, HashingEmbedder)


def agent(runner: ToolRunner, who: Principal, llm: Any, **cfg: Any) -> Investigator:
    return Investigator(llm, create_server(runner, who), who, AgentConfig(**cfg))


async def test_end_to_end_investigation(runner: ToolRunner, engine: Engine) -> None:
    events: list[str] = []
    llm = MockLLM(ScriptedSRE())
    result = await agent(runner, RESPONDER, llm).run(
        DS.question, DS.window_end, on_event=lambda e: events.append(e.type)
    )

    assert result.report.root_cause.entity_id == DS.ground_truth.root_cause_entity
    assert result.grounding.unseen_ids == [] and result.grounding.ratio == 1.0
    assert result.stop_reason == "concluded" and result.tool_calls == 4
    assert events[0] == "started" and events[-1] == "report"
    assert events.count("tool_call") == events.count("tool_result") == 4
    proposal_id = result.report.recommended_actions[0].proposal_id
    with Session(engine) as s:
        proposal = s.get(ActionProposal, proposal_id)
        assert proposal is not None and proposal.status == "pending"
        assert proposal.proposed_by == "rita"  # the human, not the agent

    # The model saw tool results only inside an explicit untrusted-data boundary.
    tool_msgs = [m for m in llm.calls[-1] if m.role == "tool"]
    assert all('trust="untrusted-data"' in (m.content or "") for m in tool_msgs)


async def test_hallucinated_citation_is_flagged(runner: ToolRunner) -> None:
    result = await agent(runner, RESPONDER, MockLLM(ScriptedSRE(hallucinate=True))).run(
        DS.question, DS.window_end
    )
    assert result.grounding.unseen_ids == ["evt_999999"]
    assert result.grounding.ratio < 1.0


async def test_viewer_gets_no_write_tools_and_denials_do_not_crash(runner: ToolRunner) -> None:
    offered: list[set[str]] = []

    def script(messages: list[Message], tools: Any) -> Message:
        if messages[-1].role == "system" and "JSON Schema" in (messages[-1].content or ""):
            return Message(
                role="assistant",
                content=(
                    '{"summary":"s","root_cause":{"description":"d","kind":"unknown",'
                    '"entity_id":null,"confidence":0.1},"hypotheses":[],"evidence":[]}'
                ),
            )
        offered.append({t.name for t in tools})
        if not any(m.role == "tool" for m in messages):
            # A model that ignores the offered tool list still hits the RBAC wall.
            return Message(
                role="assistant",
                tool_calls=[
                    ToolCall(id="c1", name="get_customer", arguments={"customer_id": "cus_x"})
                ],
            )
        return Message(role="assistant", content="Stopping.")

    events: list[Any] = []
    result = await agent(runner, VIEWER, MockLLM(script)).run(
        "who are the affected customers?", DS.window_end, on_event=events.append
    )
    assert "propose_action" not in offered[0] and "get_customer" not in offered[0]
    error = next(e for e in events if e.type == "tool_error")
    assert "lacks 'read:pii'" in error.data["error"]
    assert result.report.root_cause.kind == "unknown"


async def test_tool_budget_stops_the_loop(runner: ToolRunner) -> None:
    result = await agent(runner, RESPONDER, MockLLM(ScriptedSRE()), max_tool_calls=2).run(
        DS.question, DS.window_end
    )
    assert result.stop_reason == "tool_budget" and result.tool_calls == 2
    # The budget cut the loop before propose_action, so nothing was queued.
    assert all(a.proposal_id is None for a in result.report.recommended_actions)
    assert result.grounding.unseen_ids == []


async def test_large_results_are_truncated(runner: ToolRunner) -> None:
    llm = MockLLM(ScriptedSRE(propose=False))
    result = await agent(runner, RESPONDER, llm, tool_result_chars=300).run(
        DS.question, DS.window_end
    )
    truncated = [e for e in result.trace if e.type == "tool_result" and e.data["truncated"]]
    assert truncated
    assert all(len(m.content or "") < 600 for m in llm.calls[-1] if m.role == "tool")
