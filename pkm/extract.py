"""§5: one Claude call per meeting episode, then verify what came back.

The prompt lives in prompts/extract_commitments.md so it can be edited without
touching this file. The verbatim-quote check here is the single highest-value
piece of code in the project: it catches most hallucinated tasks, because a
model that invented a commitment usually cannot produce the line that proves it.
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

from . import config, dedup

PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "extract_commitments.md"

_SYSTEM_MARKER = re.compile(r"^#\s*=+\s*SYSTEM\s*=+\s*$", re.MULTILINE | re.IGNORECASE)
_USER_MARKER = re.compile(r"^#\s*=+\s*USER\s*=+\s*$", re.MULTILINE | re.IGNORECASE)

# A quote shorter than this cannot meaningfully corroborate anything, and short
# fragments match by accident.
MIN_QUOTE_WORDS = 4
MIN_QUOTE_CHARS = 15

COMMITMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "commitments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "Imperative, one line, no hedging."},
                    "direction": {"type": "string", "enum": ["mine", "theirs"]},
                    "owner": {"type": "string", "description": "Name as spoken, or 'me'."},
                    "due_date": {
                        "anyOf": [{"type": "string"}, {"type": "null"}],
                        "description": "YYYY-MM-DD or null.",
                    },
                    "quote": {"type": "string", "description": "Verbatim from the notes."},
                    "speaker": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                },
                "required": ["task", "direction", "owner", "due_date", "quote", "speaker"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["commitments"],
    "additionalProperties": False,
}


class ExtractionError(Exception):
    pass


# --- prompt -----------------------------------------------------------------


def load_prompt(path: Path | None = None) -> tuple[str, str]:
    """Split the prompt file into (system, user_template)."""
    text = (path or PROMPT_PATH).read_text(encoding="utf-8")

    sys_match = _SYSTEM_MARKER.search(text)
    user_match = _USER_MARKER.search(text)
    if not sys_match or not user_match or user_match.start() < sys_match.end():
        raise ExtractionError(
            f"{(path or PROMPT_PATH).name} must contain '# ===== SYSTEM =====' then "
            "'# ===== USER =====' section markers"
        )

    system = text[sys_match.end():user_match.start()].strip()
    user = text[user_match.end():].strip()
    if not system or not user:
        raise ExtractionError("both prompt sections must be non-empty")
    return system, user


def render(template: str, episode) -> str:
    # `episode` is usually a sqlite3.Row, which has no .get() — normalise first.
    if not isinstance(episode, dict):
        episode = dict(episode)

    participants = episode.get("participants") or []
    if isinstance(participants, str):
        try:
            participants = json.loads(participants or "[]")
        except json.JSONDecodeError:
            participants = [participants]

    values = {
        "MEETING_TITLE": episode.get("title") or "(untitled)",
        "MEETING_DATE": episode["started_at"],
        "ME_NAMES": ", ".join(config.ME_ALIASES),
        "PARTICIPANTS": ", ".join(participants) if participants else "(not recorded)",
        "TRANSCRIPT": episode["transcript"],
    }
    out = template
    for key, value in values.items():
        out = out.replace("{{" + key + "}}", str(value))
    return out


# --- the verbatim-quote check ----------------------------------------------

_QUOTE_CHARS = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "–": "-", "—": "-", "―": "-", "−": "-",
    " ": " ", " ": " ", " ": " ", "​": "",
    "…": "...",
})


# Markup is formatting, not content. A model reads the *rendered* text, so it
# reports `Raise 50% of the invoice` for a source line of
# `- **Raise 50% of the invoice** (Alex)`. Comparing the raw bytes makes the
# check a test of the source's formatting conventions rather than of whether
# the words were really said.
#
# Deliberately source-agnostic: no rule here encodes one product's export
# format. Markdown (`**b**`, `_i_`), Slack (`*b*`, `~s~`), and HTML email
# (`<b>`) are all just delimiters to strip, which is what keeps adding a
# connector a connector and not a rewrite (D1).
_HTML_TAG = re.compile(r"</?[a-zA-Z][^>]*>")
_LINK = re.compile(r"\[([^\]]*)\]\([^)\s]*\)")          # [label](url) -> label
_LINE_MARK = re.compile(r"(?m)^[ \t]*(?:[-*+•·]+[ \t]+|>[ \t]*|#{1,6}[ \t]+)")
_DELIM = re.compile(r"[*_`~]+")


def _loosen(text: str, *, markup: bool = False) -> str:
    """Fold away differences that copying introduces, and nothing more.

    Always: whitespace runs, curly vs straight quotes, dash flavours.
    With `markup`: tags, link syntax, list/heading/quote markers, emphasis.

    No word is ever added, removed or reordered, and a match must still be one
    contiguous span, so an invented or reworded sentence still fails.
    """
    text = unicodedata.normalize("NFKC", text).translate(_QUOTE_CHARS)
    if markup:
        text = _HTML_TAG.sub(" ", text)
        text = _LINK.sub(r"\1", text)
        text = _LINE_MARK.sub("", text)   # before whitespace collapse: needs line starts
        text = _DELIM.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def verify_quote(quote: str, transcript: str) -> tuple[bool, str]:
    """Check a quote really came from the transcript.

    Returns (ok, how), where `how` records how much normalising it took:
    exact | whitespace | markup | too_short | not_found. Each tier folds away a
    difference that copying introduces, without letting new words through.
    """
    quote = (quote or "").strip()
    if len(quote) < MIN_QUOTE_CHARS or len(quote.split()) < MIN_QUOTE_WORDS:
        return False, "too_short"

    if quote in transcript:
        return True, "exact"

    loose_q = _loosen(quote)
    if loose_q and loose_q in _loosen(transcript):
        return True, "whitespace"

    bare_q = _loosen(quote, markup=True)
    if bare_q and bare_q in _loosen(transcript, markup=True):
        return True, "markup"

    return False, "not_found"


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def validate(raw: dict, episode: dict) -> tuple[list[dict], list[dict]]:
    """Check one model response against the transcript.

    Returns (kept, dropped). Every dropped item carries a `_reason` so prompt
    iteration can see exactly what the model got wrong.
    """
    transcript = episode["transcript"]
    kept: list[dict] = []
    dropped: list[dict] = []

    items = raw.get("commitments")
    if not isinstance(items, list):
        return [], [{"_reason": "response had no commitments array", "payload": raw}]

    for item in items:
        if not isinstance(item, dict):
            dropped.append({"_reason": "not an object", "payload": item})
            continue

        record = dict(item)
        task = (record.get("task") or "").strip()
        owner = (record.get("owner") or "").strip()
        direction = (record.get("direction") or "").strip().lower()

        if not task:
            record["_reason"] = "empty task"
            dropped.append(record)
            continue
        if direction not in ("mine", "theirs"):
            record["_reason"] = f"bad direction {direction!r}"
            dropped.append(record)
            continue
        if not owner:
            record["_reason"] = "no owner named"
            dropped.append(record)
            continue

        ok, how = verify_quote(record.get("quote", ""), transcript)
        if not ok:
            record["_reason"] = f"quote {how}"
            dropped.append(record)
            continue

        due = (record.get("due_date") or "").strip() or None
        if due and not _DATE_RE.match(due):
            due = None  # unusable date, but the commitment itself stands

        if direction == "mine":
            owner = "me"

        kept.append({
            "task": re.sub(r"\s+", " ", task).rstrip(". "),
            "task_norm": dedup.normalise_text(task),
            "direction": direction,
            "owner": owner,
            "owner_norm": dedup.normalise_owner(owner, direction),
            "due_date": due,
            "quote": record["quote"].strip(),
            "speaker": (record.get("speaker") or "").strip() or None,
            "quote_match": how,
        })

    return kept, dropped


# --- the model call ---------------------------------------------------------


def call_model(episode: dict, provider=None) -> tuple[dict, dict]:
    """One call per episode. Returns (parsed_json, usage_info).

    Which provider answers is a config question (Anthropic, or any
    OpenAI-compatible endpoint) and makes no difference from here down: the
    validator treats every response as untrusted either way.
    """
    from .providers import ProviderError, get_provider

    system, user_template = load_prompt()
    provider = provider or get_provider()

    try:
        return provider.complete_json(system, render(user_template, episode), COMMITMENT_SCHEMA)
    except ProviderError as exc:
        raise ExtractionError(str(exc)) from exc


def extract(episode: dict, provider=None) -> dict:
    """Call the model and validate. `episode` is a row/dict from the episodes table."""
    raw, usage = call_model(episode, provider=provider)
    kept, dropped = validate(raw, episode)
    return {"kept": kept, "dropped": dropped, "usage": usage, "raw": raw}
