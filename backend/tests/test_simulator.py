from collections import Counter

import pytest

from opsintel.simulator import SCENARIOS, Dataset, generate
from tests.conftest import ANCHOR

DATASETS = {key: generate(s, 42, ANCHOR) for key, s in SCENARIOS.items()}


def entity_exists(ds: Dataset, ref: str) -> bool:
    t = ds.tables
    if ref.startswith("dep_"):
        return any(r["id"] == ref for r in t["deploys"])
    if ref.startswith("cfg_"):
        return any(r["id"] == ref for r in t["config_changes"])
    kind, _, ident = ref.partition(":")
    table = {"service": "services", "host": "hosts"}[kind]
    return any(r["id"] == ident for r in t[table])


def checkout_error_ratio(ds: Dataset) -> float:
    series = [
        r["value"]
        for r in ds.tables["metric_points"]
        if r["service_id"] == "checkout-svc" and r["name"] == "error_rate"
    ]
    return (sum(series[-10:]) / 10) / (sum(series[:60]) / 60)


def firing_alert_services(ds: Dataset) -> set[str]:
    return {
        e["service_id"]
        for e in ds.tables["events"]
        if e["kind"] == "alert" and e["attributes"]["state"] == "firing"
    }


def test_generation_is_deterministic() -> None:
    again = generate(SCENARIOS["bad_deploy_payments"], 42, ANCHOR)
    assert again.tables == DATASETS["bad_deploy_payments"].tables
    other_seed = generate(SCENARIOS["bad_deploy_payments"], 7, ANCHOR)
    assert other_seed.tables["events"] != again.tables["events"]


@pytest.mark.parametrize("key", list(SCENARIOS))
def test_ground_truth_references_real_entities(key: str) -> None:
    ds = DATASETS[key]
    gt = ds.ground_truth
    refs = [r for r in [gt.root_cause_entity, *gt.red_herrings] if r]
    refs += [a.target for a in gt.expected_actions]
    for ref in refs:
        assert entity_exists(ds, ref), ref
    assert ds.tables["scenario_runs"][0]["ground_truth"]["root_cause_kind"] == gt.root_cause_kind


@pytest.mark.parametrize("key", list(SCENARIOS))
def test_ids_are_unique_and_events_ordered(key: str) -> None:
    t = DATASETS[key].tables
    for table in ("events", "orders", "payments", "shipments", "deploys", "customers"):
        ids = [r["id"] for r in t[table]]
        assert len(ids) == len(set(ids)), table
    ts = [e["ts"] for e in t["events"]]
    assert ts == sorted(ts)


@pytest.mark.parametrize("key", list(SCENARIOS))
def test_deploy_versions_form_a_chain(key: str) -> None:
    by_service: dict[str, list[dict]] = {}
    for d in sorted(DATASETS[key].tables["deploys"], key=lambda d: d["deployed_at"]):
        by_service.setdefault(d["service_id"], []).append(d)
    for deploys in by_service.values():
        for prev, cur in zip(deploys, deploys[1:], strict=False):
            assert cur["previous_version"] == prev["version"]


def test_bad_deploy_matches_the_story() -> None:
    ds = DATASETS["bad_deploy_payments"]
    assert 6 <= checkout_error_ratio(ds) <= 10  # "failures jumped about 8x"
    culprit = next(d for d in ds.tables["deploys"] if d["id"] == ds.ground_truth.root_cause_entity)
    minutes_ago = (ds.window_end - culprit["deployed_at"]).total_seconds() / 60
    assert 20 <= minutes_ago <= 25
    assert {"payments-svc", "checkout-svc"} <= firing_alert_services(ds)
    reasons = Counter(
        o["failure_reason"]
        for o in ds.tables["orders"]
        if o["failure_reason"] and o["created_at"] > culprit["deployed_at"]
    )
    assert reasons.most_common(1)[0][0] == "PAYFLOW_POOL_EXHAUSTED"


def test_no_errors_logged_before_fault_starts() -> None:
    for key, ds in DATASETS.items():
        start = ds.ground_truth.fault_start
        if start is None:
            continue
        codes = {
            "PAYFLOW_POOL_EXHAUSTED",
            "DB_LOCK_TIMEOUT",
            "PAYFLOW_503",
            "TLS_CERT_EXPIRED",
            "TAX_CALC_FAILED",
            "ENOSPC",
            "OOM_KILLED",
            "QUEUE_SATURATED",
        }
        early = [
            e
            for e in ds.tables["events"]
            if e["ts"] < start and e["attributes"].get("error_code") in codes
        ]
        assert not early, (key, early[:1])


def test_healthy_control_has_no_incident() -> None:
    ds = DATASETS["healthy"]
    assert firing_alert_services(ds) == set()
    assert 0.7 <= checkout_error_ratio(ds) <= 1.4


def test_cert_expiry_spares_checkout() -> None:
    ds = DATASETS["carrier_cert_expired"]
    assert checkout_error_ratio(ds) < 1.5
    assert firing_alert_services(ds) == {"shipping-svc"}
    late = [s for s in ds.tables["shipments"] if s["created_at"] > ds.ground_truth.fault_start]
    assert sum(s["status"] == "error" for s in late) / len(late) > 0.8


def test_tax_flag_only_breaks_eu_orders() -> None:
    ds = DATASETS["tax_engine_flag"]
    region = {c["id"]: c["region"] for c in ds.tables["customers"]}
    regions = Counter(
        region[o["customer_id"]]
        for o in ds.tables["orders"]
        if o["failure_reason"] == "TAX_CALC_FAILED"
    )
    assert set(regions) == {"eu-west"}


def test_disk_full_errors_come_from_one_host() -> None:
    ds = DATASETS["host_disk_full"]
    # Callers also log the code ("call to payments-svc failed: ENOSPC") from their own
    # hosts; only payments-svc's own logs should point at a single host.
    hosts = {
        e["host_id"]
        for e in ds.tables["events"]
        if e["service_id"] == "payments-svc" and e["attributes"].get("error_code") == "ENOSPC"
    }
    assert hosts == {"payments-svc-02"}


def test_memory_leak_culprit_is_not_the_latest_deploy() -> None:
    ds = DATASETS["checkout_memory_leak"]
    latest = max(ds.tables["deploys"], key=lambda d: d["deployed_at"])
    assert latest["id"] in ds.ground_truth.red_herrings
    assert latest["id"] != ds.ground_truth.root_cause_entity


def test_flash_sale_saturates_inventory_first() -> None:
    ds = DATASETS["flash_sale"]
    saturated = {
        e["service_id"]
        for e in ds.tables["events"]
        if e["attributes"].get("error_code") == "QUEUE_SATURATED"
    }
    assert "inventory-svc" in saturated
    assert "payments-svc" not in saturated
