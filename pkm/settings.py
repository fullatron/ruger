"""Read/write the provider settings in `.env`.

Only provider settings are editable from the UI. Paths (`PKM_DB`, `PKM_INBOX`)
are deliberately NOT writable over HTTP — a browser should not be able to
repoint the database.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from . import config

# The only keys the settings API may write.
EDITABLE = ("PKM_PROVIDER", "PKM_BASE_URL", "PKM_API_KEY", "PKM_MODEL",
            "PKM_CONTEXT_LENGTH", "PKM_ME",
            "PKM_NOTION_TOKEN", "PKM_NOTION_DB", "PKM_NOTION_PARENT")

SECRET_KEYS = {"PKM_API_KEY", "PKM_NOTION_TOKEN"}


def read_env_file(path: Path | None = None) -> dict[str, str]:
    return config._load_env_file(path or config.ENV_FILE)


def write_env_file(updates: dict[str, str], path: Path | None = None) -> Path:
    """Merge `updates` into the .env file, preserving comments and key order.

    Written via a temp file in the same directory then renamed, so a crash
    mid-write cannot leave you with a truncated credentials file.
    """
    target = Path(path or config.ENV_FILE)
    lines = target.read_text(encoding="utf-8").splitlines() if target.exists() else []

    remaining = dict(updates)
    out: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append(raw)
            continue
        key = stripped.partition("=")[0].strip()
        if key in remaining:
            value = remaining.pop(key)
            if value is None or value == "":
                continue  # drop the line entirely so the default applies
            out.append(f"{key}={value}")
        else:
            out.append(raw)

    if remaining:
        if out and out[-1].strip():
            out.append("")
        for key, value in remaining.items():
            if value:
                out.append(f"{key}={value}")

    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".env.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(out).rstrip("\n") + "\n")
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, target)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise

    config.reload()
    _clear_provider_cache()
    return target


def _clear_provider_cache() -> None:
    from . import providers

    providers._cache.clear()


def current(include_secret: bool = False) -> dict:
    """What the app is using right now, for the settings page."""
    from . import providers

    data = {
        "provider": config.PROVIDER,
        "base_url": config.BASE_URL or "",
        "model": config.MODEL or "",
        "context_length": config.CONTEXT_LENGTH or "",
        "me": ", ".join(config.ME_ALIASES),
        "api_key_set": bool(config.API_KEY),
        "api_key_masked": config.redact(config.API_KEY),
        "notion_token_set": bool(config.NOTION_TOKEN),
        "notion_token_masked": config.redact(config.NOTION_TOKEN),
        "notion_db": config.NOTION_DB or "",
        "notion_parent": config.NOTION_PARENT or "",
        "env_file": str(config.ENV_FILE),
        "env_file_exists": config.ENV_FILE.exists(),
        "presets": config.PROVIDER_PRESETS,
        # Which values come from the real environment: those override .env, so
        # editing them here would look like a no-op.
        "env_overrides": [k for k in EDITABLE if os.environ.get(k)],
    }
    if include_secret:
        data["api_key"] = config.API_KEY or ""
        data["notion_token"] = config.NOTION_TOKEN or ""
    return data


def apply(payload: dict) -> dict:
    """Validate and persist a settings payload from the UI."""
    updates: dict[str, str] = {}
    errors: list[str] = []

    provider = (payload.get("provider") or "").strip().lower()
    if provider and provider not in config.PROVIDER_PRESETS:
        errors.append(f"unknown provider {provider!r}")
    if provider:
        updates["PKM_PROVIDER"] = provider

    for field, key in (("base_url", "PKM_BASE_URL"), ("model", "PKM_MODEL"), ("me", "PKM_ME")):
        if field in payload:
            updates[key] = str(payload.get(field) or "").strip()

    if payload.get("base_url") and not str(payload["base_url"]).startswith(("http://", "https://")):
        errors.append("base URL must start with http:// or https://")

    if "context_length" in payload:
        raw = str(payload.get("context_length") or "").strip()
        if raw and not raw.isdigit():
            errors.append("context length must be a whole number")
        updates["PKM_CONTEXT_LENGTH"] = raw

    # An absent api_key means "leave it alone"; an empty string means "clear it".
    if "api_key" in payload:
        key = str(payload.get("api_key") or "").strip()
        if key and key != config.MASKED_SENTINEL:
            updates["PKM_API_KEY"] = key
        elif key == "":
            updates["PKM_API_KEY"] = ""

    # The Notion board. Ids are normalised on the way in, so pasting the whole
    # URL out of the address bar is a supported way to fill these fields.
    for field, key in (("notion_db", "PKM_NOTION_DB"),
                       ("notion_parent", "PKM_NOTION_PARENT")):
        if field in payload:
            raw = str(payload.get(field) or "").strip()
            normalised = config.notion_id(raw)
            if raw and not normalised:
                errors.append(f"{raw!r} does not contain a Notion id")
            updates[key] = normalised

    if "notion_token" in payload:
        token = str(payload.get("notion_token") or "").strip()
        if token and token != config.MASKED_SENTINEL:
            updates["PKM_NOTION_TOKEN"] = token
        elif token == "":
            updates["PKM_NOTION_TOKEN"] = ""

    effective_provider = provider or config.PROVIDER
    if effective_provider in ("openai", "gemini"):
        model = updates.get("PKM_MODEL", config.MODEL)
        if not model:
            errors.append(f"the {effective_provider} provider needs a model id")

    if errors:
        return {"ok": False, "errors": errors}

    write_env_file(updates)
    return {"ok": True, "settings": current()}


def test_credentials() -> dict:
    """Spend one tiny call to prove the settings actually work."""
    from .providers import ProviderError, get_provider

    try:
        provider = get_provider()
    except ProviderError as exc:
        return {"ok": False, "error": str(exc)}

    try:
        parsed, usage = provider.complete_json(
            "You return JSON only.",
            'Reply with exactly {"commitments": []} and nothing else.',
            {"type": "object", "properties": {"commitments": {"type": "array"}},
             "required": ["commitments"], "additionalProperties": False},
        )
    except Exception as exc:
        return {"ok": False, "error": " ".join(str(exc).split())[:300],
                "provider": provider.describe()}

    return {
        "ok": True,
        "provider": provider.describe(),
        "shape_ok": isinstance(parsed.get("commitments"), list),
        "structured_mode": usage.get("structured"),
        "tokens": f"{usage.get('input_tokens', 0)} in / {usage.get('output_tokens', 0)} out",
    }
