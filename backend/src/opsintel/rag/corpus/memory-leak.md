---
title: Runbook - Memory leaks and OOMKilled containers
kind: runbook
services: [checkout-svc, payments-svc, inventory-svc]
acl: [viewer, responder, admin]
---
# Memory leaks

## Symptoms
- p99 latency creeping up over an hour or more, "GC pause", "old-gen heap usage above 85%".
- Then containers restart: "OOMKilled", "pod restarting", `OOM_KILLED` errors.
- Restarts rotate across hosts as each one fills up.

## Diagnosis
The leak was introduced when latency started to climb, not when the restarts started. Find
the deploy to the affected service just before the latency trend began. That is often not the
most recent deploy. Look for changes that add in-process caches or retain objects.

## Fix
Propose `rollback_deploy` for the deploy that introduced the leak. Restarting pods only buys
time.
