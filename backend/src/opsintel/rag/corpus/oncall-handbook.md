---
title: On-call handbook
kind: policy
services: []
acl: [viewer, responder, admin]
---
# On-call handbook

## Severity
- SEV1: checkout success rate below 90% or payments unavailable. Page immediately.
- SEV2: a tier-1 service degraded, customers partially affected.
- SEV3: a tier-2 service degraded (for example shipping labels), no checkout impact.

## Principles
- Mitigate first, then find the root cause.
- Every state-changing action (rollback, failover, drain, scale, flag change) is proposed and
  approved by a responder before it runs.
- Correlation is not causation: confirm a suspect change touches the failing path.
