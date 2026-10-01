from fastapi.testclient import TestClient

from opsintel.api.main import app
from opsintel.simulator import SCENARIOS


def test_list_scenarios() -> None:
    body = TestClient(app).get("/scenarios").json()
    assert [s["key"] for s in body] == list(SCENARIOS)
