"""OpenAI-compatible provider — any `/v1/chat/completions` endpoint.

Covers OpenAI, Google Gemini (via its OpenAI-compatible endpoint), Featherless,
Together, Groq, OpenRouter, vLLM, Ollama and LM Studio. Set a base URL and a
model id.

**Structured output is requested, never trusted.** Measured against Featherless
serving `google/gemma-4-31B-it` (2026-08-04):

    response_format json_schema  -> HTTP 200, schema SILENTLY IGNORED.
                                    Returned a fenced array with invented keys
                                    (person/commitment/deadline).
    response_format json_object  -> clean, correctly-shaped JSON.
    no response_format           -> correct JSON, wrapped in ``` fences.

Endpoints that accept a schema and ignore it are the reason this class checks
the *shape it got back* rather than the HTTP status, and walks down
json_schema -> json_object -> plain prompting until something usable arrives.
The mode that worked is remembered, so the cost is paid once per process.

On the Anthropic path the schema is genuinely enforced. Here it is a hope, and
the validator plus the verbatim-quote check in extract.py are what actually keep
bad rows off the board.
"""

from __future__ import annotations

from .base import Provider, ProviderError, array_key, parse_json_object, shape_ok

# Rough chars-per-token for budgeting only. Deliberately pessimistic: better to
# refuse a borderline transcript than let the endpoint silently truncate the
# notes and lose the commitments made at the end of the meeting.
CHARS_PER_TOKEN = 3.2

# Weakest-to-strongest is the fallback direction: json_schema -> json_object -> None.
_FALLBACK = {"json_schema": "json_object", "json_object": None}


class OpenAICompatibleProvider(Provider):
    name = "openai"

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        context_length: int | None = None,
        max_tokens: int = 8000,
        preferred_json_mode: str | None = "json_object",
        label: str | None = None,
    ):
        try:
            from openai import OpenAI
        except ModuleNotFoundError as exc:
            raise ProviderError(
                "the openai SDK is not installed — "
                "uv pip install --python .venv/bin/python openai"
            ) from exc

        if not api_key:
            raise ProviderError(f"no API key — set one for the {label or self.name} provider")
        if not model:
            raise ProviderError("no model — set a model id your endpoint serves")

        self.client = OpenAI(api_key=api_key, base_url=base_url or None,
                             max_retries=2, timeout=180.0)
        self.model = model
        self.base_url = base_url
        self.context_length = context_length
        self.max_tokens = max_tokens
        self._json_mode = preferred_json_mode
        if label:
            self.name = label

    def describe(self) -> str:
        where = self.base_url or "api.openai.com"
        return f"{self.name}:{self.model} @ {where}"

    def _check_budget(self, system: str, user: str) -> None:
        if not self.context_length:
            return
        estimate = int((len(system) + len(user)) / CHARS_PER_TOKEN)
        room = self.context_length - self.max_tokens
        if estimate > room:
            raise ProviderError(
                f"transcript is too long for {self.model}: ~{estimate:,} prompt tokens "
                f"but only {room:,} available ({self.context_length:,} context "
                f"- {self.max_tokens:,} reserved for output). "
                "Split the meeting, or use a longer-context model."
            )

    def _create(self, system: str, user: str, json_mode: str | None, schema: dict):
        kwargs = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if json_mode == "json_schema":
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "commitments", "strict": True, "schema": schema},
            }
        elif json_mode:
            kwargs["response_format"] = {"type": json_mode}
        return self.client.chat.completions.create(**kwargs)

    def _text(self, response) -> str:
        if not response.choices:
            raise ProviderError("response had no choices")
        choice = response.choices[0]
        if choice.finish_reason == "length":
            raise ProviderError(
                f"response hit max_tokens ({self.max_tokens}) — the JSON is truncated. "
                "Split the meeting or raise the cap."
            )
        return choice.message.content or ""

    def complete_json(self, system: str, user: str, schema: dict) -> tuple[dict, dict]:
        self._check_budget(system, user)

        attempted: list[str] = []
        mode = self._json_mode
        last_error: str | None = None

        while True:
            attempted.append(mode or "prompt_only")
            try:
                response = self._create(system, user, mode, schema)
            except Exception as exc:
                if mode is not None and _is_bad_request(exc):
                    # Endpoint rejects this mode outright — step down and retry.
                    last_error = f"{mode} rejected"
                    mode = _FALLBACK.get(mode)
                    continue
                # Anything else (timeout, 5xx, auth) is not a mode problem. The
                # episode is left unextracted so the next sync retries it.
                raise ProviderError(_brief(exc, self.model)) from exc

            try:
                # The key a bare array would belong under comes from the schema, so
                # this works for every prompt rather than only for extraction.
                parsed = parse_json_object(self._text(response),
                                           list_key=array_key(schema))
            except ProviderError as exc:
                last_error = str(exc)
                parsed = None

            # An endpoint can accept a schema and ignore it, so judge the
            # response by its shape, not by the status code.
            if parsed is not None and shape_ok(parsed, schema):
                self._json_mode = mode  # remember what actually worked
                usage = {
                    "provider": self.name,
                    "model": getattr(response, "model", self.model),
                    "input_tokens": getattr(response.usage, "prompt_tokens", 0) if response.usage else 0,
                    "output_tokens": getattr(response.usage, "completion_tokens", 0) if response.usage else 0,
                    "structured": mode or "prompt_only",
                    "downgraded_from": attempted[0] if attempted[0] != (mode or "prompt_only") else None,
                }
                return parsed, usage

            if mode in _FALLBACK:
                last_error = last_error or f"{mode} returned the wrong shape"
                mode = _FALLBACK[mode]
                continue

            wanted = ", ".join(sorted(schema.get("required") or [])) or "an object"
            raise ProviderError(
                f"{self.model} did not return an object with {wanted} "
                f"(tried {', '.join(attempted)}): {last_error or 'wrong shape'}"
            )


def _is_bad_request(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    if status in (400, 404, 422):
        return True
    text = str(exc).lower()
    return "response_format" in text or "json_schema" in text or "json_object" in text


# Gateways in front of open-weight hosts return enormous JSON error bodies
# (a Cloudflare 522 runs to ~900 characters). Keep the log readable.
_TRANSIENT = {408, 409, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 529}


def _brief(exc: Exception, model: str) -> str:
    status = getattr(exc, "status_code", None)
    text = " ".join(str(exc).split())
    if len(text) > 180:
        text = text[:180] + "…"
    hint = ""
    if status in _TRANSIENT:
        hint = " — transient; this meeting stays queued and the next sync retries it"
    elif status in (401, 403):
        hint = " — check the API key on the Settings page"
    return f"{model} request failed ({status or 'no status'}){hint}: {text}"
