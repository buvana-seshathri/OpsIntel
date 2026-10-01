"""Human approval of agent-proposed actions.

Rules:
- deciding needs `decide:action` (responders and admins);
- nobody approves their own proposal (two-person rule: the person whose investigation
  proposed a rollback cannot also be the one who lets it run);
- only pending proposals can be decided, and each decision is audited.

Approved actions run through an `Executor`. `SimulatedExecutor` applies the change to the
simulated company where that is meaningful (a rolled-back deploy is marked as such) and
otherwise describes what a real integration would do.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from opsintel import audit
from opsintel.auth import Permission, Principal
from opsintel.db.models import ActionProposal, ConfigChange, Deploy


class ActionError(Exception):
    pass


class Forbidden(ActionError):
    pass


class NotFound(ActionError):
    pass


class InvalidTransition(ActionError):
    pass


class Executor(Protocol):
    def execute(self, session: Session, proposal: ActionProposal) -> str: ...


class SimulatedExecutor:
    def execute(self, session: Session, proposal: ActionProposal) -> str:
        target = proposal.target
        if proposal.type == "rollback_deploy":
            deploy = session.get(Deploy, target)
            if deploy is None:
                raise ActionError(f"deploy {target} no longer exists")
            deploy.status = "rolled_back"
            return (
                f"Rolled back {deploy.service_id} from {deploy.version} to "
                f"{deploy.previous_version}"
            )
        if proposal.type == "revert_config_change":
            change = session.get(ConfigChange, target)
            if change is None:
                raise ActionError(f"config change {target} no longer exists")
            return (
                f"Reverted {change.key} on {change.service_id}: "
                f"{change.new_value!r} -> {change.old_value!r}"
            )
        name = target.split(":", 1)[-1]
        return {
            "failover_provider": f"Switched payments-svc's primary processor to {name}",
            "rotate_certificate": f"Issued and loaded a new client certificate for {name}",
            "drain_host": f"Removed {name} from the load balancer pool",
            "scale_service": f"Raised {name} replicas and its autoscaling ceiling",
            "page_team": f"Paged the on-call engineer of {name}",
        }.get(proposal.type, f"Executed {proposal.type} on {target}")


def list_proposals(
    session: Session, principal: Principal, status: str | None = None
) -> list[ActionProposal]:
    stmt = select(ActionProposal).order_by(ActionProposal.created_at.desc())
    if status:
        stmt = stmt.where(ActionProposal.status == status)
    if not principal.can(Permission.DECIDE_ACTION):
        stmt = stmt.where(ActionProposal.proposed_by == principal.subject)
    return list(session.scalars(stmt))


def decide(
    session: Session,
    principal: Principal,
    proposal_id: str,
    approve: bool,
    comment: str | None = None,
    executor: Executor | None = None,
) -> ActionProposal:
    if not principal.can(Permission.DECIDE_ACTION):
        raise Forbidden(f"role {principal.role!r} cannot approve or reject actions")
    # Row lock: two approvers clicking at once must not both execute the action.
    proposal = session.scalars(
        select(ActionProposal).where(ActionProposal.id == proposal_id).with_for_update()
    ).one_or_none()
    if proposal is None:
        raise NotFound(proposal_id)
    if proposal.status != "pending":
        raise InvalidTransition(f"{proposal_id} is already {proposal.status}")
    if approve and proposal.proposed_by == principal.subject:
        raise Forbidden("you cannot approve an action from your own investigation")

    now = datetime.now(UTC)
    proposal.status = "approved" if approve else "rejected"
    proposal.decided_by = principal.subject
    proposal.decided_at = now
    proposal.decision_comment = comment
    audit.append(
        session,
        "action_decision",
        principal.subject,
        principal.role,
        {
            "proposal_id": proposal.id,
            "type": proposal.type,
            "target": proposal.target,
            "decision": proposal.status,
            "comment": comment,
        },
        investigation_id=proposal.investigation_id,
    )
    if approve:
        try:
            result = (executor or SimulatedExecutor()).execute(session, proposal)
            proposal.status = "executed"
        except ActionError as e:
            result = f"failed: {e}"
            proposal.status = "failed"
        proposal.executed_at = datetime.now(UTC)
        proposal.execution_result = result
        audit.append(
            session,
            "action_executed",
            principal.subject,
            principal.role,
            {"proposal_id": proposal.id, "status": proposal.status, "result": result},
            investigation_id=proposal.investigation_id,
        )
    session.flush()
    return proposal


def as_dict(p: ActionProposal) -> dict[str, object]:
    return {
        "id": p.id,
        "type": p.type,
        "target": p.target,
        "rationale": p.rationale,
        "evidence_ids": p.evidence_ids,
        "status": p.status,
        "proposed_by": p.proposed_by,
        "investigation_id": p.investigation_id,
        "created_at": p.created_at,
        "decided_by": p.decided_by,
        "decided_at": p.decided_at,
        "decision_comment": p.decision_comment,
        "executed_at": p.executed_at,
        "execution_result": p.execution_result,
    }
