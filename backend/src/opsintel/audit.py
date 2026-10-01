"""Hash-chained, append-only audit log.

Each record stores `hash = sha256(prev_hash || canonical_json(record))`. Changing,
deleting or reordering any row breaks every hash after it, which `verify_chain` detects.
The database trigger from migration 0006 blocks UPDATE/DELETE/TRUNCATE in the first place;
the chain is what proves nobody bypassed it. Truncating the tail is the one edit a chain
cannot reveal on its own, so `head()` gives a value to anchor somewhere external
(CloudWatch, a ticket, a signed email) at regular intervals.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic_core import to_jsonable_python
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from opsintel.context import AuditContext, audit_context, current
from opsintel.db.models import AuditRecord
from opsintel.tools.registry import ToolCallRecord, digest

GENESIS = "0" * 64
_LOCK_KEY = 0x0A0D17  # pg_advisory_xact_lock key serialising appends to the chain


def _canonical(record: dict[str, Any]) -> bytes:
    return json.dumps(
        to_jsonable_python(record), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def compute_hash(prev_hash: str, record: dict[str, Any]) -> str:
    return hashlib.sha256(prev_hash.encode() + _canonical(record)).hexdigest()


def _content(r: AuditRecord) -> dict[str, Any]:
    return {
        "ts": r.ts.isoformat(),
        "kind": r.kind,
        "actor": r.actor,
        "role": r.role,
        "investigation_id": r.investigation_id,
        "payload": r.payload,
    }


def append(
    session: Session,
    kind: str,
    actor: str,
    role: str,
    payload: dict[str, Any],
    investigation_id: str | None = None,
    ts: datetime | None = None,
) -> AuditRecord:
    # Serialise writers so two appends can't both chain onto the same previous hash.
    session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOCK_KEY})
    prev = session.scalar(select(AuditRecord.hash).order_by(AuditRecord.seq.desc()).limit(1))
    record = AuditRecord(
        ts=ts or datetime.now(UTC),
        kind=kind,
        actor=actor,
        role=role,
        investigation_id=investigation_id,
        payload=to_jsonable_python(payload),
        prev_hash=prev or GENESIS,
        hash="",
    )
    record.hash = compute_hash(record.prev_hash, _content(record))
    session.add(record)
    session.flush()
    return record


@dataclass
class ChainReport:
    records: int
    ok: bool
    first_bad_seq: int | None = None
    reason: str | None = None
    head: str = GENESIS


def verify_chain(session: Session, batch: int = 2000) -> ChainReport:
    prev = GENESIS
    count = 0
    last_seq = 0
    while True:
        rows = session.scalars(
            select(AuditRecord)
            .where(AuditRecord.seq > last_seq)
            .order_by(AuditRecord.seq)
            .limit(batch)
        ).all()
        if not rows:
            return ChainReport(records=count, ok=True, head=prev)
        for r in rows:
            if r.prev_hash != prev:
                return ChainReport(
                    count,
                    False,
                    r.seq,
                    "prev_hash does not match the "
                    "previous record (a record was removed or reordered)",
                    prev,
                )
            if compute_hash(r.prev_hash, _content(r)) != r.hash:
                return ChainReport(
                    count,
                    False,
                    r.seq,
                    "content does not match its hash (the record was modified)",
                    prev,
                )
            prev = r.hash
            count += 1
            last_seq = r.seq


def head(session: Session) -> str:
    return (
        session.scalar(select(AuditRecord.hash).order_by(AuditRecord.seq.desc()).limit(1))
        or GENESIS
    )


class ToolCallAuditor:
    """ToolRunner observer: one audit record per tool call, allowed or denied. It writes in
    its own transaction so a failed or rolled-back tool call is still on record."""

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    def __call__(self, call: ToolCallRecord) -> None:
        ctx = current()
        payload = {
            "tool": call.tool,
            "arguments": call.arguments,
            "ok": call.ok,
            "error": call.error,
            "result_digest": call.result_digest,
            "latency_ms": call.latency_ms,
            "model": ctx.model,
            "prompt_version": ctx.prompt_version,
        }
        with self._session_factory() as s:
            append(
                s,
                "tool_call",
                call.subject,
                call.role,
                payload,
                investigation_id=ctx.investigation_id,
                ts=call.started_at,
            )


__all__ = [
    "AuditContext",
    "ToolCallAuditor",
    "append",
    "audit_context",
    "digest",
    "head",
    "verify_chain",
]
