"""Runs investigations in the background, streams their traces, and persists results."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from opsintel.agent.investigator import AgentConfig, Investigator, TraceEvent
from opsintel.auth import Principal
from opsintel.db.models import Investigation
from opsintel.db.session import session_scope
from opsintel.events import InMemoryEventBus
from opsintel.llm import LLMClient
from opsintel.mcp_server.server import create_server
from opsintel.tools import ToolRunner
from opsintel.tools.registry import simulated_now


def _root_error(e: BaseException) -> BaseException:
    """Task groups (the MCP client uses one) wrap failures in ExceptionGroups; report the
    first underlying error instead of the wrapper."""
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    return e


def can_view(row: Investigation, principal: Principal) -> bool:
    # Results can contain anything the creator was allowed to read, so only the creator
    # and admins may see them.
    return row.subject == principal.subject or principal.role == "admin"


def summarize(row: Investigation, include_trace: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": row.id,
        "question": row.question,
        "status": row.status,
        "subject": row.subject,
        "role": row.role,
        "model": row.model,
        "created_at": row.created_at,
        "finished_at": row.finished_at,
        "report": row.report,
        "grounding": row.grounding,
        "stats": row.stats,
        "error": row.error,
    }
    if include_trace:
        out["trace"] = row.trace
    return out


class InvestigationService:
    def __init__(
        self,
        runner: ToolRunner,
        llm_factory: Callable[[], LLMClient],
        bus: InMemoryEventBus | None = None,
        config: AgentConfig | None = None,
        session_factory: Callable[[], AbstractContextManager[Session]] = session_scope,
    ) -> None:
        self.runner = runner
        self.llm_factory = llm_factory
        self.bus = bus or InMemoryEventBus()
        self.config = config or AgentConfig()
        self.session_factory = session_factory
        self._tasks: set[asyncio.Task[None]] = set()

    def start(self, question: str, principal: Principal) -> str:
        llm = self.llm_factory()
        inv_id = f"inv_{secrets.token_hex(6)}"
        with self.session_factory() as s:
            s.add(
                Investigation(
                    id=inv_id,
                    question=question,
                    status="running",
                    subject=principal.subject,
                    role=principal.role,
                    model=llm.model,
                    created_at=datetime.now(UTC),
                    trace=[],
                )
            )
        self.bus.open(inv_id)  # subscribers arriving before the first event still follow it
        task = asyncio.create_task(self._run(inv_id, question, principal, llm))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return inv_id

    async def wait_all(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _run(self, inv_id: str, question: str, principal: Principal, llm: LLMClient) -> None:
        def publish(event: TraceEvent) -> None:
            self.bus.publish(inv_id, event.as_dict())

        with self.session_factory() as s:
            now = simulated_now(s)
        try:
            agent = Investigator(llm, create_server(self.runner, principal), principal, self.config)
            result = await agent.run(question, now, on_event=publish)
            with self.session_factory() as s:
                row = s.get(Investigation, inv_id)
                assert row is not None
                row.status = "completed"
                row.finished_at = datetime.now(UTC)
                row.report = result.report.model_dump(mode="json")
                row.grounding = {**result.grounding.model_dump(), "ratio": result.grounding.ratio}
                row.trace = [e.as_dict() for e in result.trace]
                row.stats = {
                    "prompt_tokens": result.usage.prompt_tokens,
                    "completion_tokens": result.usage.completion_tokens,
                    "tool_calls": result.tool_calls,
                    "llm_calls": result.llm_calls,
                    "latency_ms": result.latency_ms,
                    "stop_reason": result.stop_reason,
                }
        except Exception as e:  # the API must record failures, not lose them
            root = _root_error(e)
            error = f"{type(root).__name__}: {root}"
            failed = {
                "seq": 0,
                "type": "failed",
                "ts": datetime.now(UTC).isoformat(),
                "data": {"error": error},
            }
            self.bus.publish(inv_id, failed)
            with self.session_factory() as s:
                row = s.get(Investigation, inv_id)
                if row is not None:
                    row.status = "failed"
                    row.finished_at = datetime.now(UTC)
                    row.error = error
        finally:
            self.bus.close(inv_id)

    def get(self, inv_id: str) -> Investigation | None:
        # Detach before the session commits so callers get loaded, non-expiring objects
        # whatever the factory's expire_on_commit setting.
        with self.session_factory() as s:
            row = s.get(Investigation, inv_id)
            if row is not None:
                s.expunge(row)
            return row

    def list_for(self, principal: Principal, limit: int = 50) -> list[Investigation]:
        with self.session_factory() as s:
            stmt = select(Investigation).order_by(Investigation.created_at.desc()).limit(limit)
            if principal.role != "admin":
                stmt = stmt.where(Investigation.subject == principal.subject)
            rows = list(s.scalars(stmt))
            s.expunge_all()
            return rows
