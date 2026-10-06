"""security_scan: lets the model run the static scanners itself (audit mode
uses it first thing; it's also handy mid-task). Read-only, so no approval."""

from __future__ import annotations

from typing import Any

from ..sandbox import Sandbox
from ..security import scanners
from ..security.policy import normalize


async def security_scan(sandbox: Sandbox, args: dict[str, Any]) -> str:
    path = normalize(str(args.get("path") or "."))
    files = None if path in ("", ".") else [path]
    result = scanners.scan(sandbox, files)
    counts = {s: sum(f.severity == s for f in result.findings) for s in ("high", "medium", "low")}
    header = f"security scan of {path or '.'}: {counts['high']} high, {counts['medium']} medium, {counts['low']} low"
    return f"{header}\n{result.summary(limit=80)}"


SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "security_scan",
            "description": (
                "Run static security scanners (gitleaks for secrets, bandit for Python, semgrep rules for "
                "injection/unsafe APIs in Python, JS/TS, Go, Java, C) over the repository or one path. "
                "Findings are leads to verify, not proof -- check each one in the code."
            ),
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "file or folder; default: whole repo"}},
                "required": [],
            },
        },
    },
]

HANDLERS = {"security_scan": security_scan}
