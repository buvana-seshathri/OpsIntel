from fastapi import FastAPI
from sqlalchemy import text

from opsintel import __version__
from opsintel.db.session import get_engine
from opsintel.simulator import SCENARIOS

app = FastAPI(title="OpsIntel", version=__version__)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    with get_engine().connect() as conn:
        conn.execute(text("SELECT 1"))
    return {"status": "ok", "version": __version__}


@app.get("/scenarios")
def list_scenarios() -> list[dict[str, str]]:
    return [{"key": s.key, "title": s.title, "question": s.question} for s in SCENARIOS.values()]
