"""A scripted stand-in for the LLM: a fixed but data-driven investigation of the
bad_deploy_payments scenario. It reads real tool results, so the loop, MCP transport,
RBAC and grounding are exercised end to end without an API key."""

from __future__ import annotations

import json
import re
from typing import Any

from opsintel.llm import Message, ToolCall, ToolSpec


def _results(messages: list[Message], memory: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Parsed tool results in order. With `memory`, results seen earlier are remembered by
    call ID, so the script still knows them after the agent compacts its context (a real
    model carries them in its own reasoning)."""
    out = []
    for m in messages:
        if memory is not None and m.tool_call_id in memory:
            out.append(memory[m.tool_call_id])
            continue
        if m.role == "tool" and m.content and not m.content.startswith("ERROR"):
            body = re.search(r">\n(.*)\n</tool_result>", m.content, re.S)
            if body:
                try:
                    parsed = json.loads(body.group(1))
                except json.JSONDecodeError:
                    parsed = {}
                out.append(parsed)
                if memory is not None and m.tool_call_id:
                    memory[m.tool_call_id] = parsed
    return out


class ScriptedSRE:
    def __init__(self, hallucinate: bool = False, propose: bool = True) -> None:
        self.hallucinate = hallucinate
        self.propose = propose
        self.memory: dict[str, Any] = {}

    def __call__(self, messages: list[Message], tools: list[ToolSpec] | None) -> Message:
        if messages[-1].role == "system" and "JSON Schema" in (messages[-1].content or ""):
            return Message(role="assistant", content=json.dumps(self._report(messages)))
        done = sum(m.role == "tool" for m in messages)
        results = _results(messages, self.memory)
        plan: list[tuple[str, dict[str, Any]]] = [
            (
                "query_events",
                {"min_severity": "error", "since_minutes": 30, "group_by": "error_code"},
            ),
            ("list_changes", {"since_minutes": 120}),
            ("search_docs", {"query": "payments-svc PAYFLOW_POOL_EXHAUSTED pool exhausted"}),
        ]
        if self.propose:
            plan.append(("propose_action", {}))
        if done >= len(plan):
            return Message(role="assistant", content="The payments deploy is the cause.")
        name, args = plan[done]
        if name == "propose_action":
            culprit = self._culprit(results) or "dep_unknown"
            args = {
                "type": "rollback_deploy",
                "target": culprit,
                "rationale": "Pool exhaustion errors started right after this deploy.",
                "evidence_ids": [culprit, results[0]["groups"][0]["sample_event_id"]],
            }
        call = ToolCall(id=f"call_{done}", name=name, arguments=args)
        return Message(role="assistant", content=None, tool_calls=[call])

    @staticmethod
    def _changes(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return next((r["changes"] for r in results if "changes" in r), [])

    def _culprit(self, results: list[dict[str, Any]]) -> str | None:
        return next(
            (str(c["id"]) for c in self._changes(results) if c["service"] == "payments-svc"), None
        )

    def _report(self, messages: list[Message]) -> dict[str, Any]:
        # Cite only what the tools actually returned, as the real prompt demands.
        results = _results(messages, self.memory)
        culprit = self._culprit(results)
        herring = next((c["id"] for c in self._changes(results) if c["id"] != culprit), None)
        groups = results[0].get("groups", []) if results else []
        event = groups[0]["sample_event_id"] if groups else None
        chunk = next((r["results"][0]["chunk_id"] for r in results if r.get("results")), None)
        proposal = next((r["proposal_id"] for r in results if "proposal_id" in r), None)
        evidence = [
            {"id": i, "claim": claim}
            for i, claim in [
                (event, "PAYFLOW_POOL_EXHAUSTED dominates payments errors"),
                (culprit, "payments-svc deploy two minutes before onset"),
                (chunk, "runbook: pool exhaustion follows client changes"),
            ]
            if i
        ]
        if self.hallucinate:
            evidence.append({"id": "evt_999999", "claim": "made up"})
        return {
            "summary": "A payments-svc deploy shrank the payflow pool; roll it back.",
            "root_cause": {
                "description": "bad deploy",
                "kind": "deploy",
                "entity_id": culprit,
                "confidence": 0.9,
            },
            "hypotheses": [
                {
                    "statement": "payments deploy",
                    "status": "supported",
                    "evidence_ids": [i for i in (culprit, event) if i],
                },
                {
                    "statement": "shipping deploy",
                    "status": "rejected",
                    "evidence_ids": [herring] if herring else [],
                },
            ],
            "evidence": evidence,
            "recommended_actions": [
                {
                    "type": "rollback_deploy",
                    "target": culprit,
                    "rationale": "runbook",
                    "evidence_ids": [culprit],
                    "proposal_id": proposal,
                }
            ]
            if culprit
            else [],
            "affected_services": ["payments-svc", "checkout-svc"],
        }
