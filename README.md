# OpsIntel

An AI incident-investigation agent for a simulated e-commerce platform. Ask it
*"Checkout failures jumped 8× in the last 20 minutes. Why, and what should we do?"* and it
queries events, metrics, deploys and the service dependency graph through an MCP server,
pulls the matching runbook, and returns a root cause backed by cited evidence. Anything that
changes state, such as a rollback, waits in an approval queue for a human.

A scenario simulator injects known root causes, which makes the demo realistic and gives the
eval suite exact ground truth. See [docs/DESIGN.md](docs/DESIGN.md) for the architecture and
design decisions.

## Status

| Phase | Deliverable | State |
|---|---|---|
| 0 | Monorepo scaffold, docker-compose, lint/test CI | ✅ |
| 1 | Data model and migrations, synthetic company, simulator with 8 incident scenarios + a healthy control | ✅ |
| 2 | Runbook/postmortem corpus, chunking, local embeddings, hybrid search with ACLs | ✅ |
| 3 | Entity graph (records, log and document extraction), traversal, blast radius | ✅ |
| 4 | MCP server, JWT auth, RBAC | next |
| 5 | Agent loop, structured reports, SSE trace | |
| 6 | Audit log, approval queue, replay | |
| 7 | React dashboard | |
| 8 | Eval harness and CI gating | |
| 9 | Terraform + AWS | |

## Quickstart

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/) and Docker (for Postgres 16 + pgvector).

```bash
cp .env.example .env          # LLM_PROVIDER=mock works with no API key
docker compose up -d db
make install migrate
make simulate SCENARIO=bad_deploy_payments
make ingest                   # chunk + embed + index the runbooks
cd backend && uv run opsintel search "PAYFLOW_POOL_EXHAUSTED payments-svc"
make test                     # needs an opsintel_test database, see below
```

`make test` runs the database tests against `TEST_DATABASE_URL`; create the database once with
`docker compose exec db createdb -U opsintel opsintel_test`. Without the variable those tests
are skipped and the rest still run.

To use Groq, set `LLM_PROVIDER=groq`, `GROQ_API_KEY` and optionally `LLM_MODEL` in `.env`.

## Scenarios

`opsintel scenarios` lists them. Each loads a 2-hour window ending now, and every scenario
except the control includes a red herring (an innocent change that looks guilty).

| Key | Root cause | Red herring |
|---|---|---|
| `bad_deploy_payments` | payments-svc deploy shrinks the payflow connection pool | shipping-svc deploy |
| `inventory_db_contention` | cron schedule change makes a table-scanning job run every 15 min | inventory-svc deploy |
| `payflow_outage` | third-party processor degraded; fail over, don't roll back | payments-svc logging deploy |
| `carrier_cert_expired` | shipping-svc mTLS client cert expired | checkout-svc deploy |
| `tax_engine_flag` | feature flag enables a tax engine that breaks EU orders | inventory-svc deploy |
| `host_disk_full` | one payments host out of disk | verbose-logging deploy |
| `flash_sale` | promo traffic surge saturates inventory-svc | api-gateway deploy |
| `checkout_memory_leak` | leak from a deploy 90 min ago; OOM kills start 20 min ago | the most recent deploy |
| `healthy` | nothing is wrong (false-positive check) | — |

Symptoms are not hand-written. The engine simulates each service minute by minute, propagates
errors and latency up the dependency graph, and derives metrics, alerts, logs, orders, payments
and shipments from that one model, so the data is internally consistent.

## Document search

`backend/src/opsintel/rag/corpus/` holds 9 runbooks, 4 postmortems and 2 policies written for
the scenarios. Ingestion splits them by heading, embeds each chunk locally and stores it in
pgvector next to a full-text index. Search runs both, fuses the rankings with Reciprocal Rank
Fusion, and filters by each document's `acl` roles inside the SQL, so restricted documents
never leave the database for a caller without access. Chunk IDs (`chk_<doc>_<n>`) are
citable evidence.

Groq has no embeddings API, so embeddings are local: `EMBEDDER=fastembed` (default) runs
BAAI/bge-small-en-v1.5 on CPU; `EMBEDDER=hashing` is an offline lexical fallback used by tests.

## Entity graph

`entities` and `edges` are rebuilt after every scenario load or document ingest from three
origins:

- **records**: team `owns` service, service `depends_on` service (hard/soft), service
  `runs_on` host, deploy `deployed_to` service, config change `changed` service, and order,
  payment, shipment and customer links. Customer nodes carry no PII.
- **events**: service/host `emitted` error code (count, first/last seen, sample event IDs);
  caller `saw_failures_from` upstream.
- **documents**: document `covers` service, and `mentions` service/host/error code, with the
  chunk IDs that mention it.

So a culprit deploy, the error code it causes, and the runbook for that error code are
connected. Traversals: `trace_dependencies` and `blast_radius` are recursive CTEs that mark
whether every hop is a hard dependency (failure propagates) or not (degrades only);
`find_path` is a bounded BFS. Try `uv run opsintel graph host:payments-svc-02` after loading
`host_disk_full`.

## Layout

```
backend/
  src/opsintel/
    api/         FastAPI app
    db/          SQLAlchemy models, sessions
    graph/       entity graph builder and traversal queries
    llm/         provider-agnostic LLM interface (Groq/OpenAI-compatible, mock)
    rag/         corpus, chunking, embedders, ingestion, hybrid search
    simulator/   company topology, simulation engine, scenarios, loader
  migrations/    Alembic
  tests/
docs/DESIGN.md   architecture and decisions
```
