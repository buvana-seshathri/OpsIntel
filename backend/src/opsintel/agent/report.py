"""The structured output of an investigation, and the grounding check applied to it."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

# IDs that tools return and reports may cite.
CITABLE_ID = re.compile(
    r"\b(?:evt_\d{6}|dep_[0-9a-f]{8}|cfg_[0-9a-f]{8}|act_[0-9a-f]{8}"
    r"|ord_[0-9a-f]{10}|pay_[0-9a-f]{10}|shp_[0-9a-f]{10}|cus_[0-9a-f]{8}"
    r"|chk_[a-z0-9-]+_\d{2}|doc_[a-z0-9-]+"
    r"|service:[a-z0-9-]+|host:[a-z0-9-]+|team:[a-z0-9-]+|error_code:[A-Z0-9_]+)"
)


class Evidence(BaseModel):
    id: str = Field(description="A citable ID returned by a tool: evt_, dep_, cfg_, chk_, ...")
    claim: str = Field(description="What this item shows, in one sentence")


class Hypothesis(BaseModel):
    statement: str
    status: Literal["supported", "rejected", "open"]
    evidence_ids: list[str] = []


class RootCause(BaseModel):
    description: str
    kind: Literal[
        "deploy",
        "config_change",
        "third_party",
        "certificate",
        "host",
        "capacity",
        "none",
        "unknown",
    ]
    entity_id: str | None = Field(
        default=None,
        description=(
            "The culprit, as an ID exactly as tools return it: a dep_/cfg_ ID for a change, "
            "service:<id> for a failing dependency or saturated service, host:<id> for a "
            "single bad host. null only when nothing is wrong."
        ),
    )
    confidence: float = Field(ge=0.0, le=1.0)


ActionType = Literal[
    "rollback_deploy",
    "revert_config_change",
    "failover_provider",
    "rotate_certificate",
    "drain_host",
    "scale_service",
    "page_team",
]


class RecommendedAction(BaseModel):
    type: ActionType
    target: str = Field(
        description="Entity ID as tools return it; for failover_provider, the provider to switch TO"
    )
    rationale: str
    evidence_ids: list[str] = []
    proposal_id: str | None = Field(
        default=None, description="act_ ID if this was queued with propose_action"
    )


class InvestigationReport(BaseModel):
    summary: str = Field(description="Two or three sentences for the on-call engineer")
    root_cause: RootCause
    hypotheses: list[Hypothesis] = Field(
        description="Including rejected red herrings and why they were rejected"
    )
    evidence: list[Evidence]
    recommended_actions: list[RecommendedAction] = []
    affected_services: list[str] = []
    open_questions: list[str] = []

    def cited_ids(self) -> set[str]:
        ids = {e.id for e in self.evidence}
        for h in self.hypotheses:
            ids |= set(h.evidence_ids)
        for a in self.recommended_actions:
            ids |= set(a.evidence_ids)
        if self.root_cause.entity_id:
            ids.add(self.root_cause.entity_id)
        return ids


class Grounding(BaseModel):
    cited: int
    seen_in_tool_results: int
    unseen_ids: list[str]  # cited but never returned by a tool: likely hallucinated

    @property
    def ratio(self) -> float:
        return self.seen_in_tool_results / self.cited if self.cited else 1.0


def ids_in(text: str) -> set[str]:
    return set(CITABLE_ID.findall(text))


def check_grounding(report: InvestigationReport, seen: set[str]) -> Grounding:
    cited = report.cited_ids()
    unseen = sorted(cited - seen)
    return Grounding(
        cited=len(cited), seen_in_tool_results=len(cited) - len(unseen), unseen_ids=unseen
    )
