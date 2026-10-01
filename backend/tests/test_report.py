from typing import get_args

from opsintel.agent.report import ActionType, InvestigationReport, check_grounding, ids_in
from opsintel.tools.catalog import ACTION_TYPES


def test_report_action_types_match_the_tool() -> None:
    assert set(get_args(ActionType)) == set(ACTION_TYPES)


def test_ids_in_finds_every_citable_kind() -> None:
    text = (
        "evt_000123 dep_69c2f6ba cfg_0a1b2c3d chk_host-disk-full_02 doc_memory-leak "
        "service:payments-svc host:payments-svc-02 error_code:ENOSPC ord_0123456789"
    )
    assert len(ids_in(text)) == 9


def test_grounding_counts_unseen_citations() -> None:
    report = InvestigationReport.model_validate(
        {
            "summary": "s",
            "root_cause": {
                "description": "d",
                "kind": "deploy",
                "entity_id": "dep_69c2f6ba",
                "confidence": 0.8,
            },
            "hypotheses": [
                {"statement": "h", "status": "rejected", "evidence_ids": ["evt_000002"]}
            ],
            "evidence": [{"id": "evt_000001", "claim": "c"}],
        }
    )
    g = check_grounding(report, seen={"dep_69c2f6ba", "evt_000001"})
    assert (g.cited, g.seen_in_tool_results, g.unseen_ids) == (3, 2, ["evt_000002"])
