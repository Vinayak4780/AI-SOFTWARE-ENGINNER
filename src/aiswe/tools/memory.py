"""Tool for the agent to update this repo's durable notes file
(.aiswe/repo-notes.md) -- architecture, conventions, build/test commands,
known issues worth remembering next time. Written directly to the host path
(sandbox.repo_path is bind-mounted into the container anyway, so this is
equivalent to writing it from inside the sandbox, just without an extra
docker exec round trip)."""

from __future__ import annotations

from typing import Any

from ..memory.repo_memory import write_notes
from ..sandbox import Sandbox


async def update_repo_notes(sandbox: Sandbox, args: dict[str, Any]) -> str:
    write_notes(sandbox.repo_path, args["content"])
    return "repo notes updated"


SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "update_repo_notes",
            "description": (
                "Overwrite this repo's durable notes file (architecture, conventions, "
                "build/test commands, known issues) with the given markdown content. "
                "These notes are shown to you at the start of every future task on this "
                "repo -- write down anything worth remembering so it isn't re-discovered "
                "from scratch each time. Include prior content you still want kept; this "
                "replaces the whole file."
            ),
            "parameters": {
                "type": "object",
                "properties": {"content": {"type": "string"}},
                "required": ["content"],
            },
        },
    },
]

HANDLERS = {"update_repo_notes": update_repo_notes}
