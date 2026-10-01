"""Transport-independent tool layer.

Every tool call, whether it arrives over MCP (stdio or HTTP) or from the in-process agent,
goes through `ToolRunner.call`, which:

1. rejects unknown tools,
2. checks the caller's permission (RBAC lives here, not in any prompt),
3. validates arguments against a schema derived from the function signature,
4. runs the tool in its own database transaction,
5. reports a `ToolCallRecord` to observers (the audit log in phase 6).
"""

from __future__ import annotations

import hashlib
import inspect
import json
import time
import typing
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, create_model
from pydantic_core import to_jsonable_python
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from opsintel.auth import Permission, Principal
from opsintel.db.models import ScenarioRun
from opsintel.rag.embeddings import Embedder


class ToolError(Exception):
    """Base for errors that are reported back to the caller (and the model)."""


class ToolDenied(ToolError):
    pass


class ToolInputError(ToolError):
    pass


class UnknownTool(ToolError):
    pass


@dataclass
class ToolContext:
    session: Session
    principal: Principal
    now: datetime
    _embedder_factory: Callable[[], Embedder]

    @property
    def embedder(self) -> Embedder:
        return self._embedder_factory()


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    permission: Permission
    fn: Callable[..., Any]
    args_model: type[BaseModel]
    signature: inspect.Signature  # without the leading ctx parameter

    def input_schema(self) -> dict[str, Any]:
        return self.args_model.model_json_schema()


REGISTRY: dict[str, ToolDef] = {}


def tool(permission: Permission) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register `fn(ctx, **args)` as a tool. Its docstring is the description shown to the
    model and its annotated parameters (after ctx) become the argument schema."""

    def register(fn: Callable[..., Any]) -> Callable[..., Any]:
        hints = typing.get_type_hints(fn, include_extras=True)
        params = list(inspect.signature(fn).parameters.values())[1:]
        fields: dict[str, Any] = {
            p.name: (hints[p.name], ... if p.default is inspect.Parameter.empty else p.default)
            for p in params
        }
        model = create_model(f"{fn.__name__}_args", __config__=ConfigDict(extra="forbid"), **fields)
        signature = inspect.Signature(
            [p.replace(annotation=hints[p.name]) for p in params],
            return_annotation=dict[str, Any],
        )
        REGISTRY[fn.__name__] = ToolDef(
            name=fn.__name__,
            description=inspect.cleandoc(fn.__doc__ or ""),
            permission=permission,
            fn=fn,
            args_model=model,
            signature=signature,
        )
        return fn

    return register


@dataclass
class ToolCallRecord:
    tool: str
    subject: str
    role: str
    arguments: dict[str, Any]
    started_at: datetime
    ok: bool = False
    error: str | None = None
    result_digest: str | None = None  # sha256 of the canonical JSON result
    latency_ms: float = 0.0
    result: Any = field(default=None, repr=False)


def simulated_now(session: Session) -> datetime:
    """The end of the loaded scenario window, so "the last 20 minutes" means the same
    thing whenever a scenario is replayed. Falls back to wall-clock time."""
    end = session.scalar(select(func.max(ScenarioRun.window_end)))
    return end or datetime.now(UTC)


Observer = Callable[[ToolCallRecord], None]


class ToolRunner:
    def __init__(
        self,
        session_factory: Callable[[], AbstractContextManager[Session]],
        embedder_factory: Callable[[], Embedder],
        observers: list[Observer] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._embedder_factory = embedder_factory
        self._embedder: Embedder | None = None
        self.observers: list[Observer] = observers or []

    def _get_embedder(self) -> Embedder:
        if self._embedder is None:
            self._embedder = self._embedder_factory()
        return self._embedder

    @staticmethod
    def definitions(principal: Principal | None = None) -> Iterator[ToolDef]:
        """All tools, or only those `principal` may call."""
        for d in REGISTRY.values():
            if principal is None or principal.can(d.permission):
                yield d

    def call(self, name: str, arguments: dict[str, Any], principal: Principal) -> Any:
        record = ToolCallRecord(
            tool=name,
            subject=principal.subject,
            role=principal.role,
            arguments=dict(arguments),
            started_at=datetime.now(UTC),
        )
        start = time.perf_counter()
        try:
            definition = REGISTRY.get(name)
            if definition is None:
                raise UnknownTool(f"unknown tool {name!r}")
            if not principal.can(definition.permission):
                raise ToolDenied(
                    f"role {principal.role!r} lacks {definition.permission.value!r} "
                    f"required by {name}"
                )
            try:
                args = definition.args_model.model_validate(arguments)
            except ValidationError as e:
                raise ToolInputError(_short_validation_error(e)) from e
            with self._session_factory() as session:
                ctx = ToolContext(session, principal, simulated_now(session), self._get_embedder)
                result = to_jsonable_python(definition.fn(ctx, **dict(args)))
            canonical = json.dumps(result, sort_keys=True, separators=(",", ":"))
            record.ok = True
            record.result = result
            record.result_digest = hashlib.sha256(canonical.encode()).hexdigest()
            return result
        except ToolError as e:
            record.error = f"{type(e).__name__}: {e}"
            raise
        finally:
            record.latency_ms = round((time.perf_counter() - start) * 1000, 2)
            for observe in self.observers:
                observe(record)


def _short_validation_error(e: ValidationError) -> str:
    parts = []
    for err in e.errors():
        loc = ".".join(str(x) for x in err["loc"]) or "arguments"
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts)
