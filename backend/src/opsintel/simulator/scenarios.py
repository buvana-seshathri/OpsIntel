"""Seeded incident scenarios. Each `build` function injects causes and returns the
ground truth the evals grade against. Most include a red herring: a plausible but
innocent change that a careless investigator would blame."""

from __future__ import annotations

from opsintel.simulator.engine import (
    ExpectedAction,
    Fault,
    GroundTruth,
    Scenario,
    ScenarioBuilder,
    Surge,
)

# Minute 120 is "now"; faults start around minute 98, i.e. roughly 20 minutes ago.


def _bad_deploy_payments(b: ScenarioBuilder) -> GroundTruth:
    herring = b.deploy("shipping-svc", 70, "Add carrier rate-quote caching")
    culprit = b.deploy(
        "payments-svc",
        98,
        "Migrate payflow client to pooled HTTP client; set PAYFLOW_POOL_MAX from config",
        author="raj.patel",
    )
    b.fault(
        Fault(
            "payments-svc",
            start=98,
            ramp=2,
            error_rate=0.09,
            latency_ms=900,
            error_code="PAYFLOW_POOL_EXHAUSTED",
            log_messages=(
                "payflow client: connection pool exhausted (max=4, waiting=57); request aborted",
                "timeout acquiring payflow connection after 5000ms",
            ),
        )
    )
    return GroundTruth(
        root_cause_kind="deploy",
        root_cause_entity=culprit,
        culprit_service="payments-svc",
        summary=f"Deploy {culprit} to payments-svc shrank the payflow HTTP connection pool "
        "to 4, so charges time out waiting for a connection; checkout fails on payment.",
        expected_actions=[ExpectedAction(type="rollback_deploy", target=culprit)],
        red_herrings=[herring],
        affected_services=["payments-svc", "checkout-svc", "api-gateway"],
        fault_start=b.ts(98),
        relevant_runbooks=["payments-error-rate", "deploy-rollback"],
    )


def _inventory_db_contention(b: ScenarioBuilder) -> GroundTruth:
    herring = b.deploy("inventory-svc", 40, "Add SKU search endpoint behind feature flag")
    culprit = b.config_change(
        "inventory-db",
        92,
        "infra",
        "cron.inventory_reconcile.schedule",
        "0 2 * * *",
        "*/15 * * * *",
        by="data-eng-bot",
    )
    b.log(
        "inventory-db",
        92.5,
        "info",
        "job inventory_reconcile started: full table scan on stock (14.2M rows)",
        host_id="inventory-db-01",
        job="inventory_reconcile",
    )
    b.fault(
        Fault(
            "inventory-db",
            start=93,
            ramp=4,
            error_rate=0.08,
            latency_ms=2200,
            error_code="DB_LOCK_TIMEOUT",
            host_ids=("inventory-db-01",),
            log_messages=(
                "canceling statement due to lock timeout: "
                "UPDATE stock SET reserved = reserved + $1",
                "process 48211 still waiting for RowExclusiveLock on relation stock after 2000 ms",
            ),
        )
    )
    return GroundTruth(
        root_cause_kind="config_change",
        root_cause_entity=culprit,
        culprit_service="inventory-db",
        summary=f"Config change {culprit} moved the inventory reconcile job from nightly to "
        "every 15 minutes; its full-table scan holds locks on stock, so reservations time "
        "out and checkout fails.",
        expected_actions=[ExpectedAction(type="revert_config_change", target=culprit)],
        red_herrings=[herring],
        affected_services=["inventory-db", "inventory-svc", "checkout-svc", "api-gateway"],
        fault_start=b.ts(93),
        relevant_runbooks=["database-lock-contention"],
    )


def _payflow_outage(b: ScenarioBuilder) -> GroundTruth:
    herring = b.deploy(
        "payments-svc", 88, "Change log format of payment audit records", author="mei.chen"
    )
    b.fault(
        Fault(
            "payflow-api",
            start=96,
            ramp=3,
            error_rate=0.12,
            latency_ms=2500,
            error_code="PAYFLOW_503",
            log_service_id="payments-svc",
            log_messages=(
                "payflow-api returned 503 Service Unavailable (x-payflow-request-id present)",
                "payflow-api request timed out after 3000ms",
            ),
        )
    )
    b.log(
        "payments-svc",
        101,
        "info",
        "paysecure-api health check OK (standby processor available, failover disabled)",
        host_id="payments-svc-01",
    )
    return GroundTruth(
        root_cause_kind="third_party",
        root_cause_entity="service:payflow-api",
        culprit_service="payflow-api",
        summary="The external processor payflow-api is degraded (503s and timeouts). "
        "Nothing changed on our side that explains it; the recent payments-svc deploy only "
        "touched log formatting. Fail over to paysecure; do not roll back.",
        expected_actions=[
            ExpectedAction(type="failover_provider", target="service:paysecure-api"),
        ],
        red_herrings=[herring],
        affected_services=["payflow-api", "payments-svc", "checkout-svc", "api-gateway"],
        fault_start=b.ts(96),
        relevant_runbooks=["payments-error-rate", "payment-provider-failover"],
    )


