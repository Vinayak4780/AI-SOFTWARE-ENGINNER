"""Security policy: which paths are protected, secret, or sensitive, and what
blocks a commit. Pure functions -- the agent loop and the approval gate call
these; nothing here touches the sandbox.

  protected  never writable by the agent (.git internals: config/hooks run
             code on the host -- also mounted read-only, see sandbox/docker.py)
  secret     reading needs explicit approval; output is redacted anyway
  sensitive  writable, but flagged on the approval card because the host or
             CI executes it (editor tasks, CI workflows, build scripts)
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from typing import Any

from .redact import contains_placeholder, redact


def normalize(path: str) -> str:
    """Agent path ("/workspace/a", "./a", "a\\b") -> "a/b" relative to the repo."""
    p = path.replace("\\", "/")
    if p == "/workspace":
        return ""
    if p.startswith("/workspace/"):
        p = p[len("/workspace/"):]
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


_SECRET_PATTERNS = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore",
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*", ".npmrc", ".pypirc", ".netrc",
    "credentials", "credentials.json", "*credentials*.json", "secrets.*", "*.secret",
    "service-account*.json", ".aws/credentials", ".docker/config.json", "*.kdbx",
)
_NOT_SECRET = (".env.example", ".env.sample", ".env.template", "*.pub")

_SENSITIVE: tuple[tuple[str, str], ...] = (
    (".vscode/*", "VS Code runs tasks/settings from here on your machine"),
    (".idea/*", "your IDE reads run configurations from here"),
    (".devcontainer/*", "defines the container your editor builds and runs"),
    (".github/workflows/*", "CI runs this with your repository's secrets"),
    (".gitlab-ci.yml", "CI runs this with your repository's secrets"),
    (".circleci/*", "CI runs this with your repository's secrets"),
    ("Jenkinsfile", "CI runs this"),
    (".pre-commit-config.yaml", "runs on your machine on every commit"),
    (".envrc", "direnv runs this when you cd into the folder"),
    ("Makefile", "runs on your machine when you use make"),
    ("package.json", "npm scripts (install hooks) run on your machine"),
    ("setup.py", "runs on your machine on pip install"),
    ("Dockerfile", "defines an image you may build and run"),
    ("docker-compose*.yml", "defines containers you may run"),
    (".gitattributes", "can configure git filters"),
    (".gitmodules", "points git at other repositories"),
)


def _match(path: str, patterns: tuple[str, ...]) -> bool:
    name = os.path.basename(path)
    return any(fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(name, pat) for pat in patterns)


def is_protected(path: str) -> bool:
    p = normalize(path)
    return p == ".git" or p.startswith(".git/")


def is_secret_file(path: str) -> bool:
    p = normalize(path)
    return _match(p, _SECRET_PATTERNS) and not _match(p, _NOT_SECRET)


def sensitive_reason(path: str) -> str | None:
    p = normalize(path)
    for pattern, reason in _SENSITIVE:
        if fnmatch.fnmatch(p, pattern) or (("/" not in pattern) and fnmatch.fnmatch(os.path.basename(p), pattern)):
            return reason
    return None


def check_tool_call(name: str, args: dict[str, Any]) -> str | None:
    """An error to return to the model instead of running the tool, or None."""
    if name in ("edit_file", "write_file"):
        path = str(args.get("path", ""))
        if is_protected(path):
            return f"ERROR: {path} is protected -- aiswe never modifies .git internals (they can run code on the host)."
        content = str(args.get("new_string" if name == "edit_file" else "content", ""))
        if contains_placeholder(content):
            return (
                "ERROR: this content contains a [REDACTED:...] placeholder. Secrets are hidden from you, so writing "
                "the placeholder would destroy the real value. Use edit_file on lines that don't contain the secret, "
                "or ask the user to make that change."
            )
    return None


def approval_warnings(name: str, args: dict[str, Any]) -> list[str]:
    """Extra lines shown on the approval card for this call."""
    warnings = []
    path = str(args.get("path", ""))
    if name in ("edit_file", "write_file") and path:
        reason = sensitive_reason(path)
        if reason:
            warnings.append(f"Sensitive file {normalize(path)}: {reason}.")
        if is_secret_file(path):
            warnings.append(f"{normalize(path)} looks like a secrets file.")
    if name == "read_file" and is_secret_file(path):
        warnings.append(f"{normalize(path)} looks like a secrets file. Its contents go to the model provider (secrets are redacted first).")
    return warnings


def needs_approval_extra(name: str, args: dict[str, Any]) -> bool:
    """Calls that need approval beyond approval.TOOLS_REQUIRING_APPROVAL."""
    return name == "read_file" and is_secret_file(str(args.get("path", "")))


# --- commit gate ---------------------------------------------------------

@dataclass
class Finding:
    tool: str  # gitleaks | bandit | semgrep | deps | security-review
    severity: str  # high | medium | low
    path: str
    line: int
    rule: str
    message: str

    def short(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"[{self.severity.upper()}] {self.tool}/{self.rule} at {where}: {self.message}"


@dataclass
class GateResult:
    findings: list[Finding] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)  # a scanner that couldn't run

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "high"]

    def summary(self, limit: int = 30) -> str:
        lines = [f.short() for f in sorted(self.findings, key=lambda f: ("high", "medium", "low").index(f.severity))[:limit]]
        if len(self.findings) > limit:
            lines.append(f"... and {len(self.findings) - limit} more")
        lines += [f"[SCAN ERROR] {e}" for e in self.errors]
        # Scanner messages can quote the secret they found (bandit does).
        return redact("\n".join(lines))[0] or "no findings"
