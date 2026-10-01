import json
import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from opsintel.api.main import app
from opsintel.auth import issue_token
from opsintel.investigations import InvestigationService
from opsintel.llm import MockLLM
from opsintel.rag.embeddings import HashingEmbedder
from opsintel.simulator import SCENARIOS
from opsintel.tools import ToolRunner
from tests.scripted_sre import ScriptedSRE
from tests.test_tools import DS, load_world, session_factory


def auth(subject: str, role: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token(subject, role)}"}


def test_list_scenarios() -> None:
    body = TestClient(app).get("/scenarios").json()
    assert [s["key"] for s in body] == list(SCENARIOS)


@pytest.fixture
def client(engine: Engine) -> Iterator[TestClient]:
    load_world(engine)
    factory: Any = session_factory(engine)
    app.state.investigations = InvestigationService(
        ToolRunner(factory, HashingEmbedder),
        lambda: MockLLM(ScriptedSRE(), model="scripted-sre"),
        session_factory=factory,
    )
    with TestClient(app) as c:
        yield c
    del app.state.investigations


def wait_done(client: TestClient, inv_id: str, headers: dict[str, str]) -> dict[str, Any]:
    deadline = time.time() + 30
    while time.time() < deadline:
        body: dict[str, Any] = client.get(f"/investigations/{inv_id}", headers=headers).json()
        if body["status"] != "running":
            return body
        time.sleep(0.05)
    raise AssertionError("investigation did not finish")


@pytest.mark.db
def test_requires_a_valid_token(client: TestClient) -> None:
    assert client.post("/investigations", json={"question": "why?"}).status_code == 401
    bad = {"Authorization": "Bearer not-a-jwt"}
    assert (
        client.post("/investigations", json={"question": "why now?"}, headers=bad).status_code
        == 401
    )


@pytest.mark.db
def test_investigation_lifecycle(client: TestClient) -> None:
    rita = auth("rita", "responder")
    started = client.post("/investigations", json={"question": DS.question}, headers=rita)
    assert started.status_code == 202
    inv_id = started.json()["id"]

    body = wait_done(client, inv_id, rita)
    assert body["status"] == "completed", body["error"]
    assert body["report"]["root_cause"]["entity_id"] == DS.ground_truth.root_cause_entity
    assert body["grounding"]["ratio"] == 1.0
    assert body["stats"]["tool_calls"] == 4 and body["model"] == "scripted-sre"
    assert body["trace"][0]["type"] == "started"

    with client.stream("GET", f"/investigations/{inv_id}/events", headers=rita) as resp:
        assert resp.headers["content-type"].startswith("text/event-stream")
        frames = [
            json.loads(line[6:])
            for line in resp.iter_lines()
            if line.startswith("data: ") and line != "data: {}"
        ]
    assert [f["type"] for f in frames][0] == "started"
    assert frames[-1]["type"] == "report"
    assert [f["seq"] for f in frames] == list(range(1, len(frames) + 1))

    listed = client.get("/investigations", headers=rita).json()
    assert inv_id in {i["id"] for i in listed}


@pytest.mark.db
def test_other_users_cannot_see_an_investigation(client: TestClient) -> None:
    rita = auth("rita", "responder")
    inv_id = client.post("/investigations", json={"question": DS.question}, headers=rita).json()[
        "id"
    ]
    wait_done(client, inv_id, rita)
    mallory = auth("mallory", "responder")
    assert client.get(f"/investigations/{inv_id}", headers=mallory).status_code == 404
    assert client.get(f"/investigations/{inv_id}/events", headers=mallory).status_code == 404
    assert inv_id not in {i["id"] for i in client.get("/investigations", headers=mallory).json()}
    assert client.get(f"/investigations/{inv_id}", headers=auth("ada", "admin")).status_code == 200


@pytest.mark.db
def test_failed_investigation_is_recorded(client: TestClient) -> None:
    client.app.state.investigations.llm_factory = lambda: MockLLM([])  # type: ignore[attr-defined]
    rita = auth("rita", "responder")
    inv_id = client.post("/investigations", json={"question": DS.question}, headers=rita).json()[
        "id"
    ]
    body = wait_done(client, inv_id, rita)
    assert body["status"] == "failed" and "script exhausted" in body["error"]


@pytest.mark.db
def test_only_admins_load_scenarios(client: TestClient) -> None:
    resp = client.post("/scenarios/healthy/load", headers=auth("rita", "responder"))
    assert resp.status_code == 403
