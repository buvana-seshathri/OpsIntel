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