def _carrier_cert_expired(b: ScenarioBuilder) -> GroundTruth:
    herring = b.deploy("checkout-svc", 85, "Show estimated delivery date on order confirmation")
    not_after = b.ts(100).strftime("%Y-%m-%dT%H:%M:%SZ")
    for minute in (5, 35, 65, 95):
        b.log(
            "shipping-svc",
            minute,
            "warning",
            f"client certificate CN=shipping-svc.carrierx-mtls expires soon (notAfter={not_after})",
            host_id="shipping-svc-01",
            cert="carrierx-mtls",
            not_after=not_after,
        )
    b.fault(
        Fault(
            "shipping-svc",
            start=100,
            error_rate=0.92,
            error_code="TLS_CERT_EXPIRED",
            log_messages=(
                "TLS handshake with carrier-api failed: certificate has expired "
                "(CN=shipping-svc.carrierx-mtls)",
                "label creation failed: x509: certificate has expired or is not yet valid",
            ),
        )
    )
    return GroundTruth(
        root_cause_kind="certificate",
        root_cause_entity="service:shipping-svc",
        culprit_service="shipping-svc",
        summary="shipping-svc's mTLS client certificate for carrier-api expired, so every "
        "label request fails the TLS handshake. Checkout is unaffected because labels are "
        "created asynchronously.",
        expected_actions=[ExpectedAction(type="rotate_certificate", target="service:shipping-svc")],
        red_herrings=[herring],
        affected_services=["shipping-svc"],
        fault_start=b.ts(100),
        relevant_runbooks=["certificate-rotation"],
    )


def _tax_engine_flag(b: ScenarioBuilder) -> GroundTruth:
    herring = b.deploy("inventory-svc", 60, "Tune reservation expiry from 15m to 10m")
    culprit = b.config_change(
        "checkout-svc",
        97,
        "feature_flag",
        "checkout.tax_engine_v2",
        "false",
        "true",
        by="olivia.park",
    )
    b.fault(
        Fault(
            "checkout-svc",
            start=97,
            error_rate=0.075,
            error_code="TAX_CALC_FAILED",
            customer_region="eu-west",
            log_messages=(
                "TaxEngineV2: VAT rule lookup returned null for region=eu-west; aborting checkout",
                "unhandled NullPointerException in TaxEngineV2.computeVat (region=eu-west)",
            ),
        )
    )
    return GroundTruth(
        root_cause_kind="config_change",
        root_cause_entity=culprit,
        culprit_service="checkout-svc",
        summary=f"Feature flag change {culprit} enabled checkout.tax_engine_v2, which has no "
        "VAT rules for eu-west, so every EU checkout fails. Disable the flag.",
        expected_actions=[ExpectedAction(type="revert_config_change", target=culprit)],
        red_herrings=[herring],
        affected_services=["checkout-svc", "api-gateway"],
        fault_start=b.ts(97),
        relevant_runbooks=["feature-flag-rollback"],
    )


def _host_disk_full(b: ScenarioBuilder) -> GroundTruth:
    herring = b.deploy(
        "payments-svc", 30, "Enable verbose audit logging for chargebacks", author="dmitri.volkov"
    )
    b.log(
        "payments-svc",
        75,
        "warning",
        "disk usage on /var/log at 91%",
        host_id="payments-svc-02",
        mount="/var/log",
    )
    b.log(
        "payments-svc",
        88,
        "warning",
        "disk usage on /var/log at 98%",
        host_id="payments-svc-02",
        mount="/var/log",
    )
    b.fault(
        Fault(
            "payments-svc",
            start=94,
            error_rate=0.08,
            error_code="ENOSPC",
            host_ids=("payments-svc-02",),
            log_messages=(
                "ENOSPC: no space left on device, write '/var/log/payments/audit.log'",
                "failed to persist payment audit record: no space left on device",
            ),
        )
    )
    return GroundTruth(
        root_cause_kind="host",
        root_cause_entity="host:payments-svc-02",
        culprit_service="payments-svc",
        summary="Host payments-svc-02 ran out of disk on /var/log, so every payment routed to "
        "it fails writing its audit record. The other two hosts are healthy. Drain the host "
        "and clear logs; the verbose-logging deploy filled the disk only on this host's "
        "small volume, so a rollback is not the first move.",
        expected_actions=[ExpectedAction(type="drain_host", target="host:payments-svc-02")],
        red_herrings=[herring],
        affected_services=["payments-svc", "checkout-svc", "api-gateway"],
        fault_start=b.ts(94),
        relevant_runbooks=["host-disk-full"],
    )


