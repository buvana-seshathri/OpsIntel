---
title: Postmortem - November 2025 Black Friday capacity incident
kind: postmortem
services: [inventory-svc, checkout-svc, api-gateway]
acl: [viewer, responder, admin]
---
# Postmortem: Black Friday capacity (November 2025)

Traffic reached 2.8x normal at the promotion launch. inventory-svc hit its autoscaler
ceiling of 3 replicas and shed load, failing about 18% of checkouts. An engineer rolled back
an unrelated api-gateway deploy first, which cost 12 minutes.

Lessons: a traffic surge with no matching code change is a capacity problem. Scale the
saturated service; do not roll back unrelated deploys.
