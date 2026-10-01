"""Importing this package registers every tool."""

from opsintel.tools import catalog as _catalog  # noqa: F401
from opsintel.tools.registry import (
    REGISTRY,
    ToolCallRecord,
    ToolContext,
    ToolDef,
    ToolDenied,
    ToolError,
    ToolInputError,
    ToolRunner,
    UnknownTool,
)

__all__ = [
    "REGISTRY",
    "ToolCallRecord",
    "ToolContext",
    "ToolDef",
    "ToolDenied",
    "ToolError",
    "ToolInputError",
    "ToolRunner",
    "UnknownTool",
]
