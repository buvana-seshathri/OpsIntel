"""Client for any OpenAI-compatible chat completions API (Groq, OpenAI, vLLM, ...)."""

from __future__ import annotations

import json
import time
from typing import Any

from openai import AsyncOpenAI, BadRequestError

from opsintel.llm.base import LLMResponse, MalformedToolCall, Message, ToolCall, ToolSpec, Usage

GROQ_BASE_URL = "https://api.groq.com/openai/v1"


class OpenAICompatClient:
    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.model = model
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=6,  # backs off on 429/5xx; free-tier Groq rate-limits per minute
        )

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        json_mode: bool = False,
        temperature: float = 0.0,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [_to_wire(m) for m in messages],
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        start = time.perf_counter()
        try:
            resp = await self._client.chat.completions.create(**kwargs)
        except BadRequestError as e:
            failed = _failed_tool_generation(e)
            if failed is None:
                raise
            if tools:
                raise MalformedToolCall(failed) from e
            # Groq rejects a reply that calls a tool when none were offered (seen with
            # gpt-oss when asked for the final JSON). Hand it back as an ordinary invalid
            # answer so the caller's repair loop can ask again.
            return LLMResponse(
                message=Message(role="assistant", content=f"[attempted tool call] {failed}"),
                model=self.model,
                latency_ms=(time.perf_counter() - start) * 1000,
            )
        latency_ms = (time.perf_counter() - start) * 1000

        choice = resp.choices[0].message
        tool_calls = [
            ToolCall(id=tc.id, name=tc.function.name, arguments=_parse_args(tc.function.arguments))
            for tc in (choice.tool_calls or [])
            if tc.type == "function"
        ]
        usage = Usage(
            prompt_tokens=resp.usage.prompt_tokens if resp.usage else 0,
            completion_tokens=resp.usage.completion_tokens if resp.usage else 0,
        )
        return LLMResponse(
            message=Message(role="assistant", content=choice.content, tool_calls=tool_calls),
            model=resp.model,
            usage=usage,
            latency_ms=latency_ms,
        )


def _failed_tool_generation(e: BadRequestError) -> str | None:
    body = e.body if isinstance(e.body, dict) else {}
    err = body.get("error", body)
    if isinstance(err, dict) and err.get("code") == "tool_use_failed":
        return str(err.get("failed_generation", ""))
    return None


def _parse_args(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # Surface the malformed call to the agent loop rather than crashing it.
        return {"_unparsed": raw}
    return parsed if isinstance(parsed, dict) else {"_unparsed": raw}


def _to_wire(m: Message) -> dict[str, Any]:
    out: dict[str, Any] = {"role": m.role, "content": m.content}
    if m.tool_calls:
        out["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
            }
            for tc in m.tool_calls
        ]
    if m.tool_call_id:
        out["tool_call_id"] = m.tool_call_id
    return out
