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
| 4 | MCP server (12 tools), JWT auth, RBAC in the tool layer, action proposals | ✅ |
| 5 | Agent loop over MCP, structured grounded reports, API with SSE trace | ✅ |
| 6 | Hash-chained audit log, two-person approval queue, replay | ✅ |
| 7 | React dashboard | next |
| 8 | Eval harness (retrieval + agent + red team) and CI gating | ✅ |
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

To use Groq, set `LLM_PROVIDER=groq`, `GROQ_API_KEY` and optionally `LLM_MODEL` (default
`openai/gpt-oss-120b`) in `.env`. In a cloud sandbox, `api.groq.com` must be on the network
allowlist.

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

## MCP server and access control

`opsintel mcp` serves 12 tools over MCP:

| Tool | Permission | Purpose |
|---|---|---|
| `list_services` | read:telemetry | service catalogue, owners, dependencies |
| `query_events` | read:telemetry | logs/alerts/changes; filters and `group_by` (error_code, host, ...) |
| `get_metrics` | read:telemetry | bucketed series + baseline vs last 10 min + change point |
| `list_changes` | read:telemetry | deploys and config/flag changes |
| `get_entity`, `trace_dependencies`, `blast_radius`, `find_path` | read:graph | entity graph |
| `search_docs` | read:docs | hybrid search, filtered by document ACL |
| `query_orders` | read:orders | order outcomes grouped by reason/region/tier; customer IDs only |
| `get_customer` | read:pii | customer contact details |
| `propose_action` | propose:action | queue rollback/failover/drain/... for human approval |

Roles: **viewer** (all reads except PII), **responder** (+ propose and decide actions),
**admin** (+ PII). Checks happen in `ToolRunner.call`, which every transport goes through,
using the identity of the person who started the investigation. The model never chooses
its role, so nothing in a prompt, document or log line can widen access. Restricted
documents are filtered out of search results and graph lookups as well.

Identity: over HTTP each request carries a bearer JWT (`opsintel token alice --role
responder` mints one for development); over stdio the token is read from `OPSINTEL_TOKEN`
once at startup.

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "opsintel": {
      "command": "uv",
      "args": ["--directory", "/path/to/OpsIntel/backend", "run", "opsintel", "mcp"],
      "env": {"OPSINTEL_TOKEN": "<output of opsintel token you --role viewer>"}
    }
  }
}
```

`propose_action` checks that the target matches the action (a rollback needs a `dep_` ID)
and that every evidence ID exists, then stores a pending proposal. Nothing executes until
someone approves it (phase 6).

## Investigations

```bash
# needs LLM_PROVIDER=groq and GROQ_API_KEY
uv run opsintel investigate --scenario bad_deploy_payments --check
```

The agent (`agent/investigator.py`) has no database handle and no credentials. It
connects to the OpsIntel MCP server as an MCP client, and the server runs each call as
the person who asked, so the agent can't read more than they can. It is offered only the
tools that person may call, follows a triage → gather → correlate → hypothesize →
recommend method, and stops when it concludes or hits its tool-call budget.

Context is kept small for Groq's per-minute token limits: tool results are truncated
(3,500 characters by default) and wrapped in an explicit `untrusted-data` boundary,
and schema titles are stripped from tool definitions.

The final answer is an `InvestigationReport` (summary, root cause with entity ID and
confidence, supported and rejected hypotheses, evidence, recommended actions, affected
services), produced in JSON mode and validated with Pydantic, retrying once with the
validation errors if it doesn't parse. Grounding is then checked: every cited ID must
have appeared in a tool result the model actually saw, and any that didn't are listed
as `unseen_ids`.

API (bearer JWT):

| Method | Path | |
|---|---|---|
| POST | `/investigations` | start one; returns its ID (202) |
| GET | `/investigations/{id}` | status, report, grounding, token/tool/latency stats, trace |
| GET | `/investigations/{id}/events` | Server-Sent Events: the trace live, or replayed |
| GET | `/investigations` | your investigations (admins see all) |
| POST | `/scenarios/{key}/load` | admin only: load a scenario |

Investigations are visible only to the person who started them and to admins, since a
report can contain anything its creator could read.

## Audit, approvals and replay

**Audit log.** Every tool call (allowed or denied), model call (tokens, latency, model,
prompt version), investigation start/end and approval decision is appended to `audit_log`.
Each row stores `sha256(previous hash || canonical JSON of the row)`, and a database
trigger rejects UPDATE, DELETE and TRUNCATE. `opsintel audit verify` recomputes the chain
and names the first record that was modified, removed or reordered; `opsintel audit head`
prints the latest hash to anchor outside the database, since cutting off the tail is the
one change a chain cannot show by itself.

**Approval queue.** `propose_action` only queues. A responder or admin approves or
rejects (`opsintel actions approve act_... --as bob`, or `POST /actions/{id}/approve`).
Nobody can approve an action from their own investigation, decisions lock the row so an
action can't run twice, and approved actions run through an `Executor`. The simulated one
marks a rolled-back deploy as `rolled_back` and describes the other actions.

**Replay.** `opsintel replay inv_...` re-runs an investigation's recorded tool calls as the
original user and compares result digests with the audit log. With the same scenario
loaded every read reproduces; after the data changes (for example, once the rollback is
approved) the affected calls are reported as `differs`.

### First live runs (Groq, `openai/gpt-oss-120b`, free tier)

| Scenario | Root cause | Grounding | Tool calls | Tokens | Time |
|---|---|---|---|---|---|
| `bad_deploy_payments` | MATCH (`dep_…`, 90%) | 8/8 | 12 (budget hit) | 51k | 337 s |
| `bad_deploy_payments` | MATCH, rollback queued | 7/7 | 11 | 64k | 402 s |

Most of the time is spent waiting out free-tier per-minute rate limits.

## Layout

```
backend/
  src/opsintel/
    api/         FastAPI app
    db/          SQLAlchemy models, sessions
    graph/       entity graph builder and traversal queries
    tools/       tool registry/runner (RBAC, validation, audit hook) and the tool catalog
    mcp_server/  MCP adapter: stdio and HTTP with JWT bearer auth
    auth.py      roles, permissions, JWT issue/verify
    agent/       investigator loop, prompts, report schema, grounding check
    investigations.py, events.py   background runs, persistence, live event bus
    audit.py, actions.py, replay.py  hash-chained audit, approvals, replay
    llm/         provider-agnostic LLM interface (Groq/OpenAI-compatible, mock)
    rag/         corpus, chunking, embedders, ingestion, hybrid search
    simulator/   company topology, simulation engine, scenarios, loader
  migrations/    Alembic
  tests/
docs/DESIGN.md   architecture and decisions
```
