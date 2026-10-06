"""Security infrastructure (no model calls here -- the security *agent*
prompts live in agent/security.py):

  redact.py    strip secrets from anything sent to a model provider
  policy.py    protected / secret / sensitive paths, findings, what blocks a commit
  scanners.py  gitleaks + bandit + semgrep, run inside the sandbox
  deps.py      new dependencies: do they exist, are they known-vulnerable (host-side, OSV)
"""

from .policy import Finding, GateResult
from .redact import redact

__all__ = ["Finding", "GateResult", "redact"]
