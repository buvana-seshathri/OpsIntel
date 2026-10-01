---
title: Postmortem - March 2025 checkout outage from payflow connection pool
kind: postmortem
services: [payments-svc, checkout-svc, payflow-api]
acl: [viewer, responder, admin]
---
# Postmortem: payflow connection pool exhaustion (March 2025)

## Summary
For 34 minutes about 9% of checkouts failed. A payments-svc deploy switched the payflow
client to a pooled HTTP client and read `PAYFLOW_POOL_MAX` from a config default of 4.
Requests queued for a connection and timed out (`PAYFLOW_POOL_EXHAUSTED`).

## What went well
Error logs named the pool explicitly.

## What went poorly
Responders first suspected payflow itself and spent 15 minutes on the processor status
page before checking our own deploys.

## Lessons
- Check our own recent deploys before blaming the processor.
- Rolling back payments-svc resolved it within 3 minutes.
