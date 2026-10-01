"""The investigation loop: an LLM with tools, reached through a real MCP client session.

The agent holds no database handle and no credentials of its own. It talks to the OpsIntel
MCP server, which executes every call as the human who started the investigation, so the
agent can never read more than that person could.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from mcp import Client

from opsintel.agent.prompts import FINAL_REPORT_PROMPT, PROMPT_VERSION, SYSTEM_PROMPT
from opsintel.agent.report import Grounding, InvestigationReport, check_grounding, ids_in
from opsintel.auth import Principal
from opsintel.llm import LLMClient, LLMResponse, Message, ToolSpec, Usage, complete_structured
from opsintel.tools import REGISTRY

EventType = Literal[
    "started",
    "llm_call",
    "thought",
    "tool_call",
    "tool_result",
    "tool_error",
    "report",
    "failed",
]


@dataclass
class TraceEvent:
    seq: int
    type: EventType
    ts: datetime
    data: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "type": self.type, "ts": self.ts.isoformat(), "data": self.data}


@dataclass
class InvestigationResult:
    report: InvestigationReport
    grounding: Grounding
    trace: list[TraceEvent]
    usage: Usage
    tool_calls: int
    llm_calls: int
    latency_ms: float
    model: str
    stop_reason: Literal["concluded", "tool_budget"]


@dataclass
class AgentConfig:
    max_tool_calls: int = 12
    tool_result_chars: int = 3000
    # Prompt tokens allowed per model call, tool definitions included. Groq's free tier
    # refuses any single request above 8,000 tokens per minute (prompt + completion).
    context_token_budget: int = 5000
    chars_per_token: float = 3.2  # conservative for JSON-heavy text
    keep_recent_results: int = 2  # never compact the newest tool results


def _compact_schema(schema: Any) -> Any:
    """Drop JSON-schema noise the model does not need (titles), to save tokens."""
    if isinstance(schema, dict):
        return {k: _compact_schema(v) for k, v in schema.items() if k != "title"}
    if isinstance(schema, list):
        return [_compact_schema(v) for v in schema]
    return schema


@dataclass
class _Run:
    on_event: Callable[[TraceEvent], None] | None
    trace: list[TraceEvent] = field(default_factory=list)
    seen_ids: set[str] = field(default_factory=set)
    usage: Usage = field(default_factory=Usage)
    llm_calls: int = 0

    def emit(self, type: EventType, **data: Any) -> None:
        event = TraceEvent(len(self.trace) + 1, type, datetime.now(UTC), data)
        self.trace.append(event)
        if self.on_event:
            self.on_event(event)

    def count(self, resp: LLMResponse, purpose: str) -> None:
        usage = resp.usage
        self.emit(
            "llm_call",
            model=resp.model,
            purpose=purpose,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            latency_ms=round(resp.latency_ms, 1),
        )
        self.llm_calls += 1
        self.usage = Usage(
            prompt_tokens=self.usage.prompt_tokens + usage.prompt_tokens,
            completion_tokens=self.usage.completion_tokens + usage.completion_tokens,
        )


class Investigator:
    def __init__(
        self,
        llm: LLMClient,
        mcp_server: Any,
        principal: Principal,
        config: AgentConfig | None = None,
    ) -> None:
        """`mcp_server` is anything `mcp.Client` accepts: an in-process server built for
        this principal, a stdio command, or an HTTP URL."""
        self.llm = llm
        self.mcp_server = mcp_server
        self.principal = principal
        self.config = config or AgentConfig()

    async def run(
        self,
        question: str,
        now: datetime,
        on_event: Callable[[TraceEvent], None] | None = None,
    ) -> InvestigationResult:
        run = _Run(on_event)
        started = time.perf_counter()
        cfg = self.config
        run.emit(
            "started",
            question=question,
            subject=self.principal.subject,
            role=self.principal.role,
            model=self.llm.model,
            prompt_version=PROMPT_VERSION,
        )

        async with Client(self.mcp_server) as client:
            specs = await self._tool_specs(client)
            messages = [
                Message(
                    role="system",
                    content=SYSTEM_PROMPT.format(now=now.isoformat(), max_steps=cfg.max_tool_calls),
                ),
                Message(role="user", content=question),
            ]
            tool_calls = 0
            stop: Literal["concluded", "tool_budget"] = "concluded"
            spec_chars = sum(len(s.model_dump_json()) for s in specs)
            while True:
                if tool_calls >= cfg.max_tool_calls:
                    stop = "tool_budget"
                    break
                self._fit_context(messages, spec_chars, run)
                resp = await self.llm.complete(messages, tools=specs)
                run.count(resp, "step")
                messages.append(resp.message)
                if resp.message.content:
                    run.emit("thought", text=resp.message.content)
                if not resp.message.tool_calls:
                    break
                for call in resp.message.tool_calls:
                    tool_calls += 1
                    run.emit("tool_call", id=call.id, tool=call.name, arguments=call.arguments)
                    text = await self._execute(client, run, call.id, call.name, call.arguments)
                    messages.append(Message(role="tool", tool_call_id=call.id, content=text))

        messages.append(Message(role="user", content=FINAL_REPORT_PROMPT))
        # The report request carries the report's JSON schema instead of the tool list.
        self._fit_context(messages, len(json.dumps(InvestigationReport.model_json_schema())), run)
        report, responses = await complete_structured(
            self.llm, messages, InvestigationReport, max_repairs=2
        )
        for r in responses:
            run.count(r, "report")
        grounding = check_grounding(report, run.seen_ids)
        run.emit("report", report=report.model_dump(), grounding=grounding.model_dump())
        return InvestigationResult(
            report=report,
            grounding=grounding,
            trace=run.trace,
            usage=run.usage,
            tool_calls=tool_calls,
            llm_calls=run.llm_calls,
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
            model=self.llm.model,
            stop_reason=stop,
        )

    def _fit_context(self, messages: list[Message], fixed_chars: int, run: _Run) -> None:
        """Shrink the oldest tool results to the IDs they returned until the request fits
        the token budget. IDs survive, so the model can still cite what it saw."""
        cfg = self.config

        def size() -> int:
            chars = fixed_chars + sum(
                len(m.content or "") + sum(len(json.dumps(c.arguments)) for c in m.tool_calls)
                for m in messages
            )
            return int(chars / cfg.chars_per_token)

        tool_indexes = [i for i, m in enumerate(messages) if m.role == "tool"]
        candidates = tool_indexes[: max(0, len(tool_indexes) - cfg.keep_recent_results)]
        compacted = 0
        for i in candidates:
            if size() <= cfg.context_token_budget:
                break
            content = messages[i].content or ""
            if content.startswith("<compacted"):
                continue
            ids = sorted(ids_in(content))[:30]
            messages[i] = messages[i].model_copy(
                update={
                    "content": f'<compacted chars="{len(content)}">IDs returned: '
                    f"{', '.join(ids) or 'none'}</compacted>"
                }
            )
            compacted += 1
        if compacted:
            run.emit(
                "thought",
                text=f"[context compacted: {compacted} older tool results "
                f"reduced to their IDs, ~{size()} tokens]",
            )

    async def _tool_specs(self, client: Client) -> list[ToolSpec]:
        """Offer only the tools this principal may call. Enforcement still happens in the
        tool layer; this just saves tokens and pointless denied calls."""
        listed = (await client.list_tools()).tools
        allowed = {d.name for d in REGISTRY.values() if self.principal.can(d.permission)}
        return [
            ToolSpec(
                name=t.name,
                description=(t.description or "").split("\n\nRequires:")[0],
                parameters=_compact_schema(t.input_schema),
            )
            for t in listed
            if t.name in allowed
        ]

    async def _execute(
        self, client: Client, run: _Run, call_id: str, name: str, arguments: dict[str, Any]
    ) -> str:
        limit = self.config.tool_result_chars
        if "_unparsed" in arguments:
            error = "arguments were not valid JSON; send a JSON object"
            run.emit("tool_error", id=call_id, tool=name, error=error)
            return f"ERROR: {error}"
        result = await client.call_tool(name, arguments)
        if result.is_error:
            error = " ".join(getattr(c, "text", "") for c in result.content).strip()
            run.emit("tool_error", id=call_id, tool=name, error=error)
            return f"ERROR: {error}"
        body = json.dumps(result.structured_content, default=str, separators=(",", ":"))
        truncated = len(body) > limit
        shown = body[:limit] + ("...[truncated; narrow the query]" if truncated else "")
        ids = ids_in(shown)
        run.seen_ids |= ids
        run.emit(
            "tool_result",
            id=call_id,
            tool=name,
            chars=len(body),
            truncated=truncated,
            ids_returned=len(ids),
            preview=shown[:400],
        )
        # Mark the boundary so the model can tell data from instructions.
        return f'<tool_result tool="{name}" trust="untrusted-data">\n{shown}\n</tool_result>'
