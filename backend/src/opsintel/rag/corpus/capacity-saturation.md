---
title: Runbook - Capacity saturation and traffic surges
kind: runbook
services: [inventory-svc, checkout-svc, api-gateway, payments-svc]
acl: [viewer, responder, admin]
---
# Capacity saturation

## Symptoms
- request_rate well above normal across api-gateway, checkout-svc and downstream services.
- "request queue saturated ... shedding load", `QUEUE_SATURATED`, "worker pool utilization".
- Autoscaler messages like "cannot scale beyond max_replicas".
- Errors rise on the service with the least headroom first (usually inventory-svc, which
  receives two calls per checkout).

## Diagnosis
Compare request_rate now against the previous hour. A surge that lines up with a marketing
promotion (feature flags like `promo.*`) is expected traffic, not a fault. Do not roll back
the promotion unless the business agrees; scale instead.

## Fix
Propose `scale_service` for the saturated service (for example `service:inventory-svc`) and
raise its autoscaler `max_replicas`. Recheck after 5 minutes.
