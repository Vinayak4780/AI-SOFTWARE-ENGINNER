"""Protocol test for `aiswe serve` (server.py + AgentSession), with the model
and the Docker sandbox faked out -- needs no Docker and no API keys. A tiny
local HTTP server stands in for a custom OpenAI-compatible endpoint, so model
listing and per-message model choice are exercised too.

Run with: .venv\\Scripts\\python.exe tests\\test_serve_protocol.py  (or pytest)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"

# Runs inside the server subprocess: swap in a scripted model and an
# in-memory sandbox, then start the real serve loop.
BOOTSTRAP = r'''
import asyncio, json, sys
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
from aiswe.agent import developer
from aiswe import server

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

def fake_completion(model, messages, tools=None, **kwargs):
    if messages[0]["role"] != "system":
        return reply("1. write the file")  # planner call
    last = messages[-1]
    if last["role"] == "user":
        call = SimpleNamespace(id="call_1", function=SimpleNamespace(
            name="write_file", arguments=json.dumps({"path": "hello.txt", "content": "hi"})))
        return reply("Writing it.", [call])
    return reply(f"Done: {last['content']} [{model} @ {kwargs.get('api_base')}]")

written = {}
class FakeSandbox:
    def __init__(self, repo_path, network=False):
        self.container_name = "fake"
    def start(self): pass
    def stop(self): pass
    def prepare_git(self, name, email, init): pass
    def call_helper(self, action, **kw):
        if action == "write_file":
            written[kw["path"]] = kw["content"]
        return {"ok": True, "entries": ["README.md"]}

developer.litellm.completion = fake_completion
developer.Sandbox = FakeSandbox
developer.build_image = lambda: None
asyncio.run(server.serve(sys.argv[2], model="custom-local/m0"))
'''


class _ModelsHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 -- http.server naming
        body = json.dumps({"data": [{"id": "m0"}, {"id": "m1"}, {"id": "text-embedding-x"}]}).encode()
        self.send_response(200 if self.path == "/v1/models" else 404)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


def _send(proc: subprocess.Popen, message: dict) -> None:
    proc.stdin.write(json.dumps(message) + "\n")
    proc.stdin.flush()


def _read_until(proc: subprocess.Popen, kind: str) -> list[dict]:
    events = []
    while True:
        line = proc.stdout.readline()
        assert line, f"server exited before sending {kind!r}; got {events}\nstderr:\n{proc.stderr.read()}"
        event = json.loads(line)  # every stdout line must be protocol JSON
        events.append(event)
        if event["type"] == kind:
            return events


def _run_turn(proc: subprocess.Popen, approve: bool, model: str | None = None) -> list[dict]:
    _send(proc, {"type": "message", "text": "create hello.txt", "context": {"file": "a.py", "selection": "x = 1"}, "model": model})
    events = _read_until(proc, "approval_request")
    request = events[-1]
    assert request["tool"] == "write_file" and request["args"]["path"] == "hello.txt"
    _send(proc, {"type": "approval", "id": request["id"], "approved": approve})
    return events + _read_until(proc, "turn_end")


def test_serve_protocol() -> None:
    repo = tempfile.mkdtemp(prefix="aiswe-serve-")
    endpoint = ThreadingHTTPServer(("127.0.0.1", 0), _ModelsHandler)
    threading.Thread(target=endpoint.serve_forever, daemon=True).start()
    base_url = f"http://127.0.0.1:{endpoint.server_address[1]}/v1"

    # Only the fake custom endpoint is configured -- no real provider keys
    # from the developer's environment, so nothing here touches the network.
    env = {k: v for k, v in os.environ.items() if not k.endswith(("_API_KEY", "_AUTH_TOKEN"))}
    env.update({
        "PYTHONIOENCODING": "utf-8",
        "AISWE_CUSTOM_ENDPOINTS": json.dumps([{"name": "Local", "base_url": base_url, "api_key": ""}]),
    })
    proc = subprocess.Popen(
        [sys.executable, "-c", BOOTSTRAP, str(SRC), repo],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", env=env,
    )
    try:
        assert _read_until(proc, "ready")[-1]["repo"] == repo

        events = _run_turn(proc, approve=True)
        kinds = [e["type"] for e in events]
        assert "assistant" in kinds and "done" in kinds, kinds
        result = next(e for e in events if e["type"] == "tool_result")
        assert result["result"] == "wrote hello.txt"
        assert any(e["type"] == "assistant" and e["text"] == f"Done: wrote hello.txt [openai/m0 @ {base_url}]" for e in events)

        # Same conversation continues; this time the user says no.
        events = _run_turn(proc, approve=False)
        result = next(e for e in events if e["type"] == "tool_result")
        assert result["result"].startswith("DENIED")

        # The custom endpoint's models are listed (minus non-chat ones)...
        _send(proc, {"type": "list_models"})
        listed = {p["id"]: p for p in _read_until(proc, "models")[-1]["providers"]}
        assert "anthropic" in listed and not listed["anthropic"]["configured"]
        local = listed["custom-local"]
        assert local["configured"] and local["error"] is None, local
        assert [m["id"] for m in local["models"]] == ["custom-local/m0", "custom-local/m1"]

        # ...and picking one for a message routes the call to it.
        events = _run_turn(proc, approve=True, model="custom-local/m1")
        assert any(e["type"] == "assistant" and e["text"].endswith(f"[openai/m1 @ {base_url}]") for e in events), events

        _send(proc, {"type": "reset"})
        _read_until(proc, "reset_done")
        _send(proc, {"type": "bogus"})
        assert _read_until(proc, "error")[-1]["text"].startswith("unknown message type")

        _send(proc, {"type": "shutdown"})
        assert proc.wait(timeout=30) == 0, proc.stderr.read()
    finally:
        endpoint.shutdown()
        if proc.poll() is None:
            proc.kill()


if __name__ == "__main__":
    test_serve_protocol()
    print("serve protocol test passed")
