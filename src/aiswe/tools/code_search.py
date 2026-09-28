"""Code search tools: literal substring grep, and AST-based symbol lookup.

find_symbol is the "code intelligence" upgrade from PLAN.md's Roadmap v2 --
exact function/class definitions by name (Python only, via stdlib ast) rather
than a substring match, so the agent finds `def login(` and not every line
that happens to mention "login". search_code remains useful for everything
else (strings, comments, non-Python files, symbol *usages* not just definitions).
"""

from __future__ import annotations

from typing import Any

from ..sandbox import Sandbox


async def search_code(sandbox: Sandbox, args: dict[str, Any]) -> str:
    r = sandbox.call_helper("search_code", query=args["query"], path=args.get("path", "."))
    if not r.get("ok"):
        return f"ERROR: {r.get('error')}"
    return "\n".join(r["matches"]) if r["matches"] else "(no matches)"


async def find_symbol(sandbox: Sandbox, args: dict[str, Any]) -> str:
    r = sandbox.call_helper("find_symbol", name=args["name"], path=args.get("path", "."))
    if not r.get("ok"):
        return f"ERROR: {r.get('error')}"
    return "\n".join(r["matches"]) if r["matches"] else "(no matching function/class definition found)"


SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "Search for a literal substring across files in the workspace and return matching lines.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "path": {"type": "string", "description": "default: '.'"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_symbol",
            "description": (
                "Find the exact function or class definition matching a name (Python files only), "
                "returning file:line:kind. More precise than search_code for 'where is X defined' -- "
                "use this instead of search_code when looking for a specific function/class by name."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "path": {"type": "string", "description": "default: '.'"},
                },
                "required": ["name"],
            },
        },
    },
]

HANDLERS = {"search_code": search_code, "find_symbol": find_symbol}
