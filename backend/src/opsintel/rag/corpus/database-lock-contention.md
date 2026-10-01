---
title: Runbook - Database lock contention and slow queries
kind: runbook
services: [inventory-db, orders-db, inventory-svc, checkout-svc]
acl: [viewer, responder, admin]
---
# Database lock contention

Symptoms: `DB_LOCK_TIMEOUT`, "canceling statement due to lock timeout", "still waiting for
RowExclusiveLock", rising p99 on inventory-db or orders-db, inventory-svc timeouts, and
checkout failures with reason `DB_LOCK_TIMEOUT` or `INVENTORY_UNAVAILABLE`.

## Diagnosis
1. Look for long-running jobs or full table scans that started just before the first lock
   timeout (job start logs on the database host).
2. Check config changes to the database and batch jobs, especially cron schedules.
   Jobs like `inventory_reconcile` are designed to run nightly; running them during peak
   traffic holds locks on `stock`.
3. Rule out application deploys: a deploy that adds a feature behind a disabled flag does
   not change query patterns.

## Mitigation
- Revert the offending config change (`revert_config_change` with the `cfg_...` ID).
- If the job is still running, cancel it with `pg_cancel_backend` (DBA approval).
- Do not fail over the database for lock contention; the replica inherits the same workload.
