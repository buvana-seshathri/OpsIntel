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

    from opsintel.audit import ToolCallAuditor
    from opsintel.auth import AuthError, verify_token
    from opsintel.mcp_server.server import create_server
    from opsintel.rag.embeddings import make_embedder
    from opsintel.tools import ToolRunner

    runner = ToolRunner(session_scope, make_embedder, observers=[ToolCallAuditor(session_scope)])
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

    from opsintel.audit import ToolCallAuditor

    svc = InvestigationService(
        ToolRunner(session_scope, make_embedder, observers=[ToolCallAuditor(session_scope)]),
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


audit_app = typer.Typer(help="Inspect and verify the audit log.", no_args_is_help=True)
app.add_typer(audit_app, name="audit")


@audit_app.command("verify")
def audit_verify() -> None:
    """Recompute the hash chain; exits 1 if any record was changed, removed or reordered."""
    from opsintel.audit import verify_chain

    with session_scope() as s:
        r = verify_chain(s)
    if r.ok:
        typer.echo(f"OK: {r.records} records, chain head {r.head}")
    else:
        typer.echo(f"BROKEN at seq {r.first_bad_seq}: {r.reason}")
        raise typer.Exit(1)


@audit_app.command("head")
def audit_head() -> None:
    """Print the latest hash, to anchor somewhere outside this database."""
    from opsintel.audit import head

    with session_scope() as s:
        typer.echo(head(s))


actions_app = typer.Typer(help="The human approval queue.", no_args_is_help=True)
app.add_typer(actions_app, name="actions")


@actions_app.command("list")
def actions_list(
    status: Annotated[str | None, typer.Option(help="pending | executed | ...")] = "pending",
) -> None:
    """List proposed actions."""
    from opsintel.actions import list_proposals
    from opsintel.auth import Principal

    with session_scope() as s:
        for p in list_proposals(s, Principal("cli", "admin"), status):
            typer.echo(
                f"{p.id}  {p.status:9s} {p.type} {p.target}  by {p.proposed_by}"
                f"  ({p.investigation_id})\n    {p.rationale[:200]}"
            )


def _decide_cli(
    proposal_id: str, approve: bool, subject: str, role: str, comment: str | None
) -> None:
    from opsintel import actions
    from opsintel.auth import Principal

    try:
        with session_scope() as s:
            p = actions.decide(s, Principal(subject, role), proposal_id, approve, comment)
            result = f" - {p.execution_result}" if p.execution_result else ""
            typer.echo(f"{p.id}: {p.status}{result}")
    except actions.ActionError as e:
        typer.echo(f"refused: {e}")
        raise typer.Exit(1) from e


@actions_app.command("approve")
def actions_approve(
    proposal_id: str,
    subject: Annotated[str, typer.Option("--as", help="Who is approving")],
    role: Annotated[str, typer.Option(help="responder | admin")] = "responder",
    comment: Annotated[str | None, typer.Option(help="Why")] = None,
) -> None:
    """Approve (and execute) a pending action."""
    _decide_cli(proposal_id, True, subject, role, comment)


@actions_app.command("reject")
def actions_reject(
    proposal_id: str,
    subject: Annotated[str, typer.Option("--as", help="Who is rejecting")],
    role: Annotated[str, typer.Option(help="responder | admin")] = "responder",
    comment: Annotated[str | None, typer.Option(help="Why")] = None,
) -> None:
    """Reject a pending action."""
    _decide_cli(proposal_id, False, subject, role, comment)


@app.command()
def replay(investigation_id: str) -> None:
    """Re-run an investigation's recorded tool calls and check each result is identical."""
    from opsintel.audit import ToolCallAuditor
    from opsintel.rag.embeddings import make_embedder
    from opsintel.replay import replay_investigation
    from opsintel.tools import ToolRunner

    runner = ToolRunner(session_scope, make_embedder, observers=[ToolCallAuditor(session_scope)])
    out = replay_investigation(session_scope, runner, investigation_id)
    for d in out["details"]:
        typer.echo(f"  #{d['seq']:<5} {d['tool']:20s} {d['outcome']}")
    typer.echo(
        f"{out['reproduced']}/{out['calls']} reproduced, {out['differs']} differ, "
        f"{out['skipped (write)']} writes skipped"
    )


eval_app = typer.Typer(help="Evaluation suites and the CI gate.", no_args_is_help=True)
app.add_typer(eval_app, name="eval")


def _write(out: Path | None, data: dict[str, object]) -> None:
    import json

    text = json.dumps(data, indent=2, default=str)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n")
        typer.echo(f"wrote {out}")


@eval_app.command("retrieval")
def eval_retrieval(
    out: Annotated[Path | None, typer.Option(help="Write results JSON here")] = None,
) -> None:
    """Recall of the scenario runbooks for evidence queries and raw symptom questions."""
    from opsintel.evals.retrieval import retrieval_eval
    from opsintel.rag.embeddings import make_embedder
    from opsintel.rag.ingest import ingest_corpus

    embedder = make_embedder()
    with session_scope() as s:
        ingest_corpus(s, embedder)
        result = retrieval_eval(s, embedder)
    for c in result["cases"]:
        typer.echo(
            f"  {c['scenario']:26s} evidence rank {c['evidence_rank']}  "
            f"symptom rank {c['symptom_rank']}"
        )
    typer.echo(
        f"evidence recall@3 {result['evidence_recall_at_3']:.2f}  "
        f"symptom recall@5 {result['symptom_recall_at_5']:.2f}  ({result['embedder']})"
    )
    _write(out, {"suite": "retrieval", "metrics": result})


@eval_app.command("agent")
def eval_agent(
    case: Annotated[list[str] | None, typer.Option(help="Only these case names")] = None,
    out: Annotated[Path | None, typer.Option(help="Write results JSON here")] = None,
    max_tool_calls: Annotated[int, typer.Option(help="Tool call budget per case")] = 12,
) -> None:
    """Run the agent on every scenario and red-team case and score it (needs an LLM)."""
    import anyio

    from opsintel.agent.investigator import AgentConfig
    from opsintel.agent.prompts import PROMPT_VERSION
    from opsintel.config import get_settings
    from opsintel.evals.cases import default_cases
    from opsintel.evals.metrics import CaseScore, aggregate, as_rows
    from opsintel.evals.runner import run_cases
    from opsintel.llm import make_llm
    from opsintel.rag.embeddings import make_embedder

    cases = [c for c in default_cases() if not case or c.name in case]
    if not cases:
        raise typer.BadParameter("no matching cases")
    llm = make_llm()  # validates provider settings before the long run

    def report(s: CaseScore) -> None:
        typer.echo(
            f"  {s.case:28s} {s.status:9s} root_cause={'Y' if s.root_cause_correct else 'n'} "
            f"action={'Y' if s.action_correct else 'n'} grounded={s.grounded_exist:.2f} "
            f"tools={s.tool_calls} violation={'YES' if s.rbac_violation else 'no'} "
            f"{s.latency_s:.0f}s"
        )

    scores = anyio.run(
        lambda: run_cases(
            cases,
            make_llm,
            session_scope,
            make_embedder(),
            AgentConfig(max_tool_calls=max_tool_calls),
            on_case=report,
        )
    )
    agg = aggregate(scores)
    typer.echo("\n" + "\n".join(f"  {k}: {v}" for k, v in agg.items()))
    _write(
        out,
        {
            "suite": "agent",
            "meta": {
                "model": llm.model,
                "provider": get_settings().llm_provider,
                "prompt_version": PROMPT_VERSION,
                "max_tool_calls": max_tool_calls,
                "run_at": datetime.now(UTC),
            },
            "metrics": agg,
            "cases": as_rows(scores),
        },
    )


@eval_app.command("gate")
def eval_gate(
    results: Annotated[list[Path], typer.Argument(help="Result files from eval runs")],
    summary: Annotated[
        Path | None, typer.Option(help="Append a Markdown summary here ($GITHUB_STEP_SUMMARY)")
    ] = None,
) -> None:
    """Fail (exit 1) if any result misses its threshold in evals/thresholds.json."""
    import json

    from opsintel.evals.gate import check, load_thresholds

    limits = load_thresholds()
    failures: list[str] = []
    lines = [
        "## OpsIntel evals",
        "",
        "| suite | metric | value | threshold | |",
        "|---|---|---|---|---|",
    ]
    for path in results:
        data = json.loads(path.read_text())
        suite = data["suite"]
        suite_limits = limits.get(suite, {})
        failed = check(data["metrics"], suite_limits)
        failures += [f"{suite}: {f}" for f in failed]
        for name, bound in suite_limits.items():
            value = data["metrics"].get(name)
            ok = not any(f.startswith(f"{name} ") or f.startswith(f"{name}:") for f in failed)
            limit = " ".join(f"{k} {v}" for k, v in bound.items())
            lines.append(f"| {suite} | {name} | {value} | {limit} | {'✅' if ok else '❌'} |")
    text = "\n".join(lines) + "\n"
    typer.echo(text)
    if summary:
        with summary.open("a") as f:
            f.write(text)
    if failures:
        typer.echo("GATE FAILED:\n  " + "\n  ".join(failures))
        raise typer.Exit(1)
    typer.echo("Gate passed.")
