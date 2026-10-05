"""The developer agent: drives whatever model provider you've configured (via
LiteLLM -- OpenRouter, Groq, Anthropic, or anything else in providers.py,
including custom OpenAI-compatible endpoints) through the ACI tool set (tools/), with the
same approval gate as everything else in this project, and a reviewer agent
(reviewer.py) gating every commit before it reaches the human.

Which model runs is decided entirely by model_router.py, from whichever
provider keys are actually present in .env -- this file doesn't hardcode a
provider. It defaults to preferring free models (see PLAN.md for why: a paid
API means real per-token cost) but is not restricted to them -- if you add a
paid provider's key, it's used as a last-resort fallback automatically, or
immediately if you name its model explicitly, with a clear warning either way.

Automatic fallback to the next model in the chain if one is unavailable:
free model backends in particular are noticeably less reliable than a paid
API -- see the retry logic below, which exists because of a real, observed
failure mode (OpenRouter's free Nvidia-hosted model returning intermittent
503s).

AgentSession is the loop itself: a multi-turn conversation over one sandbox,
reporting progress as event dicts through an `emit` callback and asking an
async `approver` callback before state-changing tools run. run_task() (the
`aiswe run` CLI path) is a one-message session printed to the terminal;
server.py (`aiswe serve`, used by the VS Code extension) drives the same
session over a JSON protocol.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import litellm

from ..approval import TOOLS_REQUIRING_APPROVAL, prompt_approval
from ..memory.repo_memory import read_notes
from ..model_router import build_model_chain, build_planner_chain, is_free_model
from ..providers import completion_kwargs, model_available
from ..sandbox import Sandbox, build_image
from ..tools import TOOL_SCHEMAS, execute_tool
from ..tools.git import get_diff
from .reviewer import pick_reviewer_model, review_diff

# Reduce litellm's default per-exception banner; we print our own concise
# [warning] lines instead.
litellm.suppress_debug_info = True

MAX_TURNS = 40
MAX_RETRIES_PER_MODEL = 3
RETRY_BACKOFF_SECONDS = 3
MAX_REVIEW_ROUNDS = 2

Log = Callable[[str], None]
Emit = Callable[[dict[str, Any]], None]
Approver = Callable[[str, dict[str, Any]], Awaitable[bool]]


class SessionError(RuntimeError):
    """A problem that stops the session from doing anything useful (e.g. no
    provider key configured) -- as opposed to one failed task."""


def _provider_available(model: str) -> bool:
    """An empty-but-present key (e.g. "GROQ_API_KEY=" in .env) would otherwise
    get retried and fail every single turn instead of being skipped once, up front."""
    return model_available(model)


SYSTEM_PROMPT = (
    "You are an autonomous software engineer working inside a sandboxed container "
    "mounted on a single repository at /workspace. You can only read or change files, "
    "run commands, or commit through the tools you've been given -- there is no other "
    "way to affect the repository. Investigate before editing: read_file, search_code "
    "(substring), find_symbol (exact function/class definitions, prefer this over "
    "search_code when you know the name), list_dir. Make small, reviewable edits. "
    "Always run the test suite with run_tests after making changes and before declaring "
    "the task done. A reviewer model checks every commit attempt before it reaches the "
    "human -- if it requests changes, address the feedback and call git_commit again. "
    "If you learn something about this repo worth remembering for next time (build/test "
    "commands, conventions, architecture), call update_repo_notes. If working from a "
    "GitHub issue, get_issue/comment_issue are available; git_push/create_pull_request "
    "need network enabled and a GITHUB_TOKEN configured. When finished, summarize what "
    "changed and stop calling tools."
)

# Appended for interactive (chat) sessions, where the user may just ask a
# question rather than hand over a task.
CHAT_PROMPT = (
    "You are being used from a chat panel in the user's editor, and the conversation "
    "continues across messages. Not every message is a coding task: if the user asks a "
    "question, answer it (reading files as needed) without editing anything. Only commit "
    "when the user asked for a change and it's done. Messages may start with the file "
    "and selection the user has open in their editor -- paths there are relative to "
    "/workspace."
)

NEW_PROJECT_PROMPT = (
    "This is a NEW project: /workspace starts empty (a fresh git repo, no commits yet). "
    "Create it from scratch: pick a sensible layout for the language/framework asked for, "
    "write the source files with write_file (parent folders are created automatically), "
    "and add a README.md with how to run it, a .gitignore, and at least a few tests. "
    "Run the code and the tests with run_shell/run_tests to prove they work -- the sandbox "
    "has Python 3.11 (pytest installed) and gcc/g++/make; other toolchains (Node, Java, Go, "
    "Rust, ...) are NOT installed and there's no network unless enabled, so if the user asked "
    "for one of those, still write the project but say clearly that you couldn't run it. "
    "Record the build/test commands with update_repo_notes, then git_commit the result."
)

FALLBACK_GIT_NAME = "aiswe"
FALLBACK_GIT_EMAIL = "aiswe@localhost"


def _in_git_repo(repo_path: str) -> bool:
    """True if repo_path or any parent has a .git -- checked on the host so a
    subfolder of an existing repo never gets a nested `git init`."""
    p = Path(repo_path).resolve()
    return any((d / ".git").exists() for d in (p, *p.parents))


def _git_identity(repo_path: str) -> tuple[str, str]:
    """The host's git user.name/user.email for this repo (local or global
    config), so sandbox commits are authored by the user, not anonymously --
    the container doesn't see the host's ~/.gitconfig."""
    def get(key: str, fallback: str) -> str:
        try:
            r = subprocess.run(["git", "config", key], cwd=repo_path, capture_output=True, text=True)
        except OSError:  # git not installed on the host
            return fallback
        return r.stdout.strip() or fallback

    return get("user.name", FALLBACK_GIT_NAME), get("user.email", FALLBACK_GIT_EMAIL)


def _review_gate(
    sandbox: Sandbox,
    task: str,
    model_chain: list[str],
    used_model: str,
    review_state: dict[str, Any],
    log: Log,
) -> str | None:
    """Called on every git_commit attempt. Returns feedback text to feed back
    to the model instead of committing (reviewer requested changes), or None
    to proceed to the normal human approval gate + actual commit (reviewer
    approved, or the review-round budget for this task is used up)."""
    if review_state["rounds"] >= MAX_REVIEW_ROUNDS:
        if not review_state["notice_shown"]:
            log(f"[reviewer] max review rounds ({MAX_REVIEW_ROUNDS}) reached for this task -- skipping further automated review.")
            review_state["notice_shown"] = True
        return None

    diff_text = get_diff(sandbox)
    reviewer_model = pick_reviewer_model(model_chain, used_model)
    approved, feedback = review_diff(task=task, diff=diff_text, author_model=used_model, reviewer_model=reviewer_model)
    log(f"[reviewer:{reviewer_model}] verdict: {'APPROVE' if approved else 'REQUEST_CHANGES'}")
    if approved:
        return None

    review_state["rounds"] += 1
    return (
        f"REVIEWER REQUESTED CHANGES (round {review_state['rounds']}/{MAX_REVIEW_ROUNDS}):\n{feedback}\n\n"
        "Address this feedback, then call git_commit again."
    )


def _complete_with_fallback(model_chain: list[str], messages: list[dict[str, Any]], log: Log) -> tuple[Any, str]:
    """Try each model in the chain in order; within a model, retry a few times
    on transient failures before moving to the next model. Returns (response,
    model_actually_used)."""
    last_error = "unknown error"
    for model in model_chain:
        if not is_free_model(model):
            log(f"[billing] falling back to a paid model ({model}) -- this call incurs real token cost.")
        for attempt in range(1, MAX_RETRIES_PER_MODEL + 1):
            try:
                response = litellm.completion(**completion_kwargs(model), messages=messages, tools=TOOL_SCHEMAS)
            except Exception as e:  # noqa: BLE001 -- provider/network errors, handled uniformly
                last_error = f"{type(e).__name__}: {e}"
            else:
                if response.choices:
                    return response, model
                last_error = f"empty response (no choices): {response}"

            if attempt < MAX_RETRIES_PER_MODEL:
                wait = RETRY_BACKOFF_SECONDS * attempt
                log(f"[warning] {model} failed ({last_error}); retrying in {wait}s ({attempt}/{MAX_RETRIES_PER_MODEL})")
                time.sleep(wait)
        log(f"[warning] giving up on {model} after {MAX_RETRIES_PER_MODEL} attempts, trying next model in chain")

    raise RuntimeError(f"all models in chain {model_chain} failed; last error: {last_error}")


def _make_plan(sandbox: Sandbox, task: str, planner_chain: list[str], log: Log) -> str | None:
    """One cheap call (deliberately the fast-tier chain, not whatever chain
    was picked for the main task) that sketches an approach before the
    (possibly stronger/slower) implementation model starts. Best-effort: a
    planning failure never blocks the actual task, it just proceeds without one."""
    if not planner_chain:
        return None
    listing = sandbox.call_helper("list_dir", path=".")
    file_list = "\n".join(listing.get("entries", [])) if listing.get("ok") else "(unknown)"
    prompt = (
        f"Task: {task}\n\nTop-level files in the repo:\n{file_list}\n\n"
        "Write a short numbered plan (3-6 steps) for how to accomplish this task. "
        "Just the plan, no commentary before or after it."
    )
    try:
        response, planner_model = _complete_with_fallback(planner_chain, [{"role": "user", "content": prompt}], log)
    except RuntimeError:
        return None
    if not response.choices:
        return None
    plan = response.choices[0].message.content or ""
    if plan.strip():
        log(f"[planner:{planner_model}] plan:\n{plan}")
    return plan or None


async def _run_tool(sandbox: Sandbox, name: str, args: dict[str, Any]) -> str:
    """Tool handlers are async in signature but block on docker subprocesses
    (run_tests can take minutes), so run them on a worker thread with their
    own event loop -- keeps the caller's loop free to read approvals/cancels."""
    return await asyncio.to_thread(asyncio.run, execute_tool(sandbox, name, args))


