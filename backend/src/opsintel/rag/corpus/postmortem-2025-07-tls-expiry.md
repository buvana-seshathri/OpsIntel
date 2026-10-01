---
title: Postmortem - July 2025 shipping labels failed after certificate expiry
kind: postmortem
services: [shipping-svc, carrier-api]
acl: [viewer, responder, admin]
---
# Postmortem: carrier mTLS certificate expiry (July 2025)

shipping-svc's client certificate for carrier-api expired at 03:00. No labels were created
for 5 hours. Orders kept completing, so nobody noticed until warehouse staff reported empty
pick lists. Expiry warnings had been logged for a week at `warning` severity.

Action items: page on certificate expiry under 7 days; add a shipments error-rate alert.
