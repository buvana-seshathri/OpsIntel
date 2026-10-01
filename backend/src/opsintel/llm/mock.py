"""Scripted LLM for tests and zero-cost development."""

from __future__ import annotations

from collections.abc import Callable

from opsintel.llm.base import LLMResponse, Message, ToolSpec, Usage

Script = Callable[[list[Message], list[ToolSpec] | None], Message]


class MockLLM:
    """Replays a fixed list of assistant messages, or calls a function that decides
    the next message from the conversation so far."""

    def __init__(self, script: list[Message] | Script | None = None, model: str = "mock") -> None:
        self.model = model
        self._script = script if script is not None else []
        self.calls: list[list[Message]] = []

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        json_mode: bool = False,
        temperature: float = 0.0,
    ) -> LLMResponse:
        self.calls.append(list(messages))
        if callable(self._script):
            msg = self._script(messages, tools)
        else:
            if not self._script:
                raise RuntimeError("MockLLM script exhausted")
            msg = self._script.pop(0)
        prompt_chars = sum(len(m.content or "") for m in messages)
        return LLMResponse(
            message=msg,
            model=self.model,
            usage=Usage(
                prompt_tokens=prompt_chars // 4, completion_tokens=len(msg.content or "") // 4
            ),
        )
