# OpsIntel design

## Scenario

A microservice e-commerce platform: api-gateway, checkout, payments, inventory and shipping
services, two Postgres clusters, and three third-party APIs (two card processors and a
shipping carrier), plus the deploys, config changes, orders and customers around them.

A typical investigation: *"Checkout failures jumped 8× in the last 20 minutes. Why, and what
should we do?"* The agent checks recent events, finds a deploy to payments-svc 22 minutes ago,
follows the dependency graph, pulls the matching runbook and a past postmortem, and returns a
root cause with evidence and a recommended rollback. The rollback waits for human approval.

## Architecture

```
            React + TS dashboard (live feed, investigation trace, entity graph, audit, approvals)
                                  │  REST + SSE (JWT)
                         ┌────────▼─────────┐
                         │   FastAPI API    │── auth/RBAC, investigations, approvals
                         └────────┬─────────┘
                                  │
                         ┌────────▼──────────┐
                         │ Investigation     │  triage → gather → correlate
                         │ Agent (MCP client)│  → hypothesize → recommend (structured output)
                         └────────┬──────────┘
                                  │ MCP (tools carry the caller's identity)
                         ┌────────▼─────────┐
                         │  OpsIntel MCP    │  query_events, get_entity, trace_dependencies,
                         │  Server          │  search_docs, get_metrics, propose_action ...
                         └────────┬─────────┘  ← RBAC enforced + every call audited here
                                  │
      ┌───────────────────────────┼──────────────────────────────┐
      ▼                           ▼                              ▼
 Events (time-series)     Business data + entity graph    Documents (runbooks, postmortems)
 Postgres                 Postgres (nodes/edges tables)   pgvector + full-text (hybrid)
      ▲
 Event simulator (seeded incident scenarios)
```

## Decisions

| # | Decision | Why / trade-off |
|---|---|---|
| 1 | One Postgres 16 with pgvector for events, business data, the entity graph and vectors | One system to run and deploy on RDS; joins across data types. Recursive CTEs handle graph traversal at this scale, so no Neo4j or Pinecone. |
| 2 | Real MCP server (Python `mcp` SDK); the agent connects as an MCP client | Makes the MCP claim real, and the same tools work from Claude Desktop or any MCP client. |
| 3 | The agent inherits the caller's permissions; RBAC is enforced in the tool layer | The model can't talk its way past permissions. A viewer's investigation can't read customer PII or trigger actions. |
| 4 | Reads are autonomous; writes are proposals (`propose_action` → approval queue) | Human-in-the-loop for anything that changes state. |
| 5 | Structured outputs with Pydantic: `InvestigationReport{hypotheses, evidence[], root_cause, confidence, actions[]}` | Every piece of evidence cites a real event, entity or doc chunk ID. Evals check the citations exist, which catches hallucinations. All citable rows use prefixed IDs (`evt_`, `dep_`, `cfg_`, `ord_` ...). |
| 6 | Typed entity graph (service → host, deploy → service, order → customer → payment ...) | Enables `trace_dependencies` and `blast_radius`, so the agent reasons over relationships. |
| 7 | Hybrid RAG: vector + Postgres full-text, fused with RRF, filtered by document ACL at retrieval | Runbooks are full of exact terms where keyword search beats embeddings; ACL filtering keeps RBAC consistent in RAG. |
| 8 | Append-only, hash-chained audit log | Tamper-evident record of every tool call, enabling replay. |
| 9 | Model-agnostic LLM layer; Groq first, mock for tests | Groq's OpenAI-compatible API means one client also covers OpenAI and vLLM. Structured output uses JSON mode plus Pydantic validation with a repair retry, which works on every Groq model. Groq has no embeddings endpoint, so phase 2 embeds locally (a small open model on CPU). |
| 10 | Evals as a CI gate | Unit tests plus an eval suite over the seeded scenarios; merges blocked below thresholds. |
| 11 | Events in Postgres behind an EventBus abstraction, streamed over SSE | Kafka is overkill for a demo; the abstraction leaves room for Redis Streams or Kinesis. |
| 12 | React + Vite + TypeScript + TanStack Query + Tailwind | No SSR needed. |
| 13 | AWS: ECS Fargate, RDS, S3, Secrets Manager, CloudWatch, all in Terraform | Reproducible; tear down between demos to keep cost near zero. |

## Simulator

`backend/src/opsintel/simulator/` holds the company topology (`company.py`), the engine
(`engine.py`) and the scenarios (`scenarios.py`).

- A scenario declares causes only: deploys, config changes, faults (error rate and latency
  added to a service, optionally scoped to hosts or a customer region) and traffic surges.
- The engine simulates 120 one-minute ticks. Each service has a traffic share, capacity,
  baseline error rate and p99. Errors propagate up hard dependencies
  (`1 - (1 - own) * Π(1 - callee)`), latency excess propagates at 80%, and utilisation above
  capacity adds queueing latency and load shedding.
- Metrics, Prometheus-style alerts (3-minute `for` clause), logs, orders, payments and
  shipments are all derived from that state. Each failed order carries the error code of the
  dominant cause on its path.
- Generation is deterministic for a given scenario, seed and anchor time. Ground truth is
  stored in `scenario_runs`, which no agent tool will expose.

## Evals (phase 8)

- Root-cause accuracy against the injected ground truth.
- Grounding: share of cited evidence IDs that exist and support the claim.
- Tool-use correctness and wasted steps.
- Retrieval recall@k for the relevant runbook.
- RBAC red-team cases (prompt injection in documents and events); target is exactly 0 violations.
- Cost and latency per investigation over time.
