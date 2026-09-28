"""Human approval gate, shared by every model provider (Claude, OpenRouter, ...).

Any tool that changes state -- edits a file, runs a command, commits -- is
shown to the user (as a diff for edits) before it runs, unless auto-approve
is on. This is Phase 1's "propose diff -> approve -> apply" workflow from
PLAN.md, and it must behave identically no matter which model proposed the
action.
"""

from __future__ import annotations

import difflib
from typing import Any

TOOLS_REQUIRING_APPROVAL = {
    "edit_file", "write_file", "run_shell", "run_tests", "git_commit",
    "git_push", "create_pull_request", "comment_issue", "update_repo_notes",
}


def describe_call(tool_name: str, tool_input: dict[str, Any]) -> str:
    if tool_name == "edit_file":
        diff = difflib.unified_diff(
            tool_input.get("old_string", "").splitlines(keepends=True),
            tool_input.get("new_string", "").splitlines(keepends=True),
            fromfile=tool_input.get("path", ""),
            tofile=tool_input.get("path", ""),
        )
        return f"edit {tool_input.get('path')}:\n" + "".join(diff)
    if tool_name == "write_file":
        content = tool_input.get("content", "")
        preview = content if len(content) <= 2000 else content[:2000] + "\n...[truncated]"
        return f"write {tool_input.get('path')}:\n{preview}"
    if tool_name in ("run_shell", "run_tests"):
        return f"run: {tool_input.get('command')}"
    if tool_name == "git_commit":
        return f"git commit -m {tool_input.get('message')!r}"
    if tool_name == "git_push":
        return f"git push -u origin {tool_input.get('branch')}"
    if tool_name == "create_pull_request":
        return f"open PR: {tool_input.get('title')!r} -> {tool_input.get('base', 'main')}\n{tool_input.get('body', '')}"
    if tool_name == "comment_issue":
        return f"comment on issue #{tool_input.get('number')}:\n{tool_input.get('body')}"
    if tool_name == "update_repo_notes":
        content = tool_input.get("content", "")
        preview = content if len(content) <= 2000 else content[:2000] + "\n...[truncated]"
        return f"update .aiswe/repo-notes.md:\n{preview}"
    return f"{tool_name}({tool_input})"


def prompt_approval(tool_name: str, tool_input: dict[str, Any], *, auto_approve: bool) -> bool:
    """Returns True if the action may proceed."""
    if tool_name not in TOOLS_REQUIRING_APPROVAL:
        return True
    if auto_approve:
        return True
    print("\n--- approval requested ---")
    print(describe_call(tool_name, tool_input))
    answer = input("Allow this action? [y/N] ").strip().lower()
    return answer == "y"
