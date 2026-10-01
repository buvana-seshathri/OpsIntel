"""Replay an investigation's tool calls from the audit log and check that each one
returns exactly what the agent saw (same result digest). Calls run as the original user.
Because scenarios are seeded and tools use the scenario clock, reloading the same scenario
reproduces every read; a mismatch means the underlying data changed."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from opsintel.auth import Principal
from opsintel.context import AuditContext, audit_context
from opsintel.db.models import AuditRecord
from opsintel.tools import ToolError, ToolRunner
from opsintel.tools.registry import digest

WRITE_TOOLS = {"propose_action"}  # never re-executed


def replay_investigation(
    session_factory: Callable[[], AbstractContextManager[Session]],
    runner: ToolRunner,
    investigation_id: str,
) -> dict[str, Any]:
    with session_factory() as s:
        calls = [
            (r.seq, r.actor, r.role, dict(r.payload))
            for r in s.scalars(
                select(AuditRecord)
                .where(
                    AuditRecord.investigation_id == investigation_id,
                    AuditRecord.kind == "tool_call",
                )
                .order_by(AuditRecord.seq)
            )
        ]
    token = audit_context.set(AuditContext(model="replay"))
    details = []
    try:
        for seq, actor, role, p in calls:
            entry: dict[str, Any] = {"seq": seq, "tool": p["tool"], "original_ok": p["ok"]}
            if p["tool"] in WRITE_TOOLS:
                entry["outcome"] = "skipped (write)"
            else:
                try:
                    result = runner.call(p["tool"], p["arguments"], Principal(actor, role))
                    replayed = digest(result)
                    entry["outcome"] = (
                        "reproduced" if p["ok"] and replayed == p["result_digest"] else "differs"
                    )
                except ToolError as e:
                    # A call that was denied or invalid then should be refused again now.
                    entry["outcome"] = "reproduced" if not p["ok"] else "differs"
                    entry["replay_error"] = str(e)
            details.append(entry)
    finally:
        audit_context.reset(token)
    counts = {
        o: sum(d["outcome"] == o for d in details)
        for o in ("reproduced", "differs", "skipped (write)")
    }
    return {
        "investigation_id": investigation_id,
        "calls": len(details),
        **counts,
        "details": details,
    }
