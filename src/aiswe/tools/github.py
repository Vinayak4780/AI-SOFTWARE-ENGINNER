"""GitHub integration: push a branch, open a PR, read/comment on an issue.
Uses the `gh` CLI inside the sandbox (installed in sandbox/image/Dockerfile),
authenticated via a GITHUB_TOKEN passed into the container's environment
(see sandbox/docker.py). Needs the sandbox started with --network (these
operations must reach github.com) and GITHUB_TOKEN set in .env.

NOT live-tested against a real GitHub repo/token -- none was available in the
environment this was built in. Implemented against gh's documented CLI
interface and error-handled the same defensive way as every other tool, but
treat this module as less proven than the rest of the project until you've
run it for real once against an actual repo.
"""

from __future__ import annotations

import shlex
from typing import Any

from ..sandbox import Sandbox


def _run(sandbox: Sandbox, command: str, timeout: int = 60) -> str:
    result = sandbox.run_shell(command, timeout=timeout)
    return f"exit code: {result.exit_code}\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"


async def git_push(sandbox: Sandbox, args: dict[str, Any]) -> str:
    branch = args.get("branch")
    if not branch:
        return "ERROR: missing required argument 'branch'"
    return _run(sandbox, f"git push -u origin {shlex.quote(branch)}")


async def create_pull_request(sandbox: Sandbox, args: dict[str, Any]) -> str:
    title = args.get("title")
    if not title:
        return "ERROR: missing required argument 'title'"
    body = args.get("body", "")
    base = args.get("base", "main")
    cmd = f"gh pr create --title {shlex.quote(title)} --body {shlex.quote(body)} --base {shlex.quote(base)}"
    return _run(sandbox, cmd)


async def get_issue(sandbox: Sandbox, args: dict[str, Any]) -> str:
    number = args.get("number")
    if not number:
        return "ERROR: missing required argument 'number'"
    return _run(sandbox, f"gh issue view {shlex.quote(str(number))}")


async def comment_issue(sandbox: Sandbox, args: dict[str, Any]) -> str:
    number = args.get("number")
    body = args.get("body")
    if not number or not body:
        return "ERROR: missing required argument 'number' or 'body'"
    return _run(sandbox, f"gh issue comment {shlex.quote(str(number))} --body {shlex.quote(body)}")


SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "git_push",
            "description": "Push the given branch to origin. Requires the sandbox to have network enabled.",
            "parameters": {
                "type": "object",
                "properties": {"branch": {"type": "string"}},
                "required": ["branch"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_pull_request",
            "description": "Open a pull request from the current branch (must be pushed first) via the gh CLI.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "body": {"type": "string", "description": "default: empty"},
                    "base": {"type": "string", "description": "target branch, default: main"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_issue",
            "description": "Read a GitHub issue's title/body/comments by number, via the gh CLI.",
            "parameters": {
                "type": "object",
                "properties": {"number": {"type": "integer"}},
                "required": ["number"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "comment_issue",
            "description": "Post a comment on a GitHub issue by number, via the gh CLI.",
            "parameters": {
                "type": "object",
                "properties": {"number": {"type": "integer"}, "body": {"type": "string"}},
                "required": ["number", "body"],
            },
        },
    },
]

HANDLERS = {
    "git_push": git_push,
    "create_pull_request": create_pull_request,
    "get_issue": get_issue,
    "comment_issue": comment_issue,
}
