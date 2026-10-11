"""Build mode (agent/developer.py): build requests get a feature-checklist spec
up front and a quality check before the model may finish; other requests
don't. The model and the Docker sandbox are faked -- needs no Docker and no
API keys.

Run with: .venv\\Scripts\\python.exe tests\\test_build_mode.py  (or pytest)
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiswe.agent import developer  # noqa: E402
from aiswe.agent.developer import AgentSession, is_build_request  # noqa: E402


def test_is_build_request() -> None:
    for task in [
        "make a food market website",
        "Build me an expense tracker app",
        "create a new REST API for todos",
        "i want a website for my bakery",
        "can you generate a simple landing page for my startup?",
        "write a CLI tool that renames photos",
    ]:
        assert is_build_request(task), task
    for task in [
        "make the page responsive",
        "fix the login bug",
        "why does the cart total show NaN?",
        "add a test for the checkout form",
        "write a function that parses dates",
        "create the tests",
    ]:
        assert not is_build_request(task), task


class Msg(SimpleNamespace):
    def model_dump(self, exclude_none=True):
        d = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = [{"id": t.id, "type": "function",
                                "function": {"name": t.function.name, "arguments": t.function.arguments}}
                               for t in self.tool_calls]
        return d


def reply(content=None, tool_calls=None):
    usage = SimpleNamespace(prompt_tokens=1, completion_tokens=2)
    return SimpleNamespace(choices=[SimpleNamespace(message=Msg(content=content, tool_calls=tool_calls))], usage=usage)


def write_call(path: str, content: str):
    return [SimpleNamespace(id=f"call_{path}", function=SimpleNamespace(
        name="write_file", arguments=json.dumps({"path": path, "content": content})))]


class FakeSandbox:
    written: dict[str, str] = {}

    def __init__(self, repo_path, network=False):
        self.container_name = "fake"

    def start(self): pass
    def stop(self): pass
    def prepare_git(self, name, email, init): pass

    def call_helper(self, action, **kw):
        if action == "write_file":
            FakeSandbox.written[kw["path"]] = kw["content"]
        return {"ok": True, "entries": []}


def run_session(task: str, completion) -> tuple[list[dict], list[dict]]:
    """Runs one message; returns (events, calls) -- calls = messages of every model call."""
    FakeSandbox.written = {}
    calls: list[list[dict]] = []

    def fake_completion(model, messages, tools=None, **kwargs):
        calls.append([dict(m) for m in messages])
        return completion(messages)

    fakes = {
        "completion_kwargs": lambda model: {"model": model},
        "Sandbox": FakeSandbox,
        "build_image": lambda: None,
        "build_planner_chain": lambda: ["fake/planner"],
        "model_available": lambda model: True,
    }
    saved = {name: getattr(developer, name) for name in fakes}
    saved_completion = developer.litellm.completion
    for name, fake in fakes.items():
        setattr(developer, name, fake)
    developer.litellm.completion = fake_completion

    events: list[dict] = []

    async def approver(name, args, warnings):
        return True

    try:
        with tempfile.TemporaryDirectory() as repo:
            session = AgentSession(repo, emit=events.append, approver=approver, auto_approve=True)
            session._model_chain = lambda task, model: ["fake/main"]
            asyncio.run(session.send(task))
    finally:
        for name, original in saved.items():
            setattr(developer, name, original)
        developer.litellm.completion = saved_completion
    return events, calls


def test_build_request_gets_spec_and_quality_check() -> None:
    checks = {"n": 0}

    def completion(messages):
        first = messages[0]
        if first["role"] == "user" and first["content"].startswith("Request:"):
            return reply("2. Feature checklist:\n- [ ] menu\n- [ ] cart with totals")
        last = messages[-1]
        if last["role"] == "user" and last["content"].startswith("BUILD MODE"):
            return reply("Building.", write_call("index.html", "<h1>Shop</h1>"))
        if last["role"] == "user" and last["content"].startswith("QUALITY CHECK"):
            checks["n"] += 1
            if checks["n"] == 1:  # first check finds a gap and fixes it
                return reply("Cart totals and README missing -- fixing.", write_call("README.md", "# Shop"))
            return reply("COMPLETE: menu, cart with totals, README.")
        return reply("Done.")

    events, calls = run_session("make a food market website", completion)

    assert calls[0][0]["content"].startswith("Request:"), "spec is written first, with the main model"
    build_msg = next(m for m in calls[1] if m["role"] == "user" and m["content"].startswith("BUILD MODE"))
    assert "cart with totals" in build_msg["content"], "the spec's checklist reaches the builder"
    assert checks["n"] == 2, "a check that led to fixes is followed by one more check"
    assert set(FakeSandbox.written) == {"index.html", "README.md"}
    final = [e for e in events if e["type"] == "assistant"][-1]
    assert final["text"].startswith("COMPLETE")
    assert events[-1]["type"] == "done"


def test_quality_check_stops_when_model_changes_nothing() -> None:
    checks = {"n": 0}

    def completion(messages):
        if messages[0]["role"] == "user":
            return reply("- [ ] page")
        last = messages[-1]
        if last["role"] == "user" and last["content"].startswith("QUALITY CHECK"):
            checks["n"] += 1
            return reply("COMPLETE: all done.")
        if last["role"] == "user":
            return reply("Building.", write_call("index.html", "<h1>Hi</h1>"))
        return reply("Done.")

    run_session("build me a portfolio site", completion)
    assert checks["n"] == 1


def test_normal_request_has_no_build_mode() -> None:
    def completion(messages):
        if messages[0]["role"] == "user":
            assert messages[0]["content"].startswith("Task:"), "normal requests use the cheap planner"
            return reply("1. fix it")
        last = messages[-1]
        if last["role"] == "user":
            return reply("Fixing.", write_call("app.py", "x = 1"))
        return reply("Fixed.")

    events, calls = run_session("fix the typo in app.py", completion)
    sent = [m["content"] for c in calls for m in c if m["role"] == "user"]
    assert not any(s.startswith(("BUILD MODE", "QUALITY CHECK")) for s in sent)
    assert [e for e in events if e["type"] == "assistant"][-1]["text"] == "Fixed."


if __name__ == "__main__":
    test_is_build_request()
    test_build_request_gets_spec_and_quality_check()
    test_quality_check_stops_when_model_changes_nothing()
    test_normal_request_has_no_build_mode()
    print("ok")
