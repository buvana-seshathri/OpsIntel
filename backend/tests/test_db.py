import pytest
from alembic import command
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session

from opsintel.cli import alembic_config
from opsintel.db import models as m
from opsintel.simulator import SCENARIOS, generate
from opsintel.simulator.loader import load
from tests.conftest import ANCHOR

pytestmark = pytest.mark.db


def test_pgvector_extension_installed(engine: Engine) -> None:
    with engine.connect() as conn:
        assert conn.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")).scalar()


def test_migrations_round_trip(engine: Engine, db_url: str) -> None:
    command.downgrade(alembic_config(db_url), "base")
    command.upgrade(alembic_config(db_url), "head")


def test_load_scenario_then_replace(engine: Engine) -> None:
    first = generate(SCENARIOS["bad_deploy_payments"], 42, ANCHOR)
    with Session(engine) as s, s.begin():
        counts = load(s, first)
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(m.Event)) == counts["events"]
        assert s.scalar(select(func.count()).select_from(m.Order)) == counts["orders"]
        culprit = s.get(m.Deploy, first.ground_truth.root_cause_entity)
        assert culprit is not None and culprit.service_id == "payments-svc"

    second = generate(SCENARIOS["healthy"], 42, ANCHOR)
    with Session(engine) as s, s.begin():
        load(s, second)
    with Session(engine) as s:
        assert s.get(m.Deploy, first.ground_truth.root_cause_entity) is None
        keys = s.scalars(select(m.ScenarioRun.scenario_key)).all()
        assert keys == ["healthy"]
