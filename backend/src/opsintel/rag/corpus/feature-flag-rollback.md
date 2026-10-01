---
title: Runbook - Disabling a feature flag
kind: runbook
services: [checkout-svc, inventory-svc, payments-svc]
acl: [viewer, responder, admin]
---
# Disabling a feature flag

Feature flags change behaviour without a deploy, so they are easy to miss. Always list
`config_changes` (kind `feature_flag`) next to deploys when investigating.

## Signs a flag is the cause
- Errors start within a minute or two of a flag change and no deploy lines up.
- Failures are limited to a segment the flag targets: one region, customer tier or route.
  Group failed orders by customer region; a single region is a strong signal.
- Stack traces mention the new code path, for example `TaxEngineV2`.

## Fix
Propose `revert_config_change` with the flag change ID (`cfg_...`). Flags revert in seconds
and need only responder approval. Notify the flag owner.
