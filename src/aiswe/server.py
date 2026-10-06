"""`aiswe serve`: drives one AgentSession over a JSON-lines protocol on
stdin/stdout, for editor front-ends (vscode-extension/ in this repo).

One JSON object per line in each direction. stdout carries protocol messages
only -- anything else that prints (litellm, stray print()s, child processes)
is redirected to stderr, which a front-end should show as a log.

Client -> server:
  {"type": "message", "text": "...", "context": {...}?, "model": "..."?, "mode": "security"?}   start a turn
      context (all optional): {"file": "rel/path.py", "language": "python",
                               "selection": "...", "startLine": 10, "endLine": 20}
      model: an id from the "models" event, or "auto" (default) for automatic routing
      mode: "security" runs the message as a security audit
  {"type": "list_models", "refresh": false}               ask for a "models" event
  {"type": "approval", "id": N, "approved": true|false}   answer an approval_request
  {"type": "cancel"}                                      stop the running turn
  {"type": "reset"}                                       forget the conversation
  {"type": "shutdown"}                                    stop the sandbox and exit

Server -> client:
  {"type": "ready", "repo": "...", "version": "..."}
  every AgentSession event (log, assistant, tool_call, tool_result, done)
  {"type": "approval_request", "id": N, "tool": "...", "args": {...}, "description": "...", "warnings": [...]}
  {"type": "turn_end"}                    a message's turn finished (always sent, even on error)
  {"type": "reset_done"}
  {"type": "models", "providers": [{"id", "label", "keyEnv", "keyUrl", "configured",
                                    "custom", "error", "warning", "models": [{"id", "name", "free"}]}]}
  {"type": "error", "text": "..."}
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import sys
import threading
from typing import Any, TextIO

from . import __version__
from .agent import AgentSession
from .approval import describe_call
from .providers import all_providers, list_all_models, transport_warning


class _Protocol:
    def __init__(self, out: TextIO) -> None:
        self._out = out
        self._lock = threading.Lock()  # session events can come from worker threads

    def send(self, message: dict[str, Any]) -> None:
        line = json.dumps(message, ensure_ascii=False, default=str)
        with self._lock:
            self._out.write(line + "\n")
            self._out.flush()


def _take_protocol_streams() -> tuple[TextIO, TextIO]:
    """Move the protocol onto private, non-inheritable copies of stdin/stdout,
    and point fds 0/1 at nul/stderr. Child processes (git, docker) inherit
    fds 0/1, and on Windows Git can otherwise consume or close the protocol
    pipe out from under us; stray print()s also land on stderr this way."""
    sys.stdout.flush()
    proto_in = os.fdopen(os.dup(0), "r", encoding="utf-8", errors="replace")
    proto_out = os.fdopen(os.dup(1), "w", encoding="utf-8")
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)
    os.dup2(2, 1)
    sys.stdin = open(os.devnull, encoding="utf-8")
    sys.stdout = sys.stderr
    return proto_in, proto_out


def _read_lines(stream: TextIO, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue) -> None:
    """Blocking reader on its own thread -- asyncio can't read a stdin pipe
    portably (not at all on Windows' proactor loop)."""
    for line in stream:
        loop.call_soon_threadsafe(queue.put_nowait, line)
    loop.call_soon_threadsafe(queue.put_nowait, None)


def _models_event(refresh: bool) -> dict[str, Any]:
    """Every supported provider (so a UI can offer key fields for the ones not
    set up yet), with the live model list for each configured one."""
    listed = {pm.provider.id: pm for pm in list_all_models(refresh=refresh)}
    providers = []
    for p in all_providers():
        pm = listed.get(p.id)
        providers.append({
            "id": p.id, "label": p.label, "keyEnv": p.key_envs[0], "keyUrl": p.key_url,
            "configured": p.configured(), "custom": p.id.startswith("custom-"),
            "error": pm.error if pm else None,
            "warning": transport_warning(p),
            "models": [{"id": m.id, "name": m.name, "free": m.free} for m in pm.models] if pm else [],
        })
    return {"type": "models", "providers": providers}


def _with_context(text: str, context: dict[str, Any] | None) -> str:
    """Prefix the user's message with what they have open in the editor."""
    if not context or not context.get("file"):
        return text
    header = f"[Open in editor: {context['file']}"
    if context.get("startLine"):
        header += f", lines {context['startLine']}-{context.get('endLine', context['startLine'])}"
    header += "]"
    selection = context.get("selection")
    if selection:
        header += f"\nSelected code:\n```{context.get('language', '')}\n{selection}\n```"
    return f"{header}\n\n{text}"


async def serve(repo_path: str, *, network: bool = False, auto_approve: bool = False, model: str | None = None) -> None:
    proto_in, proto_out = _take_protocol_streams()
    protocol = _Protocol(proto_out)

    loop = asyncio.get_running_loop()
    inbox: asyncio.Queue[str | None] = asyncio.Queue()
    threading.Thread(target=_read_lines, args=(proto_in, loop, inbox), daemon=True).start()

    pending: dict[int, asyncio.Future[bool]] = {}
    ids = itertools.count(1)

    async def approver(name: str, args: dict[str, Any], warnings: list[str]) -> bool:
        request_id = next(ids)
        future: asyncio.Future[bool] = loop.create_future()
        pending[request_id] = future
        protocol.send({
            "type": "approval_request", "id": request_id, "tool": name,
            "args": args, "description": describe_call(name, args), "warnings": warnings,
        })
        try:
            return await future
        finally:
            pending.pop(request_id, None)

    def deny_pending() -> None:
        for future in pending.values():
            if not future.done():
                future.set_result(False)

    session = AgentSession(
        repo_path,
        emit=protocol.send,
        approver=approver,
        network=network,
        auto_approve=auto_approve,
        model=model,
        chat=True,
    )

    async def run_turn(text: str, model: str | None, mode: str) -> None:
        try:
            await session.send(text, model=model, mode=mode)
        except Exception as e:  # noqa: BLE001 -- report and keep serving
            protocol.send({"type": "error", "text": f"{type(e).__name__}: {e}"})
        finally:
            protocol.send({"type": "turn_end"})

    turn: asyncio.Task | None = None
    protocol.send({"type": "ready", "repo": repo_path, "version": __version__})
    try:
        while True:
            line = await inbox.get()
            if line is None:
                break
            if not line.strip():
                continue
            try:
                message = json.loads(line)
                kind = message["type"]
            except (json.JSONDecodeError, KeyError, TypeError):
                protocol.send({"type": "error", "text": f"bad message: {line.strip()[:200]}"})
                continue
            busy = turn is not None and not turn.done()

            if kind == "message":
                if busy:
                    protocol.send({"type": "error", "text": "still working on the previous message -- cancel it first"})
                    continue
                text = _with_context(str(message.get("text", "")), message.get("context"))
                turn = asyncio.create_task(run_turn(text, message.get("model") or None, str(message.get("mode") or "default")))
            elif kind == "list_models":
                async def send_models(refresh: bool) -> None:
                    protocol.send(await asyncio.to_thread(_models_event, refresh))
                asyncio.create_task(send_models(bool(message.get("refresh"))))
            elif kind == "approval":
                future = pending.get(message.get("id"))
                if future is not None and not future.done():
                    future.set_result(bool(message.get("approved")))
            elif kind == "cancel":
                session.cancel()
                deny_pending()
            elif kind == "reset":
                if busy:
                    protocol.send({"type": "error", "text": "can't start a new chat while a message is running -- cancel it first"})
                    continue
                session.reset()
                protocol.send({"type": "reset_done"})
            elif kind == "shutdown":
                break
            else:
                protocol.send({"type": "error", "text": f"unknown message type: {kind}"})
    finally:
        if turn is not None and not turn.done():
            session.cancel()
            deny_pending()
            # A blocking model/tool call can't be interrupted; it finishes
            # into a cancelled session, which then stops at its next step.
            try:
                await asyncio.wait_for(turn, timeout=10)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                pass
        await asyncio.to_thread(session.close)
