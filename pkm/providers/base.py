"""Provider interface + the JSON salvaging that open-weight models need.

Extraction only ever needs one thing from a model: a JSON object matching the
commitment schema. Everything provider-specific lives behind `complete_json`.
"""

from __future__ import annotations

import json
import re


class ProviderError(Exception):
    pass


class Provider:
    """Return a parsed JSON object for one system+user prompt pair."""

    name = "base"
    model = ""

    def complete_json(self, system: str, user: str, schema: dict) -> tuple[dict, dict]:
        raise NotImplementedError

    def describe(self) -> str:
        return f"{self.name}:{self.model}"


def required_keys(schema: dict | None) -> list[str]:
    return [str(k) for k in ((schema or {}).get("required") or [])]


def array_key(schema: dict | None) -> str | None:
    """The one required property that is an array, if there is exactly one.

    A model that returns a bare array meant to fill that key is a packaging error,
    not a content error, so it can be wrapped. With no such key — a router
    answering `{"kind": "..."}` — a bare array is simply wrong and stays wrong.
    """
    properties = (schema or {}).get("properties") or {}
    arrays = [k for k in required_keys(schema)
              if (properties.get(k) or {}).get("type") == "array"]
    return arrays[0] if len(arrays) == 1 else None


def shape_ok(parsed: object, schema: dict | None) -> bool:
    """Does this response have the keys the schema said were required?

    The whole reason the OpenAI path judges a response by its shape rather than by
    the status code (see the module docstring there). It has to be derived from the
    schema: hardcoding one prompt's key silently breaks every other prompt, which
    is exactly what happened when the same-job judge and the router were added.
    """
    if not isinstance(parsed, dict):
        return False
    keys = required_keys(schema)
    return all(key in parsed for key in keys) if keys else True


_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)\s*```", re.DOTALL)


def _balanced(text: str, open_ch: str, close_ch: str) -> str | None:
    """First balanced open/close span, ignoring braces inside strings."""
    start = text.find(open_ch)
    if start == -1:
        return None
    depth, in_string, escaped = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def parse_json_object(text: str, list_key: str = "commitments") -> dict:
    """Get a dict out of whatever the model actually said.

    Needed because open-weight models routinely wrap JSON in markdown fences,
    prepend a sentence, or return a bare array. This only repairs *packaging* —
    it never invents or renames fields, so a wrong-shaped response still fails
    validation downstream instead of quietly becoming a task.
    """
    if not text or not text.strip():
        raise ProviderError("model returned an empty response")

    candidates: list[str] = []
    stripped = text.strip()
    candidates.append(stripped)

    fenced = _FENCE.search(text)
    if fenced:
        candidates.append(fenced.group(1).strip())

    for opener, closer in (("{", "}"), ("[", "]")):
        span = _balanced(text, opener, closer)
        if span:
            candidates.append(span)

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            # Some models nest the array one level deeper than asked.
            if list_key not in parsed:
                for value in parsed.values():
                    if isinstance(value, dict) and list_key in value:
                        return value
            return parsed
        if isinstance(parsed, list):
            # An array holding the answer rather than being it. MiniMax M2.7
            # returns `[{"commitments": [...]}]` on Featherless (measured
            # 2026-08-06), and wrapping that again gives
            # `{"commitments": [{"commitments": [...]}]}` — which the validator
            # correctly refuses, so every task is lost to a stray bracket.
            # Unwrapping is packaging repair: no field is renamed or invented.
            if len(parsed) == 1 and isinstance(parsed[0], dict) and list_key in parsed[0]:
                return parsed[0]
            # A bare array is a packaging error, not a content error.
            return {list_key: parsed}

    raise ProviderError(f"could not parse JSON from response: {text[:300]!r}")
