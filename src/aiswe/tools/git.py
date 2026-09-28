"""Git tools. get_diff() is used internally by the reviewer gate (see
agent/developer.py) -- it is not exposed to the model as a callable tool,
since the model never needs to inspect a diff it can already remember writing."""

from __future__ import annotations

from typing import Any

from ..sandbox import Sandbox


async def git_commit(sandbox: Sandbox, args: dict[str, Any]) -> str:
    r = sandbox.call_helper("git_commit", message=args["message"])
    return (r.get("output") or "committed") if r.get("ok") else f"ERROR: {r.get('error')}"


def get_diff(sandbox: Sandbox) -> str:
    """Stages everything and returns the diff against HEAD, without committing."""
    r = sandbox.call_helper("git_diff")
    return r.get("diff", "") if r.get("ok") else ""


SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "git_commit",
            "description": "Stage all changes in the workspace and create a git commit with the given message.",
            "parameters": {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
        },
    },
]

HANDLERS = {"git_commit": git_commit}
