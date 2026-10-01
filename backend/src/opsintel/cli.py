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
