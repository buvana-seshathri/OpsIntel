---
title: Postmortem - September 2024 card data logging incident (restricted)
kind: postmortem
services: [payments-svc]
acl: [admin]
---
# Postmortem: card data in logs (September 2024, restricted)

A debug flag caused payments-svc to log truncated card numbers and customer emails to
`/var/log/payments/audit.log` for 6 hours. Logs were purged and the flag removed. Details of
affected customers are held by the security team.
