"""File read/write/edit tools, routed through the sandbox's JSON helper
(sandbox/image/helper.py) so every operation is confined to /workspace."""

from __future__ import annotations

from typing import Any

from ..sandbox import Sandbox


async def read_file(sandbox: Sandbox, args: dict[str, Any]) -> str:
    r = sandbox.call_helper("read_file", path=args["path"])
    return r["content"] if r.get("ok") else f"ERROR: {r.get('error')}"


async def list_dir(sandbox: Sandbox, args: dict[str, Any]) -> str:
    r = sandbox.call_helper("list_dir", path=args.get("path", "."))
    if not r.get("ok"):
        return f"ERROR: {r.get('error')}"
    return "\n".join(r["entries"]) or "(empty)"


async def edit_file(sandbox: Sandbox, args: dict[str, Any]) -> str:
    r = sandbox.call_helper(
        "edit_file", path=args["path"], old_string=args["old_string"], new_string=args["new_string"]
    )
    return f"edited {args['path']}" if r.get("ok") else f"ERROR: {r.get('error')}"


async def write_file(sandbox: Sandbox, args: dict[str, Any]) -> str:
    r = sandbox.call_helper("write_file", path=args["path"], content=args["content"])
    return f"wrote {args['path']}" if r.get("ok") else f"ERROR: {r.get('error')}"


SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file's contents from the sandboxed workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files and directories under a path in the sandboxed workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "default: '.'"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Replace one exact, unique occurrence of old_string with new_string in a file. "
                "old_string must match the file's current content exactly, including whitespace, "
                "and must be unique in the file -- include enough surrounding context to guarantee that."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create a new file, or overwrite an existing one, with the given content.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    },
]

HANDLERS = {"read_file": read_file, "list_dir": list_dir, "edit_file": edit_file, "write_file": write_file}
