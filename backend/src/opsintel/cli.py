"""`opsintel` command line: database migrations and scenario loading."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from alembic import command
from alembic.config import Config

from opsintel.db.session import session_scope
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
    typer.echo(
        f"Loaded {scenario} (seed {seed}), window {ds.window_start:%H:%M}-{ds.window_end:%H:%M} UTC"
    )
    for table, n in counts.items():
        typer.echo(f"  {table:22s} {n:6d}")
    typer.echo(f"\nQuestion: {ds.question}")


@app.command("ingest-docs")
def ingest_docs() -> None:
    """Chunk, embed and index the runbook/postmortem corpus (idempotent)."""
    from opsintel.rag.embeddings import make_embedder
    from opsintel.rag.ingest import ingest_corpus

    embedder = make_embedder()
    with session_scope() as session:
        r = ingest_corpus(session, embedder)
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
