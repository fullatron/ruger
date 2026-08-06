"""Provider selection + the JSON salvaging the openai path depends on.

No network, no API key needed.

    .venv/bin/python scratch/test_providers.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pkm import config  # noqa: E402
from pkm.providers import ProviderError, get_provider, parse_json_object  # noqa: E402


def check(label, actual, expected):
    ok = actual == expected
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {actual!r}" + ("" if ok else f" != {expected!r}"))
    if not ok:
        raise SystemExit(1)


def raises(label, fn):
    try:
        fn()
    except ProviderError:
        print(f"  PASS  {label}: raised ProviderError")
        return
    print(f"  FAIL  {label}: did not raise")
    raise SystemExit(1)


GOOD = {"commitments": [{"task": "Audit the profiles"}]}


def main() -> None:
    print("parse_json_object — packaging repairs:")
    check("clean object", parse_json_object('{"commitments":[{"task":"Audit the profiles"}]}'), GOOD)
    check("markdown fence",
          parse_json_object('```json\n{"commitments":[{"task":"Audit the profiles"}]}\n```'), GOOD)
    check("bare fence, no language",
          parse_json_object('```\n{"commitments":[{"task":"Audit the profiles"}]}\n```'), GOOD)
    check("prose before the json",
          parse_json_object('Sure! Here are the commitments:\n{"commitments":[{"task":"Audit the profiles"}]}'), GOOD)
    check("prose after the json",
          parse_json_object('{"commitments":[{"task":"Audit the profiles"}]}\nLet me know if you need more.'), GOOD)
    check("bare array gets wrapped",
          parse_json_object('[{"task":"Audit the profiles"}]'), GOOD)
    check("fenced bare array",
          parse_json_object('```json\n[{"task":"Audit the profiles"}]\n```'), GOOD)
    check("nested one level deeper",
          parse_json_object('{"result":{"commitments":[{"task":"Audit the profiles"}]}}'), GOOD)
    check("empty list preserved", parse_json_object('{"commitments":[]}'), {"commitments": []})

    # MiniMax M2.7 on Featherless returns the whole answer inside an array
    # (measured 2026-08-06). Wrapping that again buries the list one level down,
    # the validator correctly refuses the result, and every task is lost to a
    # stray bracket. Unwrapping renames nothing, so a genuinely wrong shape is
    # still wrong.
    check("the answer wrapped in an array is unwrapped, not wrapped twice",
          parse_json_object('[{"commitments":[{"task":"Audit the profiles"}]}]'), GOOD)
    check("and an empty one survives it",
          parse_json_object('[{"commitments":[]}]'), {"commitments": []})
    check("two objects are not one answer, so the old rule still applies",
          parse_json_object('[{"commitments":[]},{"commitments":[]}]'),
          {"commitments": [{"commitments": []}, {"commitments": []}]})

    print("\n  braces inside strings must not confuse the scanner:")
    tricky = 'Here:\n{"commitments":[{"task":"Fix the {broken} thing","quote":"use } carefully"}]}'
    check("brace in a string value",
          parse_json_object(tricky),
          {"commitments": [{"task": "Fix the {broken} thing", "quote": "use } carefully"}]})
    check("escaped quote in a value",
          parse_json_object(r'{"commitments":[{"task":"Say \"hi\" to Maya"}]}'),
          {"commitments": [{"task": 'Say "hi" to Maya'}]})

    print("\n  it repairs packaging, never content:")
    # Wrong field names survive as-is so the validator can reject them —
    # salvaging must never invent or rename fields.
    check("wrong keys pass through untouched",
          parse_json_object('{"commitments":[{"person":"Alex","commitment":"audit"}]}'),
          {"commitments": [{"person": "Alex", "commitment": "audit"}]})
    raises("empty response", lambda: parse_json_object(""))
    raises("whitespace only", lambda: parse_json_object("   \n  "))
    raises("no json at all", lambda: parse_json_object("I could not find any commitments."))
    raises("truncated json", lambda: parse_json_object('{"commitments":[{"task":"Aud'))

    print("\nprovider selection:")
    original = (config.PROVIDER, config.BASE_URL, config.MODEL, config.API_KEY)
    try:
        config.PROVIDER, config.BASE_URL = "anthropic", None
        config.MODEL, config.API_KEY = "claude-haiku-4-5", "sk-ant-test"
        p = get_provider()
        check("anthropic selected", p.name, "anthropic")
        check("anthropic model", p.model, "claude-haiku-4-5")

        config.PROVIDER, config.BASE_URL = "openai", "https://api.featherless.ai/v1"
        config.MODEL, config.API_KEY = "google/gemma-4-31B-it", "rc_test"
        p = get_provider()
        check("openai selected", p.name, "openai")
        check("describe includes base url",
              "api.featherless.ai" in p.describe(), True)
        check("json mode starts at json_object", p._json_mode, "json_object")

        # The real failure modes: PKM_API_KEY / PKM_MODEL simply not set.
        config.PROVIDER, config.BASE_URL = "gemini", ""
        config.MODEL, config.API_KEY = "gemini-2.5-flash", "AIza-test"
        p = get_provider()
        check("gemini selected", p.name, "gemini")
        check("gemini gets its preset base url",
              p.base_url, config.PROVIDER_PRESETS["gemini"]["base_url"])
        # Gemini does honour json_schema, so it starts one rung higher and
        # steps down on its own if a response comes back the wrong shape.
        check("gemini starts at json_schema", p._json_mode, "json_schema")
        check("generic openai does not", config.PROVIDER_PRESETS["openai"]["schema_enforced"], False)

        config.PROVIDER, config.BASE_URL = "openai", "https://api.featherless.ai/v1"
        config.MODEL = "google/gemma-4-31B-it"
        config.API_KEY = None
        raises("openai with no key set", lambda: get_provider(model="m"))
        config.API_KEY = "rc_test"
        config.MODEL = ""
        raises("openai with no model set", lambda: get_provider())
        config.MODEL = "google/gemma-4-31B-it"
        raises("unknown provider", lambda: get_provider(provider="llamafile"))
    finally:
        config.PROVIDER, config.BASE_URL, config.MODEL, config.API_KEY = original

    print("\ncontext budget guard:")
    from pkm.providers.openai_provider import OpenAICompatibleProvider

    small = OpenAICompatibleProvider(
        model="m", api_key="k", base_url="http://x/v1", context_length=1000, max_tokens=800
    )
    small._check_budget("sys", "short")           # fits
    print("  PASS  short transcript accepted")
    raises("over-long transcript refused up front",
           lambda: small._check_budget("sys", "x" * 100_000))

    print("\nthe shape check comes from the schema, not from one prompt's keys:")
    # The bug this covers: the OpenAI path judged every response by
    # `parsed["commitments"]`, so the same-job judge and the capture router — which
    # return other shapes — were treated as wrong-shaped, walked the whole fallback
    # ladder, and raised. Both features failed silently, one of them into a default.
    from pkm.providers.base import array_key, shape_ok

    commitments = {"type": "object", "properties": {"commitments": {"type": "array"}},
                   "required": ["commitments"]}
    router = {"type": "object", "properties": {"kind": {"type": "string"}},
              "required": ["kind"]}
    judge = {"type": "object",
             "properties": {"same_as": {"type": "integer"},
                            "confidence": {"type": "string"}},
             "required": ["same_as", "confidence"]}

    check("extraction still accepted", shape_ok({"commitments": []}, commitments), True)
    check("a router answer is accepted", shape_ok({"kind": "command"}, router), True)
    check("a judge answer is accepted",
          shape_ok({"same_as": 4, "confidence": "high"}, judge), True)
    check("a missing required key is refused", shape_ok({"kind": "x"}, judge), False)
    check("and a non-object always is", shape_ok(["kind"], router), False)

    check("a bare array is wrapped under the array key",
          array_key(commitments), "commitments")
    check("but a router has no array to wrap into", array_key(router), None)
    check("so its bare array stays wrong",
          shape_ok(parse_json_object('["command"]', list_key=None), router), False)

    print("\nOK — salvage handles real model output, selection and guards behave.")


if __name__ == "__main__":
    main()
