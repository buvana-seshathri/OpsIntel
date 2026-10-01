---
title: Runbook - Rolling back a deploy
kind: runbook
services: [api-gateway, checkout-svc, payments-svc, inventory-svc, shipping-svc]
acl: [viewer, responder, admin]
---
# Rolling back a deploy

Use when a deploy is the most likely cause of an incident. Rolling back is the default
mitigation for a bad deploy: it is fast and reversible. Fix forward later.

## Confirm the suspect
- The deploy finished shortly before symptoms started on the deployed service or a caller.
- The change summary plausibly touches the failing path (client config, caching, memory).
- Errors are on the deployed version. A deploy that only changed log formatting or a feature
  hidden behind a disabled flag is rarely the cause.
- Gradual failures (memory growth, GC pauses, OOMKilled) can start long after the deploy that
  caused them. Look further back than the latest deploy.

## Roll back
1. Propose `rollback_deploy` with the deploy ID (for example `dep_...`). Approval by a
   responder is required.
2. The service returns to `previous_version`. Watch error_rate and p99 for 10 minutes.
3. Mark the deploy `rolled_back` and open a follow-up ticket for the owning team.

## When not to roll back
- The failing dependency is a third party (payflow-api, carrier-api).
- The issue is capacity (traffic surge) or a single unhealthy host.