class AgentSession:
    """A conversation with the developer agent over one sandboxed repo.

    Events passed to `emit` (all dicts with a "type" key):
      log         {"text"}                          progress/diagnostic line
      assistant   {"text", "model"}                 the model's reply text
      tool_call   {"id", "name", "args", "model"}   the model wants to run a tool
      tool_result {"id", "name", "result"}          what the tool returned (or DENIED)
      done        {"usage": {...} | None}           the model stopped calling tools
    `approver(tool_name, args)` is awaited for every tool in
    approval.TOOLS_REQUIRING_APPROVAL unless auto_approve is set.
    """

    def __init__(
        self,
        repo_path: str,
        *,
        emit: Emit,
        approver: Approver,
        network: bool = False,
        auto_approve: bool = False,
        model: str | None = None,
        new_project: bool = False,
        chat: bool = False,
    ) -> None:
        self.repo_path = repo_path
        self.emit = emit
        self.approver = approver
        self.network = network
        self.auto_approve = auto_approve
        self.model = model
        self.new_project = new_project
        self.chat = chat
        self.sandbox: Sandbox | None = None
        self.messages: list[dict[str, Any]] = []
        self._cancelled = False

    def log(self, text: str) -> None:
        self.emit({"type": "log", "text": text})

    def _model_chain(self, task: str, model: str | None) -> list[str]:
        try:
            model_chain = build_model_chain(task, override=model)
        except SystemExit as e:  # the router's "nothing configured" exit -- must not kill `aiswe serve`
            raise SessionError(str(e)) from None
        skipped = [m for m in model_chain if not _provider_available(m)]
        model_chain = [m for m in model_chain if _provider_available(m)]
        if skipped:
            self.log(f"[router] skipping (no API key configured): {skipped}")
        if not model_chain:
            raise SessionError(
                "No model in the chain has a configured API key. Set OPENROUTER_API_KEY "
                "and/or GROQ_API_KEY in .env (see .env.example)."
            )
        annotated = [f"{m}{'' if is_free_model(m) else ' [PAID]'}" for m in model_chain]
        self.log(f"[router] model chain for this task: {annotated}")
        if not is_free_model(model_chain[0]):
            self.log(f"[billing] warning: first model to try ({model_chain[0]}) is a paid model -- this run may incur real token cost.")
        return model_chain

    def _start_sandbox(self) -> None:
        build_image()
        sandbox = Sandbox(self.repo_path, network=self.network)
        sandbox.start()
        self.sandbox = sandbox
        self.log(f"[sandbox] started container {sandbox.container_name} (network={'on' if self.network else 'off'})")
        needs_init = not _in_git_repo(self.repo_path)
        sandbox.prepare_git(*_git_identity(self.repo_path), init=needs_init)
        if needs_init:
            self.log(f"[git] {self.repo_path} wasn't a git repository -- initialized a new one (branch: main)")

    def _initial_messages(self) -> list[dict[str, Any]]:
        system = SYSTEM_PROMPT + ("\n\n" + CHAT_PROMPT if self.chat else "")
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        if self.new_project:
            messages.append({"role": "user", "content": NEW_PROJECT_PROMPT})
        notes = read_notes(self.repo_path)
        if notes.strip():
            self.log("[memory] loaded existing repo notes (.aiswe/repo-notes.md)")
            messages.append({"role": "user", "content": f"Existing repo notes from a previous run:\n\n{notes}"})
        return messages

    def cancel(self) -> None:
        """Stop the current send() at its next step (between model calls/tools).
        Pending approvals are the caller's to deny."""
        self._cancelled = True

    def reset(self) -> None:
        """Forget the conversation; the sandbox is kept for the next message."""
        self.messages = []

    def close(self) -> None:
        if self.sandbox is not None:
            self.sandbox.stop()
            self.log(f"[sandbox] stopped container {self.sandbox.container_name}")
            self.sandbox = None

    async def send(self, task: str, model: str | None = None) -> None:
        """Run one user message through the agent until the model stops calling
        tools (or gives up / is cancelled). `model` overrides the session's
        model for this message ("auto" = automatic routing). Raises
        SessionError if nothing can run."""
        self._cancelled = False
        # Auto routing may list models live from providers -- network, so off the loop.
        model_chain = await asyncio.to_thread(self._model_chain, task, model or self.model)
        if self.sandbox is None:
            await asyncio.to_thread(self._start_sandbox)
        sandbox = self.sandbox
        assert sandbox is not None

        first_message = not self.messages
        if first_message:
            self.messages = self._initial_messages()
        messages = self.messages
        messages.append({"role": "user", "content": task})

        if first_message:
            planner_chain = [m for m in build_planner_chain() if _provider_available(m)]
            plan = await asyncio.to_thread(_make_plan, sandbox, task, planner_chain, self.log)
            if plan:
                messages.append({"role": "user", "content": f"Suggested plan (from a cheaper planning pass -- adjust as needed, this isn't binding):\n{plan}"})
        review_state: dict[str, Any] = {"rounds": 0, "notice_shown": False}

        response = None
        for _turn in range(MAX_TURNS):
            if self._cancelled:
                self.log("[cancelled] stopped at your request")
                return
            try:
                response, used_model = await asyncio.to_thread(_complete_with_fallback, model_chain, messages, self.log)
            except RuntimeError as e:
                self.log(f"\n[error] giving up on this task: {e}")
                self.log("[error] any edits/commits already made before this point are still in your repo.")
                return

            choice = response.choices[0]
            msg = choice.message
            messages.append(msg.model_dump(exclude_none=True) if hasattr(msg, "model_dump") else dict(msg))

            if msg.content:
                self.emit({"type": "assistant", "text": msg.content, "model": used_model})

            if not msg.tool_calls:
                break

            for tool_call in msg.tool_calls:
                name = tool_call.function.name
                # Every tool_call needs a matching tool message, or the next
                # request to the provider is rejected -- so a cancel still
                # answers the remaining calls.
                if self._cancelled:
                    messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": "CANCELLED: the user stopped this task"})
                    continue
                try:
                    args = json.loads(tool_call.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                self.emit({"type": "tool_call", "id": tool_call.id, "name": name, "args": args, "model": used_model})

                if name == "git_commit":
                    review_feedback = await asyncio.to_thread(
                        _review_gate, sandbox, task, model_chain, used_model, review_state, self.log
                    )
                    if review_feedback is not None:
                        messages.append(
                            {"role": "tool", "tool_call_id": tool_call.id, "content": review_feedback}
                        )
                        self.emit({"type": "tool_result", "id": tool_call.id, "name": name, "result": review_feedback})
                        continue

                needs_approval = name in TOOLS_REQUIRING_APPROVAL and not self.auto_approve
                if not needs_approval or await self.approver(name, args):
                    result_text = await _run_tool(sandbox, name, args)
                else:
                    result_text = "DENIED: user denied this action"

                messages.append(
                    {"role": "tool", "tool_call_id": tool_call.id, "content": result_text}
                )
                self.emit({"type": "tool_result", "id": tool_call.id, "name": name, "result": result_text})
        else:
            self.log(f"\n[warning] hit the {MAX_TURNS}-turn limit without the model finishing")

        usage = None
        if response is not None and getattr(response, "usage", None):
            u = response.usage
            usage = {"prompt_tokens": u.prompt_tokens, "completion_tokens": u.completion_tokens}
        self.emit({"type": "done", "usage": usage})


def _print_event(event: dict[str, Any]) -> None:
    """Terminal rendering of AgentSession events, for `aiswe run`/`aiswe new`."""
    kind = event["type"]
    if kind in ("log", "assistant"):
        print(event["text"])
    elif kind == "tool_call":
        print(f"[tool:{event['model']}] {event['name']}({event['args']})")
    elif kind == "done" and event["usage"]:
        u = event["usage"]
        print(f"\n--- done (last turn: {u['prompt_tokens']} in / {u['completion_tokens']} out tokens, $0 -- free model) ---")


async def _terminal_approver(name: str, args: dict[str, Any]) -> bool:
    return prompt_approval(name, args, auto_approve=False)


async def run_task(
    repo_path: str,
    task: str,
    *,
    network: bool = False,
    auto_approve: bool = False,
    model: str | None = None,
    new_project: bool = False,
) -> None:
    session = AgentSession(
        repo_path,
        emit=_print_event,
        approver=_terminal_approver,
        network=network,
        auto_approve=auto_approve,
        model=model,
        new_project=new_project,
    )
    try:
        await session.send(task)
    except SessionError as e:
        raise SystemExit(str(e)) from None
    finally:
        session.close()
