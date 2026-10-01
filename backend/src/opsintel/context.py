"""Per-investigation context that follows a run into the MCP server task and the worker
threads executing tools (context variables are copied into both)."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class AuditContext:
    investigation_id: str | None = None
    model: str | None = None
    prompt_version: str | None = None


audit_context: ContextVar[AuditContext | None] = ContextVar("audit_context", default=None)


def current() -> AuditContext:
    return audit_context.get() or AuditContext()
