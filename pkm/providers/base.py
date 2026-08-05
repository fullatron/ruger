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
            # A bare array is a packaging error, not a content error.
            return {list_key: parsed}

    raise ProviderError(f"could not parse JSON from response: {text[:300]!r}")
