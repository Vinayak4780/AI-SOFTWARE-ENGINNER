"""Strip secrets from text before it is sent to a model provider.

Everything a tool returns (file contents, command output, scanner reports)
goes to whichever provider is serving the model -- possibly a free tier that
logs prompts. redact() replaces anything that looks like a credential with a
placeholder; policy.py then refuses writes that would put a placeholder back
into a file (which would silently destroy the real secret).
"""

from __future__ import annotations

import re

PLACEHOLDER_PREFIX = "[REDACTED:"

# (label, pattern). Patterns with a group named "secret" redact only that
# group, keeping the surrounding text (e.g. the variable name) readable.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private-key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.DOTALL)),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,})\b")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("openrouter-key", re.compile(r"\bsk-or-[A-Za-z0-9_-]{20,}")),
    ("openai-key", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}")),
    ("groq-key", re.compile(r"\bgsk_[A-Za-z0-9]{40,}")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("nvidia-key", re.compile(r"\bnvapi-[A-Za-z0-9_-]{40,}")),
    ("slack-token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("stripe-key", re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{20,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("url-credentials", re.compile(r"(?<=://)(?P<secret>[^/\s:@]+:[^/\s@]{3,})(?=@)")),
    # NAME=value / "name": "value" for secret-sounding names. The value must
    # look like a literal (8+ non-space chars, not a ${VAR}/os.environ lookup).
    ("assignment", re.compile(
        r"(?i)\b[\w.-]*(?:api[_-]?key|secret|passw(?:or)?d|passwd|token|auth|credential|private[_-]?key)[\w.-]*"
        r"\s*[\"']?\s*[:=]\s*[\"']?(?P<secret>(?![\"']?\$\{)(?!os\.|process\.env|getenv)[^\s\"'`,;#]{8,})"
    )),
]


def redact(text: str) -> tuple[str, int]:
    """Returns (redacted text, number of secrets replaced)."""
    count = 0
    for label, pattern in _PATTERNS:
        def replace(match: re.Match[str], label: str = label) -> str:
            nonlocal count
            value = match.group(0)
            if PLACEHOLDER_PREFIX in value:
                return value
            count += 1
            if "secret" in pattern.groupindex and match.group("secret") is not None:
                start, end = match.span("secret")
                offset = match.start()
                return value[: start - offset] + f"{PLACEHOLDER_PREFIX}{label}]" + value[end - offset:]
            return f"{PLACEHOLDER_PREFIX}{label}]"
        text = pattern.sub(replace, text)
    return text, count


def contains_placeholder(text: str) -> bool:
    return PLACEHOLDER_PREFIX in text
