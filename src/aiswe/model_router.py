"""Picks which model to use for a task, from whatever providers you've
actually configured -- not a fixed hardcoded pair.

The only thing you should ever need to do is drop an API key into .env
(OPENROUTER_API_KEY, GROQ_API_KEY, ANTHROPIC_API_KEY, anything in
PROVIDER_KEY_ENV below) and optionally a model id in AISWE_MODEL. If you
don't name a model, this file looks at which provider keys are actually
present, filters MODEL_CATALOG down to models you can actually call, and
builds a chain: the best-fitting model for the task first, then fallbacks,
free models always preferred over paid ones unless you name a paid model
yourself. Model ids are litellm's own format: "<provider>/<model>".

This file is the one place that decides what "best for this task" and
"free" mean -- see classify_task() and MODEL_CATALOG. Both are approximate
by nature (a keyword heuristic, a hand-maintained catalog); extend either
without needing to change agent_free.py.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Which env var(s) prove a provider is usable. litellm accepts either the
# provider's own conventional name (checked against litellm's source directly
# -- see model_router.py's edit history) or, for a couple, a documented
# alternate. Add a new provider by adding one line here plus (optionally)
# entries in MODEL_CATALOG -- everything else (skip-if-missing, chain
# building, the CLI's billing banner) picks it up automatically.
PROVIDER_KEY_ENV: dict[str, tuple[str, ...]] = {
    "openrouter": ("OPENROUTER_API_KEY",),
    "groq": ("GROQ_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "cerebras": ("CEREBRAS_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
    "openai": ("OPENAI_API_KEY",),
    "mistral": ("MISTRAL_API_KEY",),
    "together_ai": ("TOGETHERAI_API_KEY",),
    "fireworks_ai": ("FIREWORKS_AI_API_KEY",),
    "deepinfra": ("DEEPINFRA_API_KEY",),
    "xai": ("XAI_API_KEY",),
    "cohere": ("COHERE_API_KEY",),
}


@dataclass(frozen=True)
class ModelInfo:
    id: str  # litellm model id, e.g. "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free"
    provider: str  # must match a key in PROVIDER_KEY_ENV
    tier: str  # "strong" (harder/riskier tasks) or "fast" (simple tasks)
    free: bool


# Known-good model ids per provider, tagged so routing can prefer free ones.
# OpenRouter/Groq entries were confirmed working with live API calls (tool
# calling verified) on 2026-09-20 -- see PLAN.md/README.md. Anthropic ids are
# current Claude models (paid, no free tier) included so the catalog is
# genuinely provider-agnostic, not so they get picked by default: the chain
# builder below only reaches for a `free=False` entry as a last resort, and
# prints a clear warning if it does. Gemini/Cerebras ids are NOT included
# here (current free-tier model names weren't verified live) -- add your own
# via AISWE_STRONG_MODELS/AISWE_FAST_MODELS once you've checked their docs.
MODEL_CATALOG: list[ModelInfo] = [
    ModelInfo("openrouter/nvidia/nemotron-3-ultra-550b-a55b:free", "openrouter", "strong", True),
    ModelInfo("openrouter/qwen/qwen3.8-27b:free", "openrouter", "strong", True),
    ModelInfo("openrouter/google/gemma-4-31b-it:free", "openrouter", "strong", True),
    ModelInfo("groq/llama-3.1-8b-instant", "groq", "fast", True),
    ModelInfo("openrouter/liquid/lfm-2.5-2.6b:free", "openrouter", "fast", True),
    ModelInfo("openrouter/google/gemma-4-26b-a4b-it:free", "openrouter", "fast", True),
    ModelInfo("anthropic/claude-sonnet-5", "anthropic", "strong", False),
    ModelInfo("anthropic/claude-haiku-4-5-20251001", "anthropic", "fast", False),
]

_COMPLEX_KEYWORDS = (
    "refactor", "architecture", "redesign", "migrate", "migration",
    "across the codebase", "multiple files", "entire", "all files",
    "rewrite", "optimi", "performance", "concurrency", "race condition",
    "security", "vulnerab", "database", "schema", "api design",
)


def _env_list(name: str) -> list[str] | None:
    value = os.environ.get(name)
    if not value:
        return None
    items = [m.strip() for m in value.split(",") if m.strip()]
    return items or None


def available_providers() -> set[str]:
    """Providers whose API key is actually present and non-empty right now."""
    return {
        provider
        for provider, key_envs in PROVIDER_KEY_ENV.items()
        if any(os.environ.get(key_env, "").strip() for key_env in key_envs)
    }


def classify_task(task: str) -> str:
    """Very rough heuristic, not a model call: length + keyword match.
    Good enough to pick a starting tier; the fallback chain covers the rest."""
    lowered = task.lower()
    if len(task) > 300 or any(k in lowered for k in _COMPLEX_KEYWORDS):
        return "complex"
    return "simple"


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for m in items:
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


def build_model_chain(task: str, override: str | None = None) -> list[str]:
    """Ordered list of litellm model ids to try in turn. An explicit --model
    (or $AISWE_MODEL) always wins and skips routing/fallback entirely --
    including calling a paid model, since you named it yourself."""
    if override:
        return [override]

    env_override = os.environ.get("AISWE_MODEL")
    if env_override:
        return [env_override]

    strong_override = _env_list("AISWE_STRONG_MODELS")
    fast_override = _env_list("AISWE_FAST_MODELS")

    if strong_override or fast_override:
        strong, fast = strong_override or [], fast_override or []
    else:
        avail = available_providers()
        candidates = [m for m in MODEL_CATALOG if m.provider in avail]
        # free models first within each tier; a paid one only appears as a
        # last-resort fallback, never as the first pick, unless named explicitly.
        strong = [m.id for m in candidates if m.tier == "strong" and m.free]
        strong += [m.id for m in candidates if m.tier == "strong" and not m.free]
        fast = [m.id for m in candidates if m.tier == "fast" and m.free]
        fast += [m.id for m in candidates if m.tier == "fast" and not m.free]

    chain = (strong + fast) if classify_task(task) == "complex" else (fast + strong)
    chain = _dedupe(chain)

    if not chain:
        configured = ", ".join(sorted(available_providers())) or "none"
        raise SystemExit(
            f"No usable model found for any configured provider (detected keys for: {configured}). "
            "Add an API key to .env (see .env.example) for at least one supported provider, "
            "or set AISWE_MODEL / AISWE_STRONG_MODELS / AISWE_FAST_MODELS to name models directly."
        )
    return chain


def build_planner_chain() -> list[str]:
    """The fast/cheap tier specifically, regardless of the main task's
    difficulty -- planning is a lightweight sub-task on its own (see
    agent/developer.py's planning phase), a different axis from "how hard is
    the actual implementation" that build_model_chain() routes on."""
    fast_override = _env_list("AISWE_FAST_MODELS")
    if fast_override:
        return fast_override

    avail = available_providers()
    candidates = [m for m in MODEL_CATALOG if m.provider in avail]
    fast = [m.id for m in candidates if m.tier == "fast" and m.free]
    fast += [m.id for m in candidates if m.tier == "fast" and not m.free]
    return fast


def is_free_model(model_id: str) -> bool:
    """Best-effort: looks up the catalog; unknown ids (e.g. a user-supplied
    override or a provider not in MODEL_CATALOG) are treated as unknown, not
    assumed free -- callers should warn rather than assert cost either way."""
    for m in MODEL_CATALOG:
        if m.id == model_id:
            return m.free
    return model_id.endswith(":free")  # OpenRouter's own convention, at least
