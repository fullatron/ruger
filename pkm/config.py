"""Configuration. Everything overridable by env var, nothing required.

Values come from the process environment, falling back to a `.env` file at the
repo root (gitignored — that is where a provider key belongs, not in source).
Real env vars always win, which is why the settings UI shows you which fields it
cannot affect.

`reload()` recomputes everything after the .env file changes on disk.
"""

from __future__ import annotations

import getpass
import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = Path(os.environ.get("PKM_ENV_FILE", REPO_ROOT / ".env"))
SCHEMA_PATH = REPO_ROOT / "schema.sql"

# Shown in place of a stored key; submitting it unchanged means "keep it".
MASKED_SENTINEL = "________KEEP________"

# What the settings page offers. `base_url` empty means the SDK default.
PROVIDER_PRESETS = {
    "anthropic": {
        "label": "Anthropic",
        "base_url": "",
        "models": ["claude-haiku-4-5", "claude-sonnet-5", "claude-opus-5"],
        "default_model": "claude-haiku-4-5",
        "key_hint": "Starts with sk-ant. Leave blank to use an `ant auth login` profile.",
        "schema_enforced": True,
        "note": "JSON schema is enforced server-side, which makes this the most reliable extraction.",
    },
    "gemini": {
        "label": "Google Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        # Suggestions only — the field takes any id the endpoint accepts, and
        # "Browse models" lists what is actually live. Note that Google serves
        # some aliases the /models endpoint does not return, so an id missing
        # from that list can still be valid.
        "models": ["gemini-3.1-flash-lite", "gemini-3-flash-preview",
                   "gemini-3.1-pro-preview", "gemini-2.5-flash", "gemini-2.5-pro"],
        "default_model": "gemini-2.5-flash",
        "key_hint": "Starts with AIza. Create one at aistudio.google.com/apikey.",
        "schema_enforced": True,
        "note": "Uses Gemini’s OpenAI-compatible endpoint. Asks for a JSON schema "
                "and falls back to JSON mode on its own if that isn’t honoured.",
    },
    "openai": {
        "label": "OpenAI-compatible",
        "base_url": "https://api.featherless.ai/v1",
        "models": [],
        "default_model": "",
        "key_hint": "Any endpoint speaking /v1/chat/completions.",
        "schema_enforced": False,
        "note": "OpenAI, Featherless, Together, Groq, OpenRouter, vLLM, Ollama, "
                "LM Studio. Schema is not enforced here, so the quote check does the work.",
    },
}


