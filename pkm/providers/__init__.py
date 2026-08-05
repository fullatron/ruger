"""Provider selection.

    PKM_PROVIDER=anthropic          (default)
    PKM_PROVIDER=openai + PKM_BASE_URL + PKM_API_KEY + PKM_MODEL

Setting PKM_BASE_URL alone is enough — it implies the openai-compatible path.
"""

from __future__ import annotations

from .. import config
from .base import Provider, ProviderError, parse_json_object

__all__ = ["Provider", "ProviderError", "parse_json_object", "get_provider"]

_cache: dict[tuple, Provider] = {}


def get_provider(
    provider: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    context_length: int | None = None,
) -> Provider:
    name = (provider or config.PROVIDER).strip().lower()
    model = model or config.MODEL
    api_key = api_key if api_key is not None else config.API_KEY
    base_url = base_url if base_url is not None else config.BASE_URL
    context_length = context_length if context_length is not None else config.CONTEXT_LENGTH

    key = (name, model, base_url, bool(api_key), context_length)
    if key in _cache:
        return _cache[key]

    if name == "anthropic":
        from .anthropic_provider import AnthropicProvider

        instance: Provider = AnthropicProvider(model=model, api_key=api_key, base_url=base_url)
    elif name == "gemini":
        from .openai_provider import OpenAICompatibleProvider

        # Gemini speaks OpenAI's shape and does honour json_schema, so start
        # there; the provider steps down on its own if a response comes back
        # the wrong shape.
        instance = OpenAICompatibleProvider(
            model=model,
            api_key=api_key,
            base_url=base_url or config.PROVIDER_PRESETS["gemini"]["base_url"],
            context_length=context_length,
            preferred_json_mode="json_schema",
            label="gemini",
        )
    elif name in ("openai", "openai-compatible", "openai_compatible", "compatible"):
        from .openai_provider import OpenAICompatibleProvider

        # json_object is the proven-safe starting point for arbitrary endpoints.
        instance = OpenAICompatibleProvider(
            model=model,
            api_key=api_key,
            base_url=base_url,
            context_length=context_length,
            preferred_json_mode="json_object",
        )
    else:
        raise ProviderError(
            f"unknown PKM_PROVIDER {name!r} — expected one of: "
            f"{', '.join(config.PROVIDER_PRESETS)}"
        )

    _cache[key] = instance
    return instance