def _flash_sale(b: ScenarioBuilder) -> GroundTruth:
    herring = b.deploy("api-gateway", 80, "Upgrade TLS library")
    b.config_change(
        "checkout-svc",
        95,
        "feature_flag",
        "promo.fall_flash_50",
        "false",
        "true",
        by="marketing-ops",
    )
    b.surge(Surge(start=95, multiplier=2.6, ramp=4))
    b.log(
        "inventory-svc",
        97,
        "warning",
        "autoscaler: cannot scale inventory-svc beyond max_replicas=3",
        host_id=None,
        autoscaler="hpa",
    )
    return GroundTruth(
        root_cause_kind="capacity",
        root_cause_entity="service:inventory-svc",
        culprit_service="inventory-svc",
        summary="A flash-sale promotion roughly tripled traffic. inventory-svc has the least "
        "headroom, hit its autoscaling ceiling and started shedding load, which fails checkout. "
        "Raise inventory-svc capacity; nothing needs rolling back.",
        expected_actions=[ExpectedAction(type="scale_service", target="service:inventory-svc")],
        red_herrings=[herring],
        affected_services=["inventory-svc", "checkout-svc", "api-gateway"],
        fault_start=b.ts(95),
        relevant_runbooks=["capacity-saturation"],
    )


def _checkout_memory_leak(b: ScenarioBuilder) -> GroundTruth:
    culprit = b.deploy(
        "checkout-svc", 30, "Cache computed cart totals in-process", author="tom.okafor"
    )
    herring = b.deploy("shipping-svc", 104, "Retry label creation with jitter")
    b.fault(
        Fault(
            "checkout-svc",
            start=40,
            ramp=60,
            latency_ms=500,
            error_code="GC_PRESSURE",
            log_messages=("GC pause 1.4s (heap 1.8Gi / 2.0Gi)", "old-gen heap usage above 85%"),
        )
    )
    b.fault(
        Fault(
            "checkout-svc",
            start=96,
            ramp=3,
            error_rate=0.06,
            error_code="OOM_KILLED",
            log_messages=("upstream connection reset: checkout-svc pod restarting",),
        )
    )
    for minute, host in (
        (96, "checkout-svc-02"),
        (101, "checkout-svc-01"),
        (107, "checkout-svc-03"),
        (112, "checkout-svc-02"),
    ):
        b.log(
            "checkout-svc",
            minute,
            "critical",
            f"container checkout-svc OOMKilled on {host} (limit 2Gi); restarting",
            host_id=host,
            reason="OOMKilled",
        )
    return GroundTruth(
        root_cause_kind="deploy",
        root_cause_entity=culprit,
        culprit_service="checkout-svc",
        summary=f"Deploy {culprit} (90 minutes ago) added an unbounded in-process cache. Heap "
        "grew steadily until pods began OOM-killing about 20 minutes ago. The most recent "
        "deploy (shipping-svc) is unrelated.",
        expected_actions=[ExpectedAction(type="rollback_deploy", target=culprit)],
        red_herrings=[herring],
        affected_services=["checkout-svc", "api-gateway"],
        fault_start=b.ts(40),
        relevant_runbooks=["memory-leak", "deploy-rollback"],
    )


def _healthy(b: ScenarioBuilder) -> GroundTruth:
    b.deploy("inventory-svc", 90, "Add SKU search endpoint behind feature flag")
    return GroundTruth(
        root_cause_kind="none",
        root_cause_entity=None,
        culprit_service=None,
        summary="No incident. All services are within normal error and latency ranges.",
    )


SCENARIOS: dict[str, Scenario] = {
    s.key: s
    for s in [
        Scenario(
            "bad_deploy_payments",
            "Bad payments deploy",
            "Checkout failures jumped about 8x in the last 20 minutes. Why, and what should we do?",
            _bad_deploy_payments,
        ),
        Scenario(
            "inventory_db_contention",
            "Inventory DB lock contention",
            "Checkout is failing intermittently and inventory looks slow. What's going on?",
            _inventory_db_contention,
        ),
        Scenario(
            "payflow_outage",
            "Payment processor outage",
            "Payment errors spiked about 25 minutes ago. Should we roll back payments?",
            _payflow_outage,
        ),
        Scenario(
            "carrier_cert_expired",
            "Carrier certificate expired",
            "Shipping labels stopped being created about 20 minutes ago. Why?",
            _carrier_cert_expired,
        ),
        Scenario(
            "tax_engine_flag",
            "Tax engine feature flag",
            "Some customers report checkout errors since about 20 minutes ago. What's the cause?",
            _tax_engine_flag,
        ),
        Scenario(
            "host_disk_full",
            "Payments host disk full",
            "Payment errors have been elevated for about 25 minutes. Investigate.",
            _host_disk_full,
        ),
        Scenario(
            "flash_sale",
            "Flash-sale traffic surge",
            "Checkout latency and errors are up sharply in the last 25 minutes. What happened?",
            _flash_sale,
        ),
        Scenario(
            "checkout_memory_leak",
            "Checkout memory leak",
            "Checkout errors started about 20 minutes ago. Was it the deploy that just went out?",
            _checkout_memory_leak,
        ),
        Scenario(
            "healthy",
            "No incident (control)",
            "Is anything wrong with checkout right now?",
            _healthy,
        ),
    ]
}
