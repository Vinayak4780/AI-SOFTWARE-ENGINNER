"""Unit tests for the security layer: redaction, path policy, approval rules,
scanner output parsing, dependency checks (network faked), and the commit
gate inside AgentSession (model and sandbox faked). No Docker, no keys.

Run with: .venv\\Scripts\\python.exe -m pytest tests\\test_security.py -q
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiswe.approval import describe_call, needs_approval  # noqa: E402
from aiswe.security import deps, scanners  # noqa: E402
from aiswe.security.policy import (  # noqa: E402
    Finding, GateResult, approval_warnings, check_tool_call, is_protected, is_secret_file, sensitive_reason,
)
from aiswe.security.redact import redact  # noqa: E402

# --- redaction ---------------------------------------------------------------


def test_redacts_known_key_formats() -> None:
    text = (
        "OPENROUTER_API_KEY=sk-or-v1-0123456789abcdef0123456789abcdef\n"
        "aws = AKIAIOSFODNN7EXAMPLE\n"
        "gh: ghp_" + "a" * 36 + "\n"
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----\n"
        "db = postgres://admin:hunter2pass@db.local/app\n"
    )
    out, n = redact(text)
    assert n == 5, out
    for secret in ("sk-or-v1-0123", "AKIAIOSFODNN7EXAMPLE", "ghp_aaaa", "MIIEow", "hunter2pass"):
        assert secret not in out
    assert "OPENROUTER_API_KEY=" in out  # names stay readable


def test_redaction_keeps_code_that_reads_secrets() -> None:
    code = 'api_key = os.environ["API_KEY"]\ntoken = os.getenv("TOKEN")\npassword: str\nsecret = ${SECRET}'
    out, n = redact(code)
    assert n == 0 and out == code


def test_redacts_generic_assignment() -> None:
    out, n = redact('DB_PASSWORD = "correct-horse-battery"')
    assert n == 1 and "correct-horse" not in out and "DB_PASSWORD" in out


# --- path policy ------------------------------------------------------------


def test_path_policy() -> None:
    assert is_protected(".git/config") and is_protected("/workspace/.git/hooks/pre-commit") and is_protected(".git")
    assert not is_protected(".github/workflows/ci.yml") and not is_protected("src/.gitignore")
    assert is_secret_file(".env") and is_secret_file("config/prod.env.local") is False
    assert is_secret_file(".env.production") and is_secret_file("keys/server.pem") and is_secret_file("id_rsa")
    assert not is_secret_file(".env.example") and not is_secret_file("id_rsa.pub")
    assert sensitive_reason(".vscode/tasks.json") and sensitive_reason(".github/workflows/ci.yml")
    assert sensitive_reason("frontend/package.json") and sensitive_reason("Makefile")
    assert sensitive_reason("src/app.py") is None


def test_tool_call_refusals() -> None:
    assert check_tool_call("write_file", {"path": ".git/hooks/post-checkout", "content": "x"})
    assert check_tool_call("edit_file", {"path": "/workspace/.git/config", "old_string": "a", "new_string": "b"})
    assert check_tool_call("write_file", {"path": ".env", "content": "KEY=[REDACTED:openai-key]"})
    assert check_tool_call("write_file", {"path": "app.py", "content": "print(1)"}) is None
    assert check_tool_call("run_shell", {"command": "ls"}) is None


def test_approval_rules() -> None:
    # Secret files always ask, even with auto-approve.
    assert needs_approval("read_file", {"path": ".env"}, auto_approve=True, network=False)
    assert not needs_approval("read_file", {"path": "app.py"}, auto_approve=False, network=False)
    assert needs_approval("write_file", {"path": "a"}, auto_approve=False, network=False)
    assert not needs_approval("write_file", {"path": "a"}, auto_approve=True, network=False)
    # --yes doesn't cover network-capable tools when network is on.
    assert needs_approval("run_shell", {"command": "ls"}, auto_approve=True, network=True)
    assert not needs_approval("write_file", {"path": "a"}, auto_approve=True, network=True)
    assert approval_warnings("write_file", {"path": ".github/workflows/ci.yml", "content": ""})


def test_approval_preview_is_never_truncated() -> None:
    content = "a" * 5000 + "MALICIOUS_TAIL"
    assert "MALICIOUS_TAIL" in describe_call("write_file", {"path": "x.py", "content": content})


# --- scanner parsing --------------------------------------------------------


def test_parse_scanner_reports() -> None:
    gl = scanners.parse_gitleaks([{"File": "/workspace/app.py", "StartLine": 3, "RuleID": "generic-api-key", "Description": "Generic API Key"}])
    assert gl == [Finding("gitleaks", "high", "app.py", 3, "generic-api-key", gl[0].message)]

    bandit = scanners.parse_bandit({"results": [
        {"filename": "./app.py", "line_number": 5, "issue_severity": "HIGH", "issue_confidence": "HIGH",
         "test_id": "B602", "test_name": "subprocess_popen_with_shell_equals_true", "issue_text": "shell=True"},
        {"filename": "./b.py", "line_number": 1, "issue_severity": "LOW", "issue_confidence": "HIGH",
         "test_id": "B101", "test_name": "assert_used", "issue_text": "assert"},
    ]})
    assert [f.severity for f in bandit] == ["high", "low"] and bandit[0].path == "app.py"

    sg = scanners.parse_semgrep({"results": [{"check_id": "opt.aiswe.py-sql-string-building", "path": "db.py",
                                              "start": {"line": 9}, "extra": {"severity": "ERROR", "message": "SQL"}}]})
    assert sg[0].rule == "py-sql-string-building" and sg[0].severity == "high" and sg[0].line == 9


def test_changed_files() -> None:
    diff = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@\n+a\ndiff --git a/gone.py b/gone.py\n--- a/gone.py\n+++ /dev/null\n"
    assert scanners.changed_files(diff) == ["x.py"]


def test_gate_result_blocking() -> None:
    r = GateResult([Finding("bandit", "medium", "a", 1, "r", "m"), Finding("gitleaks", "high", "b", 2, "r", "m")], ["semgrep failed"])
    assert len(r.blocking) == 1 and r.summary().index("[HIGH]") < r.summary().index("[MEDIUM]")
    assert "[SCAN ERROR] semgrep failed" in r.summary()


# --- dependency check -------------------------------------------------------


def test_added_dependencies() -> None:
    old = "requests==2.31.0\nflask\n"
    new = "requests==2.32.3\nflask\nreqeusts-toolbelt==1.0\n# comment\n-r other.txt\n"
    added = deps.added_dependencies("requirements.txt", old, new)
    assert [(d.name, d.version, d.new) for d in added] == [("reqeusts-toolbelt", "1.0", True), ("requests", "2.32.3", False)]

    pkg_old = json.dumps({"dependencies": {"express": "^4.18.2"}})
    pkg_new = json.dumps({"dependencies": {"express": "^4.18.2", "lodahs": "1.0.0"}, "devDependencies": {"jest": "latest"}})
    assert {(d.name, d.version) for d in deps.added_dependencies("web/package.json", pkg_old, pkg_new)} == {("lodahs", "1.0.0"), ("jest", None)}


def test_dependency_check_flags_missing_and_vulnerable() -> None:
    added = [deps.Dep("PyPI", "reqeusts", None, "requirements.txt"), deps.Dep("PyPI", "django", "2.0", "requirements.txt"),
             deps.Dep("PyPI", "flask", "3.0.0", "requirements.txt", new=False)]
    checked = []
    result = deps.check(added, exists=lambda d: checked.append(d.name) or d.name != "reqeusts",
                        vulns=lambda ds: [["GHSA-1"] if d.name == "django" else [] for d in ds])
    assert checked == ["reqeusts", "django"]  # version bumps aren't existence-checked
    assert sorted(f.rule for f in result.blocking) == ["known-vulnerability", "unknown-package"]

    def offline(_dep):
        raise OSError("no network")
    assert deps.check(added[:1], exists=offline).errors  # can't check -> error, not "fine"


# --- commit gate inside the agent loop --------------------------------------


class _Msg(SimpleNamespace):
    def model_dump(self, exclude_none=True):
        d = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = [{"id": t.id, "type": "function", "function": {"name": t.function.name, "arguments": t.function.arguments}}
                               for t in self.tool_calls]
        return d


def _reply(content=None, calls=None):
    return SimpleNamespace(choices=[SimpleNamespace(message=_Msg(content=content, tool_calls=calls))], usage=None)


def _call(i, name, **args):
    return SimpleNamespace(id=f"c{i}", function=SimpleNamespace(name=name, arguments=json.dumps(args)))


def _run_session(monkeypatch, tmp_path, script, gate_results, auto_approve=False, approve=True):
    """Drive AgentSession with a scripted model; returns (events, approvals, tool messages)."""
    from aiswe.agent import developer

    steps = iter(script)
    monkeypatch.setattr(developer, "_complete_with_fallback", lambda chain, messages, log: (next(steps), "fake/model"))
    monkeypatch.setattr(developer, "build_model_chain", lambda task, override=None: ["fake/model"])
    monkeypatch.setattr(developer, "build_planner_chain", lambda: [])
    monkeypatch.setattr(developer, "_review_gate", lambda *a: None)
    monkeypatch.setattr(developer, "get_diff", lambda sandbox: "+++ b/app.py\n+x")
    gates = iter(gate_results)
    monkeypatch.setattr(developer, "run_commit_gate", lambda *a: next(gates))

    async def fake_run_tool(sandbox, name, args):
        return {"read_file": "OPENAI_API_KEY=sk-proj-" + "x" * 40, "git_commit": "committed"}.get(name, "ok")
    monkeypatch.setattr(developer, "_run_tool", fake_run_tool)

    events, approvals = [], []

    async def approver(name, args, warnings):
        approvals.append((name, warnings))
        return approve

    session = developer.AgentSession(str(tmp_path), emit=events.append, approver=approver, auto_approve=auto_approve)
    session.sandbox = SimpleNamespace(stop=lambda: None, container_name="fake")
    asyncio.run(session.send("do it"))
    tool_msgs = [m["content"] for m in session.messages if m.get("role") == "tool"]
    return events, approvals, tool_msgs


def test_gate_feeds_findings_back_then_asks_human(monkeypatch, tmp_path) -> None:
    high = GateResult([Finding("semgrep", "high", "app.py", 3, "py-sql-string-building", "SQL injection")])
    script = [_reply(calls=[_call(1, "git_commit", message="a")]),
              _reply(calls=[_call(2, "git_commit", message="b")]),
              _reply(calls=[_call(3, "git_commit", message="c")]),
              _reply("done")]
    _, approvals, tool_msgs = _run_session(monkeypatch, tmp_path, script, [high, high, high])
    assert tool_msgs[0].startswith("SECURITY GATE BLOCKED THIS COMMIT (round 1/2)") and "py-sql-string-building" in tool_msgs[0]
    assert tool_msgs[1].startswith("SECURITY GATE BLOCKED THIS COMMIT (round 2/2)")
    # Rounds used up: the human decides, with the findings on the approval card.
    assert approvals and approvals[0][0] == "git_commit" and any("py-sql-string-building" in w for w in approvals[0][1])
    assert tool_msgs[2] == "committed"


def test_gate_fails_closed_without_a_human(monkeypatch, tmp_path) -> None:
    broken = GateResult(errors=["semgrep is not installed in the sandbox image"])
    script = [_reply(calls=[_call(1, "git_commit", message="a")]), _reply("done")]
    _, approvals, tool_msgs = _run_session(monkeypatch, tmp_path, script, [broken], auto_approve=True)
    assert not approvals and tool_msgs[0].startswith("DENIED: the security gate")


def test_clean_gate_commits_and_results_are_redacted(monkeypatch, tmp_path) -> None:
    script = [_reply(calls=[_call(1, "read_file", path="config.py"), _call(2, "write_file", path=".git/hooks/pre-commit", content="x"),
                            _call(3, "git_commit", message="a")]),
              _reply("done")]
    events, _, tool_msgs = _run_session(monkeypatch, tmp_path, script, [GateResult()], auto_approve=True)
    assert "sk-proj-" not in tool_msgs[0] and "[REDACTED:" in tool_msgs[0]
    assert all("sk-proj-" not in json.dumps(e) for e in events)  # nothing reaches the UI either
    assert tool_msgs[1].startswith("ERROR:") and "protected" in tool_msgs[1]
    assert tool_msgs[2] == "committed"
