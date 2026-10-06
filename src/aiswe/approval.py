"""Human approval gate, shared by every model provider (Claude, OpenRouter, ...).

Any tool that changes state -- edits a file, runs a command, commits -- is
shown to the user (as a diff for edits) before it runs, unless auto-approve
is on. This is Phase 1's "propose diff -> approve -> apply" workflow from
PLAN.md, and it must behave identically no matter which model proposed the
action.
"""

from __future__ import annotations

import difflib
import os
from typing import Any, Iterable

from .security.policy import needs_approval_extra

TOOLS_REQUIRING_APPROVAL = {
    "edit_file", "write_file", "run_shell", "run_tests", "git_commit",
    "git_push", "create_pull_request", "comment_issue", "update_repo_notes",
}

# Tools that can reach the internet when the sandbox has network. With network
# on, --yes does NOT cover these (a prompt-injected model with an unattended
# shell and open internet could ship your code anywhere) unless
# AISWE_ALLOW_UNATTENDED_NETWORK=1 is set.
NETWORK_TOOLS = {"run_shell", "run_tests", "git_push", "create_pull_request", "comment_issue"}


def needs_approval(tool_name: str, tool_input: dict[str, Any], *, auto_approve: bool, network: bool) -> bool:
    if needs_approval_extra(tool_name, tool_input):
        return True  # e.g. reading a secrets file: always asks, even with --yes
    if tool_name not in TOOLS_REQUIRING_APPROVAL:
        return False
    if not auto_approve:
        return True
    return network and tool_name in NETWORK_TOOLS and os.environ.get("AISWE_ALLOW_UNATTENDED_NETWORK") != "1"


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
        # Never truncated: whatever isn't shown would be approved unseen.
        return f"write {tool_input.get('path')}:\n{tool_input.get('content', '')}"
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
        return f"update .aiswe/repo-notes.md:\n{tool_input.get('content', '')}"
    if tool_name == "read_file":
        return f"read {tool_input.get('path')}"
    return f"{tool_name}({tool_input})"


def prompt_approval(tool_name: str, tool_input: dict[str, Any], warnings: Iterable[str] = ()) -> bool:
    """Asks on the terminal; returns True if the action may proceed. No
    terminal (EOF) means no."""
    print("\n--- approval requested ---")
    print(describe_call(tool_name, tool_input))
    for warning in warnings:
        print(f"!! {warning}")
    try:
        answer = input("Allow this action? [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer == "y"
