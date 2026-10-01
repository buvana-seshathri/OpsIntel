---
title: Runbook - Elevated payments-svc error rate
kind: runbook
services: [payments-svc, payflow-api, paysecure-api, checkout-svc]
acl: [viewer, responder, admin]
---
# Elevated payments-svc error rate

Alert: `payments-svc error_rate` FIRING. Checkout fails whenever a charge fails, so
payments errors show up as checkout failures and api-gateway 5xx.

## Triage
1. Check whether the errors come from our code or from the processor. Group payments-svc
   error logs by `error_code`.
   - `PAYFLOW_503`, `PAYFLOW_5XX` or "payflow-api request timed out": the processor is
     failing. Go to the payment provider failover runbook. Do not roll back our services
     unless a deploy lines up with the first error.
   - `PAYFLOW_POOL_EXHAUSTED`, "connection pool exhausted", "timeout acquiring payflow
     connection": our client is starving itself. This is almost always a recent payments-svc
     deploy or config change touching the HTTP client or `PAYFLOW_POOL_MAX`.
   - `ENOSPC` or other host-level errors: check whether errors come from a single host.
2. List deploys and config changes to payments-svc in the 60 minutes before the first error.
3. Compare the error onset with the deploy time. A deploy within a few minutes of onset is
   the prime suspect.

## Mitigation
- Recent payments-svc deploy suspected: roll back to the previous version (see the deploy
  rollback runbook). Rollback is safe; payments-svc is stateless.
- Processor outage: fail over to paysecure.
- Single bad host: drain the host.

## Escalation
Page payments on-call (#team-payments) if error rate stays above 5% for 10 minutes.
