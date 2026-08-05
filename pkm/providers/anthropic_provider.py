"""Anthropic provider — the default. Uses native structured outputs."""

from __future__ import annotations

import json

from .base import Provider, ProviderError


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, model: str, api_key: str | None = None, base_url: str | None = None):
        try:
            import anthropic
        except ModuleNotFoundError as exc:
            raise ProviderError(
                "the anthropic SDK is not installed — "
                "uv pip install --python .venv/bin/python anthropic"
            ) from exc

        kwargs = {}
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        # With no api_key, the SDK resolves ANTHROPIC_API_KEY, then
        # ANTHROPIC_AUTH_TOKEN, then an `ant auth login` profile.
        self.client = anthropic.Anthropic(**kwargs)
        self.model = model

    def complete_json(self, system: str, user: str, schema: dict) -> tuple[dict, dict]:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=8000,
            system=system,
            messages=[{"role": "user", "content": user}],
            # Enforced server-side: the response is guaranteed to match.
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )

        if response.stop_reason == "refusal":
            raise ProviderError(f"model refused: {getattr(response, 'stop_details', None)}")
        if response.stop_reason == "max_tokens":
            raise ProviderError("response hit max_tokens — transcript too long for one call")

        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise ProviderError("no text block in response")

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderError(f"response was not JSON: {text[:200]!r}") from exc

        usage = {
            "provider": self.name,
            "model": response.model,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "structured": "json_schema",
        }
        return parsed, usage
