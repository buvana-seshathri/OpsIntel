"""`opsintel` command line: database migrations and scenario loading."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from alembic import command
from alembic.config import Config

from opsintel.db.session import session_scope
from opsintel.graph.build import rebuild_graph
from opsintel.simulator import SCENARIOS, generate
from opsintel.simulator.loader import load

app = typer.Typer(no_args_is_help=True, add_completion=False)

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


def alembic_config(url: str | None = None) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_INI.parent / "migrations"))
    if url:
        cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@app.command()
def migrate() -> None:
    """Apply all database migrations."""
    command.upgrade(alembic_config(), "head")


@app.command()
def scenarios() -> None:
    """List the incident scenarios the simulator can load."""
    for key, s in SCENARIOS.items():
        typer.echo(f"{key:26s} {s.title}")


@app.command()
def simulate(
    scenario: Annotated[str, typer.Argument(help="Scenario key; see `opsintel scenarios`.")],
    seed: Annotated[int, typer.Option(help="Same seed and anchor give the same data.")] = 42,
    anchor: Annotated[
        datetime | None, typer.Option(help="End of the 2-hour window (UTC). Defaults to now.")
    ] = None,
) -> None:
    """Replace the database contents with a freshly generated scenario."""
    if scenario not in SCENARIOS:
        raise typer.BadParameter(f"unknown scenario {scenario!r}; try `opsintel scenarios`")
    end = (anchor or datetime.now(UTC)).replace(tzinfo=UTC)
    ds = generate(SCENARIOS[scenario], seed, end)
    with session_scope() as session:
        counts = load(session, ds)
        graph = rebuild_graph(session)
    typer.echo(
        f"Loaded {scenario} (seed {seed}), window {ds.window_start:%H:%M}-{ds.window_end:%H:%M} UTC"
    )
    for table, n in counts.items():
        typer.echo(f"  {table:22s} {n:6d}")
    typer.echo(
        f"  graph: {graph['edges']} edges, "
        f"{sum(v for k, v in graph.items() if k != 'edges')} entities"
    )
    typer.echo(f"\nQuestion: {ds.question}")


@app.command("ingest-docs")
def ingest_docs() -> None:
    """Chunk, embed and index the runbook/postmortem corpus (idempotent)."""
    from opsintel.rag.embeddings import make_embedder
    from opsintel.rag.ingest import ingest_corpus

    embedder = make_embedder()
    with session_scope() as session:
        r = ingest_corpus(session, embedder)
        rebuild_graph(session)
    typer.echo(
        f"Embedder {embedder.name}: {r.added} added, {r.updated} updated, "
        f"{r.unchanged} unchanged, {r.removed} removed, {r.chunks} chunks written"
    )


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="Free-text query.")],
    role: Annotated[str, typer.Option(help="Caller role used for document ACLs.")] = "responder",
    k: Annotated[int, typer.Option(help="Number of results.")] = 5,
) -> None:
    """Hybrid search over the indexed documents."""
    from opsintel.rag.embeddings import make_embedder
    from opsintel.rag.search import hybrid_search

    with session_scope() as session:
        hits = hybrid_search(session, make_embedder(), query, roles=[role], k=k)
    for h in hits:
        typer.echo(
            f"{h.score:.4f}  vec#{h.vector_rank or '-':<3} kw#{h.keyword_rank or '-':<3} "
            f"{h.chunk_id}  [{h.heading}]"
        )


@app.command()
def graph(
    entity: Annotated[str, typer.Argument(help="Entity ID, e.g. service:payments-svc")],
    to: Annotated[str | None, typer.Option(help="Show the shortest path to this entity.")] = None,
) -> None:
    """Inspect the entity graph: neighbours, blast radius, or a path."""
    import json

    from opsintel.graph.queries import blast_radius, find_path, get_entity

    with session_scope() as session:
        if to:
            out: object = find_path(session, entity, to)
        else:
            out = {
                "entity": get_entity(session, entity),
                "blast_radius": blast_radius(session, entity),
            }
    typer.echo(json.dumps(out, indent=2, default=str))


@app.command()
def token(
    subject: Annotated[str, typer.Argument(help="Who the token identifies, e.g. alice")],
    role: Annotated[str, typer.Option(help="viewer | responder | admin")] = "responder",
    ttl_hours: Annotated[int, typer.Option(help="Lifetime in hours")] = 12,
) -> None:
    """Mint a signed access token (development: there is no login service yet)."""
    from opsintel.auth import issue_token

    typer.echo(issue_token(subject, role, ttl_seconds=ttl_hours * 3600))


@app.command()
def mcp(
    transport: Annotated[str, typer.Option(help="stdio | http")] = "stdio",
    port: Annotated[int, typer.Option(help="HTTP port")] = 8001,
    host: Annotated[str, typer.Option(help="HTTP bind address")] = "127.0.0.1",
) -> None:
    """Serve the OpsIntel tools over MCP.

    stdio (Claude Desktop, local agents): identity comes from the OPSINTEL_TOKEN environment
    variable, verified once at startup. http: every request must carry a bearer token.
    """
    import os

    import anyio
    import uvicorn

    from opsintel.auth import AuthError, verify_token
    from opsintel.mcp_server.server import create_server
    from opsintel.rag.embeddings import make_embedder
    from opsintel.tools import ToolRunner

    runner = ToolRunner(session_scope, make_embedder)
    if transport == "stdio":
        raw = os.environ.get("OPSINTEL_TOKEN")
        if not raw:
            raise typer.BadParameter("set OPSINTEL_TOKEN (see `opsintel token`)")
        try:
            principal = verify_token(raw)
        except AuthError as e:
            raise typer.BadParameter(str(e)) from e
        anyio.run(create_server(runner, principal).run_stdio_async)
    elif transport == "http":
        app_ = create_server(runner, http_auth=True).streamable_http_app()
        uvicorn.run(app_, host=host, port=port)
    else:
        raise typer.BadParameter("transport must be stdio or http")


@app.command()
def investigate(
    question: Annotated[
        str | None, typer.Argument(help="Defaults to the scenario's question")
    ] = None,
    scenario: Annotated[
        str | None, typer.Option(help="Load this scenario first (replaces current data)")
    ] = None,
    role: Annotated[str, typer.Option(help="viewer | responder | admin")] = "responder",
    subject: Annotated[str, typer.Option(help="Who is asking")] = "cli-user",
    check: Annotated[bool, typer.Option(help="Compare with the scenario's ground truth")] = False,
    max_tool_calls: Annotated[int, typer.Option(help="Tool call budget")] = 12,
) -> None:
    """Run an investigation in the terminal, printing the agent's steps as they happen."""
    import json

    import anyio
    from sqlalchemy import select

    from opsintel.agent.investigator import AgentConfig
    from opsintel.auth import Principal
    from opsintel.config import get_settings
    from opsintel.db.models import Investigation, ScenarioRun
    from opsintel.investigations import InvestigationService
    from opsintel.llm import make_llm
    from opsintel.rag.embeddings import make_embedder
    from opsintel.tools import ToolRunner

    if get_settings().llm_provider == "mock":
        raise typer.BadParameter("LLM_PROVIDER=mock has no script; set LLM_PROVIDER=groq")
    try:
        make_llm()
    except ValueError as e:
        raise typer.BadParameter(str(e)) from e
    if scenario:
        if scenario not in SCENARIOS:
            raise typer.BadParameter(f"unknown scenario {scenario!r}")
        ds = generate(SCENARIOS[scenario], 42, datetime.now(UTC))
        with session_scope() as s:
            load(s, ds)
            rebuild_graph(s)
        question = question or ds.question
        typer.echo(f"Loaded scenario {scenario}")
    if not question:
        raise typer.BadParameter("give a question or --scenario")

    svc = InvestigationService(
        ToolRunner(session_scope, make_embedder),
        make_llm,
        config=AgentConfig(max_tool_calls=max_tool_calls),
    )

    async def main() -> str:
        inv_id = svc.start(question, Principal(subject, role))
        typer.echo(f"{inv_id}: {question}\n")
        async for e in svc.bus.subscribe(inv_id):
            d = e["data"]
            if e["type"] == "tool_call":
                typer.echo(f"  -> {d['tool']}({json.dumps(d['arguments'])})")
            elif e["type"] == "tool_result":
                typer.echo(
                    f"     {d['chars']} chars, {d['ids_returned']} citable IDs"
                    + (" (truncated)" if d["truncated"] else "")
                )
            elif e["type"] == "tool_error":
                typer.echo(f"     ERROR {d['error']}")
            elif e["type"] == "thought":
                typer.echo(f"  .. {d['text'][:300]}")
            elif e["type"] == "failed":
                typer.echo(f"FAILED: {d['error']}")
        await svc.wait_all()
        return inv_id

    inv_id = anyio.run(main)
    with session_scope() as s:
        row = s.get(Investigation, inv_id)
        assert row is not None
        if row.status != "completed" or row.report is None:
            raise typer.Exit(1)
        report, stats, grounding = row.report, row.stats or {}, row.grounding or {}
        truth = s.scalar(select(ScenarioRun.ground_truth)) if check else None
    rc = report["root_cause"]
    typer.echo(f"\nSummary: {report['summary']}")
    typer.echo(
        f"Root cause ({rc['kind']}, {rc['confidence']:.0%}): {rc['entity_id']} - "
        f"{rc['description']}"
    )
    for a in report.get("recommended_actions", []):
        typer.echo(f"Action: {a['type']} {a['target']} (proposal {a.get('proposal_id')})")
    typer.echo(
        f"Grounding: {grounding.get('seen_in_tool_results')}/{grounding.get('cited')} "
        f"citations returned by tools; unseen {grounding.get('unseen_ids')}"
    )
    typer.echo(
        f"Cost: {stats.get('prompt_tokens')} prompt + {stats.get('completion_tokens')} "
        f"completion tokens, {stats.get('tool_calls')} tool calls, "
        f"{stats.get('latency_ms', 0) / 1000:.1f}s"
    )
    if truth is not None:
        ok = rc["entity_id"] == truth["root_cause_entity"] or (
            truth["root_cause_kind"] == "none" and rc["kind"] == "none"
        )
        typer.echo(
            f"Ground truth: {truth['root_cause_kind']} {truth['root_cause_entity']} -> "
            f"{'MATCH' if ok else 'MISS'}"
        )
