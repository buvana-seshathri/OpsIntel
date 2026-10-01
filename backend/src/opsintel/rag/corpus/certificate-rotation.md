---
title: Runbook - Expired or expiring TLS certificates
kind: runbook
services: [shipping-svc, carrier-api, api-gateway]
acl: [viewer, responder, admin]
---
# Certificate rotation

shipping-svc authenticates to carrier-api with an mTLS client certificate
(`CN=shipping-svc.carrierx-mtls`). When it expires every label request fails.

## Symptoms
- `TLS_CERT_EXPIRED`, "x509: certificate has expired or is not yet valid", "TLS handshake
  with carrier-api failed".
- shipping-svc error_rate near 100% starting at an exact minute (no ramp), and shipments in
  status `error`.
- Earlier warnings: "client certificate ... expires soon (notAfter=...)".
- Checkout is unaffected; labels are created asynchronously after the order is written.

## Fix
1. Propose `rotate_certificate` targeting `service:shipping-svc`. The new certificate is
   issued from the internal CA and loaded without a restart.
2. Replay failed label requests from the shipping retry queue.
3. Follow up: the expiry warning fired for hours. Route it to paging.
