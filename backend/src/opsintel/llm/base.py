"""Provider-agnostic chat interface.

The agent only talks to `LLMClient`. Providers translate to and from their wire
format, so swapping Groq for OpenAI (or a scripted mock in tests) is a config change.
"""

from __future__ import annotations

import json
from typing import Any, Literal, Protocol, TypeVar

from pydantic import BaseModel, Field, ValidationError

Role = Literal["system", "user", "assistant", "tool"]


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class Message(BaseModel):
    role: Role
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None  # set on role="tool" messages


class ToolSpec(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema of the arguments object


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class LLMResponse(BaseModel):
    message: Message
    model: str
    usage: Usage = Field(default_factory=Usage)
    latency_ms: float = 0.0


class LLMClient(Protocol):
    model: str

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        json_mode: bool = False,
        temperature: float = 0.0,
    ) -> LLMResponse: ...


class StructuredOutputError(Exception):
    pass


T = TypeVar("T", bound=BaseModel)


async def complete_structured(
    client: LLMClient,
    messages: list[Message],
    schema: type[T],
    max_repairs: int = 1,
) -> tuple[T, list[LLMResponse]]:
    """Ask for JSON matching `schema`, validate it with Pydantic, and retry with the
    validation errors if it doesn't parse. JSON mode is supported by every provider we
    target, so this works even where native json_schema output is not available."""
    instruction = Message(
        role="system",
        content=(
            "Respond with a single JSON object that conforms to this JSON Schema. "
            "No prose, no markdown fences.\n" + json.dumps(schema.model_json_schema())
        ),
    )
    convo = [*messages, instruction]
    responses: list[LLMResponse] = []
    for _ in range(max_repairs + 1):
        resp = await client.complete(convo, json_mode=True)
        responses.append(resp)
        raw = resp.message.content or ""
        try:
            return schema.model_validate_json(raw), responses
        except ValidationError as e:
            convo = [
                *convo,
                resp.message,
                Message(
                    role="user",
                    content=(
                        f"That was not a valid answer:\n{e}\nTools are not available now. "
                        "Return the JSON object only."
                    ),
                ),
            ]
    raise StructuredOutputError(f"{schema.__name__} not produced after {max_repairs + 1} attempts")
