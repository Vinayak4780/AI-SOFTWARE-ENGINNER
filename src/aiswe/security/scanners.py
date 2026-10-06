"""Static security scanners, run inside the sandbox (installed in
sandbox/image/Dockerfile), normalized to policy.Finding:

  gitleaks  hardcoded secrets (staged changes, or the whole tree)
  bandit    Python security issues
  semgrep   aiswe's own offline rules (sandbox/image/semgrep-rules.yml):
            injection / unsafe APIs across Python, JS/TS, Go, Java, C

A scanner that fails to run is reported as a GateResult error, never as
"no findings" -- the commit gate treats errors as unresolved (fail closed).
"""

from __future__ import annotations

import json
import shlex
from typing import Any

from ..sandbox import Sandbox
from .policy import Finding, GateResult, normalize

SEMGREP_RULES = "/opt/aiswe/semgrep-rules.yml"
MAX_FILES = 400
_EXCLUDES = (".git", ".venv", "venv", "node_modules", "build", "dist", "__pycache__", ".aiswe")


def changed_files(diff: str) -> list[str]:
    """Files added/modified in a unified diff (deleted files excluded)."""
    files = []
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            files.append(line[len("+++ b/"):].strip())
    return files


def _quote_all(files: list[str]) -> str:
    return " ".join(shlex.quote(f) for f in files[:MAX_FILES])


def _json_from(output: str) -> Any:
    start = min((i for i in (output.find("{"), output.find("[")) if i >= 0), default=-1)
    if start < 0:
        raise ValueError(f"no JSON in output: {output[:300]!r}")
    return json.loads(output[start:])


# --- parsers (pure, unit-tested) -------------------------------------------

def parse_gitleaks(report: Any) -> list[Finding]:
    out = []
    for item in report or []:
        out.append(Finding(
            "gitleaks", "high", normalize(item.get("File", "")), int(item.get("StartLine") or 0),
            item.get("RuleID", "secret"), f"hardcoded secret ({item.get('Description') or item.get('RuleID')}) -- move it to an environment variable or secret store",
        ))
    return out


def parse_bandit(report: dict[str, Any]) -> list[Finding]:
    out = []
    for r in report.get("results", []):
        sev, conf = r.get("issue_severity", "LOW"), r.get("issue_confidence", "LOW")
        if sev == "HIGH" and conf in ("MEDIUM", "HIGH"):
            severity = "high"
        elif sev in ("HIGH", "MEDIUM"):
            severity = "medium"
        else:
            severity = "low"
        out.append(Finding(
            "bandit", severity, normalize(r.get("filename", "")), int(r.get("line_number") or 0),
            f"{r.get('test_id')}:{r.get('test_name')}", r.get("issue_text", ""),
        ))
    return out


def parse_semgrep(report: dict[str, Any]) -> list[Finding]:
    levels = {"ERROR": "high", "WARNING": "medium", "INFO": "low"}
    out = []
    for r in report.get("results", []):
        extra = r.get("extra", {})
        rule = r.get("check_id", "semgrep").rsplit(".", 1)[-1]
        out.append(Finding(
            "semgrep", levels.get(extra.get("severity", "INFO"), "low"), normalize(r.get("path", "")),
            int((r.get("start") or {}).get("line") or 0), rule, extra.get("message", ""),
        ))
    return out


# --- running ---------------------------------------------------------------

def _run_json(sandbox: Sandbox, tool: str, command: str, result: GateResult, timeout: int = 300) -> Any:
    r = sandbox.run_shell(command, timeout=timeout)
    if r.exit_code in (126, 127):
        result.errors.append(f"{tool} is not installed in the sandbox image")
        return None
    try:
        return _json_from(r.stdout)
    except (ValueError, json.JSONDecodeError):
        result.errors.append(f"{tool} failed (exit {r.exit_code}): {(r.stderr or r.stdout).strip()[:300]}")
        return None


def scan(sandbox: Sandbox, files: list[str] | None = None, *, staged: bool = False) -> GateResult:
    """Scan specific files (e.g. a commit's changed files) or, with files=None,
    the whole workspace. staged=True scans secrets in the staged diff only."""
    result = GateResult()
    if files is not None and not files:
        return result

    # gitleaks: --redact keeps the secret value itself out of the report (and the model's context).
    report = "/tmp/aiswe-gitleaks.json"
    mode = "git --pre-commit --staged" if staged else "dir ."
    data = _run_json(sandbox, "gitleaks",
                     f"gitleaks {mode} --redact --no-banner --exit-code 0 --log-level error "
                     f"--report-format json --report-path {report} >/dev/null 2>&1; cat {report}", result)
    if data is not None:
        result.findings += parse_gitleaks(data)

    py_files = [f for f in files if f.endswith(".py")] if files is not None else None
    if py_files is None or py_files:
        target = _quote_all(py_files) if py_files else "-r . -x " + ",".join(f"./{e}" for e in _EXCLUDES)
        data = _run_json(sandbox, "bandit", f"bandit -f json -q {target} 2>/dev/null", result)
        if data is not None:
            result.findings += parse_bandit(data)

    target = _quote_all(files) if files is not None else "."
    excludes = "" if files is not None else " ".join(f"--exclude={e}" for e in _EXCLUDES)
    data = _run_json(sandbox, "semgrep",
                     f"semgrep scan --config {SEMGREP_RULES} --json --quiet --metrics=off --disable-version-check "
                     f"{excludes} {target} 2>/dev/null", result)
    if data is not None:
        result.findings += parse_semgrep(data)
        result.errors += [f"semgrep: {e.get('message', e)[:200]}" for e in data.get("errors", []) if e.get("level") == "error"]
    return result
