---
title: Runbook - Payment provider failover (payflow to paysecure)
kind: runbook
services: [payments-svc, payflow-api, paysecure-api]
acl: [viewer, responder, admin]
---
# Payment provider failover

payments-svc charges cards through payflow (primary). paysecure is a warm standby that
receives health checks only. Failover is a config switch, not a deploy.

## When to fail over
- payflow-api returns `PAYFLOW_503` / 503 Service Unavailable or times out for more than 5
  minutes, and
- no payments-svc change explains it (check the payments error-rate runbook first), and
- paysecure-api health checks are passing.

## Procedure
1. Propose `failover_provider` targeting `service:paysecure-api`. Requires responder approval.
2. payments-svc routes new charges to paysecure within 30 seconds.
3. Watch payments-svc error_rate. Fees are higher on paysecure; fail back once payflow's
   status page shows recovery.

## Do not
Roll back payments-svc during a processor outage. It costs time and changes nothing.
