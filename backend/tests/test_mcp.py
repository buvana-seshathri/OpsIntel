import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import httpx
import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from pydantic import SecretStr
from sqlalchemy import Engine

from opsintel.auth import Principal, issue_token
from opsintel.config import Settings
from opsintel.mcp_server.server import create_server
from opsintel.rag.embeddings import HashingEmbedder
from opsintel.tools import REGISTRY, ToolRunner
from tests.test_tools import load_world, session_factory

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def runner(engine: Engine) -> ToolRunner:
    load_world(engine)
    factory: Any = session_factory(engine)
    return ToolRunner(factory, HashingEmbedder)


async def test_in_process_client_lists_and_calls_tools(runner: ToolRunner) -> None:
    server = create_server(runner, Principal("cli", "viewer"))
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert set(tools) == set(REGISTRY)
        assert "Requires: read:pii" in (tools["get_customer"].description or "")
        assert tools["query_events"].input_schema["properties"]["min_severity"]["enum"]

        ok = await client.call_tool("trace_dependencies", {"service": "checkout-svc"})
        assert not ok.is_error and ok.structured_content
        assert ok.structured_content["dependencies"][0]["depth"] == 1

        denied = await client.call_tool(
            "propose_action",
            {
                "type": "page_team",
                "target": "team:payments-team",
                "rationale": "trying as a viewer",
                "evidence_ids": ["service:payments-svc"],
            },
        )
        assert denied.is_error
        assert "lacks 'propose:action'" in denied.content[0].text  # type: ignore[union-attr]


async def test_server_without_identity_refuses(runner: ToolRunner) -> None:
    async with Client(create_server(runner)) as client:
        result = await client.call_tool("list_services", {})
        assert result.is_error and "unauthenticated" in result.content[0].text  # type: ignore[union-attr]


@pytest.fixture(scope="module")
def http_server(runner: ToolRunner) -> Iterator[tuple[str, Settings]]:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    settings = Settings(
        jwt_secret=SecretStr("http-test-secret-0123456789-abcdefgh"), mcp_base_url=base
    )
    app = create_server(runner, http_auth=True, settings=settings).streamable_http_app()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    yield f"{base}/mcp", settings
    server.should_exit = True
    thread.join(timeout=5)


def test_http_rejects_missing_or_forged_tokens(http_server: tuple[str, Settings]) -> None:
    url, _ = http_server
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    assert httpx.post(url, json=body).status_code == 401
    forged = issue_token(
        "mallory",
        "admin",
        settings=Settings(jwt_secret=SecretStr("not-the-server-secret-0123456789-abcd")),
    )
    resp = httpx.post(url, json=body, headers={"Authorization": f"Bearer {forged}"})
    assert resp.status_code == 401


async def _call(url: str, token: str, tool: str, args: dict[str, Any]) -> Any:
    headers = {"Authorization": f"Bearer {token}"}
    async with (
        httpx2.AsyncClient(headers=headers, timeout=30) as http,
        Client(
            streamable_http_client(url, http_client=http)  # type: ignore[arg-type]
        ) as client,
    ):
        return await client.call_tool(tool, args)


async def test_http_identity_comes_from_the_token(http_server: tuple[str, Settings]) -> None:
    url, settings = http_server
    viewer = issue_token("vera", "viewer", settings=settings)
    admin = issue_token("ada", "admin", settings=settings)
    args = {"query": "customer data access policy", "k": 10}
    as_viewer = await _call(url, viewer, "search_docs", args)
    as_admin = await _call(url, admin, "search_docs", args)
    docs = lambda r: {h["document_id"] for h in r.structured_content["results"]}  # noqa: E731
    assert "doc_customer-data-access-policy" not in docs(as_viewer)
    assert "doc_customer-data-access-policy" in docs(as_admin)
    denied = await _call(url, viewer, "get_customer", {"customer_id": "cus_x"})
    assert denied.is_error
