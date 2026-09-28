"""Shell/test-execution tools, run inside the sandboxed container."""

from __future__ import annotations

from typing import Any

from ..sandbox import Sandbox


async def run_shell(sandbox: Sandbox, args: dict[str, Any]) -> str:
    command = args.get("command")
    if not command:
        return "ERROR: missing required argument 'command'"
    result = sandbox.run_shell(command, timeout=120)
    return f"exit code: {result.exit_code}\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"


async def run_tests(sandbox: Sandbox, args: dict[str, Any]) -> str:
    command = args.get("command") or "pytest -q"
    result = sandbox.run_shell(command, timeout=300)
    return f"exit code: {result.exit_code}\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"


SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": (
                "Run a shell command inside the sandboxed container, rooted at the repo. "
                "No network access unless the sandbox was started with it enabled."
            ),
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": (
                "Run the project's test command inside the sandbox (e.g. 'pytest -q'). "
                "Always run this after making changes and before finishing the task."
            ),
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
]

HANDLERS = {"run_shell": run_shell, "run_tests": run_tests}
