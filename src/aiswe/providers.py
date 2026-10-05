"""Provider registry: which model platforms aiswe can talk to, how to tell
one is configured (its API key env var), how to list its models live, and
how to turn one of aiswe's model ids into litellm.completion() arguments.

aiswe model ids are "<provider id>/<model>", e.g. "openrouter/qwen/qwen3-coder:free",
"anthropic/claude-sonnet-5", "modelscope/Qwen/Qwen3-Coder-480B-A35B-Instruct",
"custom-local/llama3.1". For providers litellm supports natively the id is
already litellm's own format; the rest (ModelScope, Qwen/DashScope, NVIDIA,
custom endpoints) are OpenAI-compatible APIs called through litellm's
"openai/" provider with an api_base.

Custom endpoints (any OpenAI-compatible server: Ollama, LM Studio, vLLM, a
company gateway, ...) come from the environment:
  AISWE_CUSTOM_ENDPOINTS='[{"name": "local", "base_url": "http://localhost:11434/v1", "api_key": ""}]'
or, for a single one in .env:
  AISWE_CUSTOM_NAME=local  AISWE_CUSTOM_BASE_URL=http://localhost:11434/v1  AISWE_CUSTOM_API_KEY=
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Provider:
    id: str  # model id prefix; equals litellm's provider name when litellm_native
    label: str
    key_envs: tuple[str, ...]  # first one is the name shown to users
    api_base: str  # OpenAI-style base URL, used for listing (and for calls when not native)
    key_url: str = ""
    litellm_native: bool = True  # False -> called as "openai/<model>" with api_base
    lister: str = "openai"  # "openai" | "openrouter" | "anthropic" | "gemini"
    free_tier: bool = False  # the platform has a usable free tier
    base_env: str | None = None  # env var that overrides api_base (e.g. a region)
    auto_prefer: tuple[str, ...] = ()  # substrings (case-insensitive) Auto looks for, best first
    fallback_models: tuple[str, ...] = ()  # shown if live listing fails
    key_optional: bool = False  # custom endpoints like Ollama need no key

    def base_url(self) -> str:
        return (os.environ.get(self.base_env, "").strip() if self.base_env else "") or self.api_base

    def api_key(self) -> str:
        for env in self.key_envs:
            value = os.environ.get(env, "").strip()
            if value:
                return value
        return ""

    def configured(self) -> bool:
        return self.key_optional or bool(self.api_key())


PROVIDERS: list[Provider] = [
    Provider("openrouter", "OpenRouter", ("OPENROUTER_API_KEY",), "https://openrouter.ai/api/v1",
             "https://openrouter.ai/settings/keys", lister="openrouter", auto_prefer=("qwen3-coder", ":free")),
    Provider("groq", "Groq", ("GROQ_API_KEY",), "https://api.groq.com/openai/v1",
             "https://console.groq.com/keys", free_tier=True, auto_prefer=("llama-3.3-70b", "qwen", "llama")),
    Provider("anthropic", "Claude (Anthropic)", ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"), "https://api.anthropic.com/v1",
             "https://console.anthropic.com/settings/keys", lister="anthropic", auto_prefer=("sonnet", "opus", "haiku")),
    Provider("openai", "OpenAI", ("OPENAI_API_KEY",), "https://api.openai.com/v1",
             "https://platform.openai.com/api-keys", auto_prefer=("gpt-5", "gpt-4.1", "gpt-4o", "o4-mini")),
    Provider("gemini", "Google Gemini", ("GEMINI_API_KEY", "GOOGLE_API_KEY"), "https://generativelanguage.googleapis.com/v1beta",
             "https://aistudio.google.com/apikey", lister="gemini", free_tier=True,
             auto_prefer=("2.5-pro", "2.5-flash", "flash", "pro")),
    Provider("dashscope", "Qwen (Alibaba DashScope)", ("DASHSCOPE_API_KEY",),
             "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "https://modelstudio.console.alibabacloud.com/",
             litellm_native=False, base_env="DASHSCOPE_API_BASE",
             auto_prefer=("qwen3-coder-plus", "qwen-coder", "qwen-max", "qwen-plus"),
             fallback_models=("qwen3-coder-plus", "qwen-max", "qwen-plus", "qwen-turbo")),
    Provider("modelscope", "ModelScope", ("MODELSCOPE_API_KEY",), "https://api-inference.modelscope.cn/v1",
             "https://modelscope.cn/my/myaccesstoken", litellm_native=False, free_tier=True,
             auto_prefer=("qwen3-coder", "qwen3", "qwen")),
    Provider("nvidia", "NVIDIA NIM", ("NVIDIA_API_KEY", "NVIDIA_NIM_API_KEY"), "https://integrate.api.nvidia.com/v1",
             "https://build.nvidia.com/settings/api-keys", litellm_native=False, free_tier=True,
             auto_prefer=("qwen3-coder", "nemotron", "llama-3.3-70b", "llama")),
    Provider("deepseek", "DeepSeek", ("DEEPSEEK_API_KEY",), "https://api.deepseek.com",
             "https://platform.deepseek.com/api_keys", auto_prefer=("deepseek-chat",)),
    Provider("mistral", "Mistral", ("MISTRAL_API_KEY",), "https://api.mistral.ai/v1",
             "https://console.mistral.ai/api-keys", auto_prefer=("devstral", "codestral", "mistral-large", "mistral-medium")),
    Provider("xai", "xAI (Grok)", ("XAI_API_KEY",), "https://api.x.ai/v1",
             "https://console.x.ai/", auto_prefer=("grok-code", "grok-4", "grok")),
    Provider("cerebras", "Cerebras", ("CEREBRAS_API_KEY",), "https://api.cerebras.ai/v1",
             "https://cloud.cerebras.ai/", free_tier=True, auto_prefer=("qwen-3", "llama-3.3", "llama")),
    Provider("together_ai", "Together AI", ("TOGETHERAI_API_KEY", "TOGETHER_API_KEY"), "https://api.together.xyz/v1",
             "https://api.together.ai/settings/api-keys", auto_prefer=("qwen3-coder", "llama-3.3-70b")),
    Provider("fireworks_ai", "Fireworks AI", ("FIREWORKS_AI_API_KEY", "FIREWORKS_API_KEY"), "https://api.fireworks.ai/inference/v1",
             "https://fireworks.ai/account/api-keys", auto_prefer=("qwen3-coder", "llama")),
    Provider("deepinfra", "DeepInfra", ("DEEPINFRA_API_KEY",), "https://api.deepinfra.com/v1/openai",
             "https://deepinfra.com/dash/api_keys", auto_prefer=("qwen3-coder", "llama-3.3-70b")),
]

_SLUG = re.compile(r"[^a-z0-9_-]+")


def _custom_endpoints() -> list[Provider]:
    entries: list[dict[str, Any]] = []
    raw = os.environ.get("AISWE_CUSTOM_ENDPOINTS", "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            entries = [e for e in parsed if isinstance(e, dict)] if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            entries = []
    if os.environ.get("AISWE_CUSTOM_BASE_URL", "").strip():
        entries.append({
            "name": os.environ.get("AISWE_CUSTOM_NAME", "custom"),
            "base_url": os.environ["AISWE_CUSTOM_BASE_URL"],
            "api_key": os.environ.get("AISWE_CUSTOM_API_KEY", ""),
        })

    out: list[Provider] = []
    for e in entries:
        base = str(e.get("base_url", "")).strip().rstrip("/")
        slug = _SLUG.sub("-", str(e.get("name", "")).strip().lower()).strip("-") or "custom"
        if not base:
            continue
        provider_id = f"custom-{slug}"
        # The key is handed over through a per-endpoint env var so Provider
        # stays a plain description (api_key() reads the environment).
        key_env = f"AISWE_KEY_{provider_id.upper().replace('-', '_')}"
        os.environ[key_env] = str(e.get("api_key", "") or "")
        out.append(Provider(provider_id, f"{e.get('name') or slug} (custom)", (key_env,), base,
                            litellm_native=False, key_optional=True))
    return out


def all_providers() -> list[Provider]:
    return PROVIDERS + _custom_endpoints()


def get_provider(provider_id: str) -> Provider | None:
    return next((p for p in all_providers() if p.id == provider_id), None)


def split_model_id(model_id: str) -> tuple[str, str]:
    provider_id, _, model = model_id.partition("/")
    return provider_id, model


def configured_providers() -> list[Provider]:
    return [p for p in all_providers() if p.configured()]


def model_available(model_id: str) -> bool:
    """False only for a known provider with no key -- an unknown prefix is
    passed through to litellm as-is, which may know it."""
    provider = get_provider(split_model_id(model_id)[0])
    return provider is None or provider.configured()


def completion_kwargs(model_id: str) -> dict[str, Any]:
    """litellm.completion() arguments (model, api_key, api_base) for an aiswe model id."""
    provider_id, model = split_model_id(model_id)
    provider = get_provider(provider_id)
    if provider is None:
        return {"model": model_id}
    kwargs: dict[str, Any] = {}
    key = provider.api_key()
    if provider.litellm_native:
        kwargs["model"] = model_id
        if provider.base_env and os.environ.get(provider.base_env, "").strip():
            kwargs["api_base"] = provider.base_url()
    else:
        kwargs["model"] = f"openai/{model}"
        kwargs["api_base"] = provider.base_url()
        key = key or "none"  # the OpenAI client insists on some key, local servers ignore it
    if key:
        kwargs["api_key"] = key
    return kwargs


# --- live model listing ------------------------------------------------------

@dataclass
class ModelEntry:
    id: str  # full aiswe model id
    name: str
    free: bool = False


@dataclass
class ProviderModels:
    provider: Provider
    models: list[ModelEntry] = field(default_factory=list)
    error: str | None = None


# Not chat models -- no use to a coding agent.
_NON_CHAT = re.compile(
    r"embed|whisper|tts|dall-e|image|moderation|audio|realtime|transcribe|rerank|guard|"
    r"speech|davinci|babbage|sora|clip|-ocr|safety", re.IGNORECASE,
)
_CACHE_SECONDS = 600
_cache: dict[str, tuple[float, ProviderModels]] = {}


def _get_json(url: str, headers: dict[str, str]) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "aiswe", **headers})
    with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310 -- fixed provider URLs / user's own endpoint
        return json.loads(response.read().decode("utf-8"))


def _fetch(provider: Provider) -> list[ModelEntry]:
    key = provider.api_key()
    base = provider.base_url().rstrip("/")
    prefix = provider.id + "/"

    if provider.lister == "openrouter":
        data = _get_json(f"{base}/models", {"Authorization": f"Bearer {key}"} if key else {})["data"]
        out = []
        for m in data:
            params = m.get("supported_parameters")
            if params is not None and "tools" not in params:
                continue  # the agent needs tool calling
            pricing = m.get("pricing") or {}
            free = m["id"].endswith(":free") or (str(pricing.get("prompt")) == "0" and str(pricing.get("completion")) == "0")
            out.append(ModelEntry(prefix + m["id"], m.get("name") or m["id"], free))
        return out

    if provider.lister == "anthropic":
        data = _get_json(f"{base}/models?limit=1000", {"x-api-key": key, "anthropic-version": "2023-06-01"})["data"]
        return [ModelEntry(prefix + m["id"], m.get("display_name") or m["id"]) for m in data]

    if provider.lister == "gemini":
        data = _get_json(f"{base}/models?pageSize=1000", {"x-goog-api-key": key}).get("models", [])
        out = []
        for m in data:
            if "generateContent" not in m.get("supportedGenerationMethods", []):
                continue
            model = m["name"].removeprefix("models/")
            if _NON_CHAT.search(model):
                continue
            out.append(ModelEntry(prefix + model, m.get("displayName") or model, provider.free_tier))
        return out

    payload = _get_json(f"{base}/models", {"Authorization": f"Bearer {key}"} if key else {})
    data = payload if isinstance(payload, list) else payload.get("data", [])  # Together returns a bare list
    out = []
    for m in data:
        model = m.get("id") if isinstance(m, dict) else None
        if not model or _NON_CHAT.search(model) or (isinstance(m, dict) and m.get("type") not in (None, "chat", "language", "code")):
            continue
        out.append(ModelEntry(prefix + model, model, provider.free_tier))
    return out


def list_models(provider: Provider, *, refresh: bool = False) -> ProviderModels:
    """Live model list for one provider (cached for a few minutes). Never raises."""
    cached = _cache.get(provider.id)
    if cached and not refresh and time.monotonic() - cached[0] < _CACHE_SECONDS:
        return cached[1]
    result = ProviderModels(provider)
    try:
        result.models = sorted(_fetch(provider), key=lambda m: m.id.lower())
    except urllib.error.HTTPError as e:
        result.error = f"HTTP {e.code} from {provider.label}" + (" -- check the API key" if e.code in (401, 403) else "")
    except Exception as e:  # noqa: BLE001 -- network/JSON errors, reported to the user
        result.error = f"couldn't list models: {type(e).__name__}: {e}"
    if not result.models and provider.fallback_models:
        result.models = [ModelEntry(f"{provider.id}/{m}", m, provider.free_tier) for m in provider.fallback_models]
    _cache[provider.id] = (time.monotonic(), result)
    return result


def list_all_models(*, refresh: bool = False) -> list[ProviderModels]:
    """Every configured provider's models, fetched in parallel."""
    providers = configured_providers()
    if not providers:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(providers))) as pool:
        return list(pool.map(lambda p: list_models(p, refresh=refresh), providers))


def auto_picks(per_provider: int = 2) -> list[str]:
    """Good coding models from each configured provider, for Auto routing:
    the first listed models matching each provider's auto_prefer patterns
    (or its first models, for custom endpoints). Free-tier platforms first."""
    picks: list[str] = []
    ordered = sorted(list_all_models(), key=lambda pm: not pm.provider.free_tier)
    for pm in ordered:
        chosen: list[str] = []
        models = pm.models
        if pm.provider.id == "openrouter":
            models = [m for m in models if m.free]  # paid OpenRouter models only if picked by hand
        for pattern in pm.provider.auto_prefer or ("",):
            for m in models:
                if len(chosen) >= per_provider:
                    break
                if pattern.lower() in m.id.lower() and m.id not in chosen:
                    chosen.append(m.id)
        picks += chosen[:per_provider]
    return picks


def is_free(model_id: str) -> bool:
    provider_id, model = split_model_id(model_id)
    if model.endswith(":free"):
        return True
    cached = _cache.get(provider_id)
    if cached:
        for m in cached[1].models:
            if m.id == model_id:
                return m.free
    provider = get_provider(provider_id)
    return bool(provider and provider.free_tier)
