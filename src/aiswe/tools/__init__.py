"""Aggregates every tools/* module's schema list and handlers into the flat
TOOL_SCHEMAS + execute_tool() interface agent/developer.py drives the model
loop with. Add a new tool by adding it to one of these modules (or a new
module here) -- nothing else needs to change."""

from __future__ import annotations

from typing import Any

from ..sandbox import Sandbox
from . import code_search, filesystem, git, github, memory, security, terminal

_MODULES = [filesystem, terminal, git, code_search, security, memory, github]

TOOL_SCHEMAS: list[dict[str, Any]] = [schema for m in _MODULES for schema in m.SCHEMAS]

_HANDLERS: dict[str, Any] = {}
for _m in _MODULES:
    _HANDLERS.update(_m.HANDLERS)


async def execute_tool(sandbox: Sandbox, name: str, args: dict[str, Any]) -> str:
    """Never raises: a malformed tool call (missing/wrong-typed argument --
    smaller free models don't always follow the required-argument schema
    strictly, unlike Claude) becomes an ERROR string fed back to the model as
    a normal tool result, so it can see the mistake and retry, instead of
    crashing the whole run over one bad call."""
    handler = _HANDLERS.get(name)
    if handler is None:
        return f"ERROR: unknown tool {name}"
    try:
        return await handler(sandbox, args)
    except KeyError as e:
        return f"ERROR: missing required argument {e}"
    except Exception as e:  # noqa: BLE001 -- any other malformed-call failure, reported the same way
        return f"ERROR: {type(e).__name__}: {e}"
