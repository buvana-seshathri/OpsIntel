---
title: Runbook - Host out of disk space
kind: runbook
services: [payments-svc, checkout-svc, inventory-svc, shipping-svc, api-gateway]
acl: [viewer, responder, admin]
---
# Host out of disk

## Symptoms
- `ENOSPC`, "no space left on device", writes to `/var/log` failing.
- Errors concentrated on one host while other hosts of the same service are healthy. With
  three hosts behind the load balancer, one bad host fails roughly a third of requests it
  receives.
- Earlier warnings such as "disk usage on /var/log at 91%".

## Diagnosis
Group the service's error logs by `host_id`. If one host accounts for all of them, treat it
as a host problem, not a code problem, even if a recent deploy increased log volume.

## Fix
1. Propose `drain_host` for the host (for example `host:payments-svc-02`). The load balancer
   stops routing to it.
2. Clear rotated logs and fix log rotation, then return the host to service.
3. If a deploy increased log volume, open a ticket; a rollback is not the first move.
