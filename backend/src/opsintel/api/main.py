from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import select, text

from opsintel import __version__, actions
from opsintel.audit import ToolCallAuditor, verify_chain
from opsintel.auth import AuthError, Principal, verify_token
from opsintel.db.models import AuditRecord
from opsintel.db.session import get_engine, session_scope
from opsintel.graph.build import rebuild_graph
from opsintel.investigations import InvestigationService, can_view, summarize
from opsintel.llm import make_llm
from opsintel.rag.embeddings import make_embedder
from opsintel.replay import replay_investigation
from opsintel.simulator import SCENARIOS, generate
from opsintel.simulator.loader import load
from opsintel.tools import ToolRunner


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    if not hasattr(app.state, "investigations"):
        runner = ToolRunner(
            session_scope, make_embedder, observers=[ToolCallAuditor(session_scope)]
        )
        app.state.investigations = InvestigationService(runner, make_llm)
    yield
    await app.state.investigations.wait_all()


app = FastAPI(title="OpsIntel", version=__version__, lifespan=lifespan)
bearer = HTTPBearer(auto_error=False)


def principal(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Principal:
    if creds is None:
        raise HTTPException(401, "missing bearer token", {"WWW-Authenticate": "Bearer"})
    try:
        return verify_token(creds.credentials)
    except AuthError as e:
        raise HTTPException(401, str(e), {"WWW-Authenticate": "Bearer"}) from e


Caller = Annotated[Principal, Depends(principal)]


def service(request: Request) -> InvestigationService:
    svc: InvestigationService = request.app.state.investigations
    return svc


Service = Annotated[InvestigationService, Depends(service)]


@app.get("/healthz")
def healthz() -> dict[str, str]:
    with get_engine().connect() as conn:
        conn.execute(text("SELECT 1"))
    return {"status": "ok", "version": __version__}


@app.get("/scenarios")
def list_scenarios() -> list[dict[str, str]]:
    return [{"key": s.key, "title": s.title, "question": s.question} for s in SCENARIOS.values()]


@app.post("/scenarios/{key}/load")
def load_scenario(key: str, caller: Caller, svc: Service, seed: int = 42) -> dict[str, Any]:
    """Admin only: replace the data with a freshly simulated scenario (demo control)."""
    if caller.role != "admin":
        raise HTTPException(403, "only admins can load scenarios")
    if key not in SCENARIOS:
        raise HTTPException(404, f"unknown scenario {key!r}")
    from datetime import UTC, datetime

    ds = generate(SCENARIOS[key], seed, datetime.now(UTC))
    with svc.session_factory() as s:
        counts = load(s, ds)
        rebuild_graph(s)
    return {"scenario": key, "question": ds.question, "rows": counts}


class InvestigationRequest(BaseModel):
    question: str = Field(min_length=5, max_length=1000)


@app.post("/investigations", status_code=202)
async def start_investigation(
    body: InvestigationRequest, caller: Caller, svc: Service
) -> dict[str, str]:
    inv_id = svc.start(body.question, caller)
    return {"id": inv_id, "status": "running", "events": f"/investigations/{inv_id}/events"}


@app.get("/investigations")
def list_investigations(caller: Caller, svc: Service) -> list[dict[str, Any]]:
    return [summarize(r) for r in svc.list_for(caller)]


def _visible(svc: InvestigationService, inv_id: str, caller: Principal) -> Any:
    row = svc.get(inv_id)
    # 404 rather than 403 so IDs of other people's investigations are not confirmed.
    if row is None or not can_view(row, caller):
        raise HTTPException(404, "investigation not found")
    return row


@app.get("/investigations/{inv_id}")
def get_investigation(inv_id: str, caller: Caller, svc: Service) -> dict[str, Any]:
    return summarize(_visible(svc, inv_id, caller), include_trace=True)


@app.get("/investigations/{inv_id}/events")
async def investigation_events(inv_id: str, caller: Caller, svc: Service) -> StreamingResponse:
    """Server-Sent Events: the agent's trace live, or replayed if it already finished."""
    row = _visible(svc, inv_id, caller)

    async def stream() -> AsyncIterator[str]:
        if svc.bus.is_known(inv_id):
            events: AsyncIterator[dict[str, Any]] = svc.bus.subscribe(inv_id)
        else:  # e.g. after an API restart: replay what was persisted

            async def replay() -> AsyncIterator[dict[str, Any]]:
                for e in row.trace:
                    yield e

            events = replay()
        async for event in events:
            yield f"id: {event['seq']}\nevent: {event['type']}\ndata: {json.dumps(event)}\n\n"
        yield "event: end\ndata: {}\n\n"

    return StreamingResponse(
        stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
    )


# --- Approval queue -----------------------------------------------------------------------


class Decision(BaseModel):
    comment: str | None = Field(default=None, max_length=2000)


@app.get("/actions")
def list_actions(
    caller: Caller, svc: Service, status: str | None = None
) -> list[dict[str, object]]:
    """Responders and admins see the whole queue; others see their own proposals."""
    with svc.session_factory() as s:
        return [actions.as_dict(p) for p in actions.list_proposals(s, caller, status)]


def _decide(
    svc: InvestigationService, proposal_id: str, caller: Principal, approve: bool, body: Decision
) -> dict[str, object]:
    try:
        with svc.session_factory() as s:
            return actions.as_dict(actions.decide(s, caller, proposal_id, approve, body.comment))
    except actions.Forbidden as e:
        raise HTTPException(403, str(e)) from e
    except actions.NotFound as e:
        raise HTTPException(404, f"no proposal {e}") from e
    except actions.InvalidTransition as e:
        raise HTTPException(409, str(e)) from e


@app.post("/actions/{proposal_id}/approve")
def approve_action(
    proposal_id: str, caller: Caller, svc: Service, body: Decision | None = None
) -> dict[str, object]:
    return _decide(svc, proposal_id, caller, True, body or Decision())


@app.post("/actions/{proposal_id}/reject")
def reject_action(
    proposal_id: str, caller: Caller, svc: Service, body: Decision | None = None
) -> dict[str, object]:
    return _decide(svc, proposal_id, caller, False, body or Decision())


# --- Audit and replay ------------------------------------------------------------------------


@app.post("/investigations/{inv_id}/replay")
def replay(inv_id: str, caller: Caller, svc: Service) -> dict[str, Any]:
    _visible(svc, inv_id, caller)
    return replay_investigation(svc.session_factory, svc.runner, inv_id)


def _admin(caller: Principal) -> None:
    if caller.role != "admin":
        raise HTTPException(403, "the audit log is admin-only")


@app.get("/audit")
def audit_log(
    caller: Caller, svc: Service, investigation_id: str | None = None, limit: int = 200
) -> list[dict[str, Any]]:
    _admin(caller)
    stmt = select(AuditRecord).order_by(AuditRecord.seq.desc()).limit(min(limit, 1000))
    if investigation_id:
        stmt = stmt.where(AuditRecord.investigation_id == investigation_id)
    with svc.session_factory() as s:
        return [
            {
                "seq": r.seq,
                "ts": r.ts,
                "kind": r.kind,
                "actor": r.actor,
                "role": r.role,
                "investigation_id": r.investigation_id,
                "payload": r.payload,
                "prev_hash": r.prev_hash,
                "hash": r.hash,
            }
            for r in s.scalars(stmt)
        ]


@app.get("/audit/verify")
def audit_verify(caller: Caller, svc: Service) -> dict[str, Any]:
    _admin(caller)
    with svc.session_factory() as s:
        return vars(verify_chain(s))
