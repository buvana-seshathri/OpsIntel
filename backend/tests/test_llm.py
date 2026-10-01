import json
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, SecretStr

from opsintel.config import Settings
from opsintel.llm import (
    Message,
    MockLLM,
    StructuredOutputError,
    ToolCall,
    ToolSpec,
    complete_structured,
    make_llm,
)
from opsintel.llm.openai_compat import OpenAICompatClient


class Verdict(BaseModel):
    root_cause: str
    confidence: float


async def test_mock_replays_script_in_order() -> None:
    llm = MockLLM(
        [Message(role="assistant", content="one"), Message(role="assistant", content="two")]
    )
    a = await llm.complete([Message(role="user", content="hi")])
    b = await llm.complete([Message(role="user", content="again")])
    assert (a.message.content, b.message.content) == ("one", "two")
    with pytest.raises(RuntimeError):
        await llm.complete([])


async def test_structured_output_repairs_invalid_json() -> None:
    llm = MockLLM(
        [
            Message(role="assistant", content='{"root_cause": "bad deploy"}'),  # missing field
            Message(role="assistant", content='{"root_cause": "bad deploy", "confidence": 0.9}'),
        ]
    )
    verdict, responses = await complete_structured(
        llm, [Message(role="user", content="why?")], Verdict
    )
    assert verdict == Verdict(root_cause="bad deploy", confidence=0.9)
    assert len(responses) == 2
    # The repair turn shows the model its own output and the validation error.
    repair_prompt = llm.calls[1][-1].content or ""
    assert "confidence" in repair_prompt


async def test_structured_output_gives_up() -> None:
    llm = MockLLM([Message(role="assistant", content="nope")] * 2)
    with pytest.raises(StructuredOutputError):
        await complete_structured(llm, [Message(role="user", content="why?")], Verdict)


async def test_openai_compat_translates_tools_and_tool_calls(monkeypatch: Any) -> None:
    client = OpenAICompatClient(model="llama-test", api_key="k", base_url="http://unused")
    seen: dict[str, Any] = {}

    async def fake_create(**kwargs: Any) -> Any:
        seen.update(kwargs)
        call = SimpleNamespace(
            id="call_1",
            type="function",
            function=SimpleNamespace(name="query_events", arguments='{"service": "payments-svc"}'),
        )
        bad = SimpleNamespace(
            id="call_2",
            type="function",
            function=SimpleNamespace(name="get_entity", arguments="{not json"),
        )
        return SimpleNamespace(
            model="llama-test",
            choices=[
                SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=[call, bad]))
            ],
            usage=SimpleNamespace(prompt_tokens=120, completion_tokens=30),
        )

    monkeypatch.setattr(client._client.chat.completions, "create", fake_create)
    history = [
        Message(role="user", content="investigate"),
        Message(role="assistant", tool_calls=[ToolCall(id="c0", name="t", arguments={"a": 1})]),
        Message(role="tool", tool_call_id="c0", content="result"),
    ]
    tool = ToolSpec(name="query_events", description="d", parameters={"type": "object"})
    resp = await client.complete(history, tools=[tool])

    assert seen["tools"][0]["function"]["name"] == "query_events"
    assert seen["messages"][1]["tool_calls"][0]["function"]["arguments"] == json.dumps({"a": 1})
    assert seen["messages"][2]["tool_call_id"] == "c0"
    assert "response_format" not in seen
    assert resp.message.tool_calls[0].arguments == {"service": "payments-svc"}
    assert resp.message.tool_calls[1].arguments == {"_unparsed": "{not json"}
    assert resp.usage.total_tokens == 150


def test_make_llm_requires_key_for_groq() -> None:
    with pytest.raises(ValueError, match="GROQ_API_KEY"):
        make_llm(Settings(llm_provider="groq", groq_api_key=None))
    llm = make_llm(Settings(llm_provider="groq", groq_api_key=SecretStr("gsk_test")))
    assert isinstance(llm, OpenAICompatClient)
    assert isinstance(make_llm(Settings(llm_provider="mock")), MockLLM)


async def test_tool_call_without_tools_becomes_repairable_text(monkeypatch: Any) -> None:
    import httpx2
    from openai import BadRequestError

    client = OpenAICompatClient(model="m", api_key="k", base_url="http://unused")
    body = {"error": {"code": "tool_use_failed", "failed_generation": '{"name": "x"}'}}

    async def reject(**kwargs: Any) -> Any:
        request = httpx2.Request("POST", "http://unused")
        raise BadRequestError("tool use", response=httpx2.Response(400, request=request), body=body)

    monkeypatch.setattr(client._client.chat.completions, "create", reject)
    resp = await client.complete([Message(role="user", content="json please")], json_mode=True)
    assert resp.message.content == '[attempted tool call] {"name": "x"}'
    tool = ToolSpec(name="x", description="d", parameters={"type": "object"})
    with pytest.raises(BadRequestError):  # with tools offered it is a real error
        await client.complete([Message(role="user", content="go")], tools=[tool])