def _load_env_file(path: Path) -> dict[str, str]:
    """Minimal .env reader: KEY=value, # comments, optional quotes."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            if key:
                values[key] = value
    except OSError:
        pass
    return values


_FILE_ENV: dict[str, str] = {}


def env(name: str, default: str | None = None) -> str | None:
    """Process env first, then .env, then the default."""
    value = os.environ.get(name)
    if value:
        return value
    value = _FILE_ENV.get(name)
    if value:
        return value
    return default


def _default_me() -> str:
    """Guess from the OS account. Wrong guess => every 'mine' lands as 'theirs'."""
    try:
        user = getpass.getuser()
    except Exception:
        user = ""
    guess = user.replace(".", " ").replace("_", " ").strip().title()
    return ",".join(filter(None, [guess, "me", "I", "myself"]))


def reload() -> None:
    """Recompute every setting from the environment and the .env file."""
    global _FILE_ENV, INBOX, DB_PATH, TRASH, ME_ALIASES
    global PROVIDER, BASE_URL, MODEL, API_KEY, CONTEXT_LENGTH
    global DEDUP_THRESHOLD, SERVER_HOST, SERVER_PORT
    global NOTION_TOKEN, NOTION_DB, NOTION_PARENT

    _FILE_ENV = _load_env_file(ENV_FILE)

    # Where Granola exports get dropped (D2). Files stay the durable artifact;
    # the database is derived from them and can be rebuilt at any time.
    INBOX = Path(env("PKM_INBOX", "~/.pkm/inbox")).expanduser()
    # Removed notes move here rather than being unlinked.
    TRASH = Path(env("PKM_TRASH", "~/.pkm/trash")).expanduser()
    # Board state lives here, not in the browser (D6).
    DB_PATH = Path(env("PKM_DB", "~/.pkm/ruger.db")).expanduser()

    ME_ALIASES = [a.strip() for a in (env("PKM_ME") or _default_me()).split(",") if a.strip()]

    # A base URL implies an OpenAI-compatible endpoint, so PKM_PROVIDER is
    # rarely needed by hand.
    BASE_URL = env("PKM_BASE_URL") or env("OPENAI_BASE_URL") or ""
    PROVIDER = (env("PKM_PROVIDER") or ("openai" if BASE_URL else "anthropic")).strip().lower()

    preset = PROVIDER_PRESETS.get(PROVIDER, {})
    if not BASE_URL:
        BASE_URL = preset.get("base_url", "")
    MODEL = env("PKM_MODEL") or preset.get("default_model", "")

    # PKM_API_KEY works for any provider; provider-native names still work, and
    # for Anthropic an unset key falls through to the SDK's own resolution
    # (ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, or an `ant auth login` profile).
    native = {
        "anthropic": "ANTHROPIC_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "openai": "OPENAI_API_KEY",
    }.get(PROVIDER, "OPENAI_API_KEY")
    API_KEY = env("PKM_API_KEY") or env(native) or (
        env("GOOGLE_API_KEY") if PROVIDER == "gemini" else None
    )

    _ctx = env("PKM_CONTEXT_LENGTH")
    CONTEXT_LENGTH = int(_ctx) if _ctx and _ctx.isdigit() else None

    # D4: Jaccard over lowercased, stopworded tokens, >= 0.6.
    DEDUP_THRESHOLD = float(env("PKM_DEDUP_THRESHOLD", "0.6"))
    SERVER_HOST = env("PKM_HOST", "127.0.0.1")
    SERVER_PORT = int(env("PKM_PORT", "8765"))

    # The board the commitments get pushed to. Separate credential from the
    # extraction key: one reads meetings, the other writes your task list, and
    # they have no business sharing a secret.
    NOTION_TOKEN = env("PKM_NOTION_TOKEN") or env("NOTION_TOKEN") or ""
    # Accepts a raw id or a pasted Notion URL; normalised on the way in so the
    # settings field can be as forgiving as a paste.
    NOTION_DB = notion_id(env("PKM_NOTION_DB"))
    # Where `pkm notion setup` is allowed to create the database.
    NOTION_PARENT = notion_id(env("PKM_NOTION_PARENT"))


_H = "[0-9a-fA-F]"
_DASHED = re.compile(f"{_H}{{8}}-{_H}{{4}}-{_H}{{4}}-{_H}{{4}}-{_H}{{12}}")
# Anchored on both sides so a longer hex run is never silently truncated to 32.
_BARE = re.compile(f"(?<!{_H}){_H}{{32}}(?!{_H})")


def notion_id(value: str | None) -> str:
    """Pull a Notion object id out of whatever the user pasted.

    Ids arrive bare, dashed, or buried in a URL whose slug also carries the page
    title (`.../Ruger-Board-24f8a1…?v=<view id>`). Two things matter: the `?v=`
    view id is a *different object*, so the query string goes first; and the id
    is the last one in the path, because a slug can contain anything.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.split("?", 1)[0].split("#", 1)[0]

    found = [(m.start(), m.group()) for m in _DASHED.finditer(text)]
    found += [(m.start(), m.group()) for m in _BARE.finditer(text)]
    if not found:
        return ""

    raw = max(found)[1].replace("-", "").lower()
    return f"{raw[0:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}"


def redact(secret: str | None) -> str:
    if not secret:
        return "(not set)"
    return f"{secret[:6]}…{secret[-4:]} ({len(secret)} chars)" if len(secret) > 14 else "(set)"


reload()
