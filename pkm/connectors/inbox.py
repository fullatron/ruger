"""Inbox connector (D2): markdown files with frontmatter in ~/.pkm/inbox.

Manual copy-paste out of Granola for v0. The undocumented local API is never a
dependency of the build; automating the pull is step 4, and only if step 2 was
worth it.

Frontmatter is preferred but optional, because a file pasted straight out of
Granola has none. Everything falls back to something derivable from the file:

    ---
    title: Weekly with Maya
    date: 2026-08-01
    participants: [Alex, Maya]
    id: granola-892f9134          # optional; defaults to the relative path
    ---
    ... notes or transcript ...
"""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path

from .. import config

MARKDOWN_SUFFIXES = {".md", ".markdown", ".txt"}

_FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?", re.DOTALL)
_H1 = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)

# "Sat, 01 Aug 26" is what Granola's export puts on its own line.
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d %b %y",
    "%d %b %Y",
    "%d %B %Y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%b %d %Y",
    "%m/%d/%Y",
)
_DATE_LINE = re.compile(
    r"^\s*(?:\w{3,9},\s*)?"                      # optional leading weekday
    r"(\d{1,2}\s+\w{3,9}\s+\d{2,4}"             # 01 Aug 26
    r"|\w{3,9}\s+\d{1,2},?\s+\d{4}"             # Aug 1, 2026
    r"|\d{4}-\d{2}-\d{2})\s*$",                 # 2026-08-01
    re.MULTILINE,
)


class InboxError(Exception):
    pass


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split leading `---` frontmatter off. Returns (fields, remaining body).

    Deliberately a small key/value + list parser rather than a YAML dependency:
    the frontmatter we write is flat, and the body must stay byte-exact for the
    verbatim-quote check (§5).
    """
    match = _FRONTMATTER.match(text)
    if not match:
        return {}, text

    fields: dict[str, object] = {}
    key: str | None = None
    for raw in match.group(1).splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue

        if line.lstrip().startswith("- ") and key:
            fields.setdefault(key, [])
            if isinstance(fields[key], list):
                fields[key].append(line.lstrip()[2:].strip().strip("\"'"))
            continue

        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()

        if value.startswith("[") and value.endswith("]"):
            fields[key] = [
                v.strip().strip("\"'") for v in value[1:-1].split(",") if v.strip()
            ]
        elif value:
            fields[key] = value.strip("\"'")
        else:
            fields[key] = []

    return fields, text[match.end():]


def parse_date(value: str) -> str | None:
    """Return YYYY-MM-DD, or None if nothing parses."""
    value = (value or "").strip().strip("\"'")
    if not value:
        return None
    # Tolerate an ISO datetime.
    head = value.replace("T", " ").split()[0] if " " in value or "T" in value else value
    for candidate in (value, head):
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(candidate, fmt).date().isoformat()
            except ValueError:
                continue
    return None


def _title_for(path: Path, fields: dict, body: str) -> str:
    raw = fields.get("title")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    h1 = _H1.search(body)
    if h1:
        return h1.group(1).strip()
    # Granola names the export file after the meeting, sometimes with a leading
    # "# " that survived the paste.
    return path.stem.lstrip("#").strip() or path.name


def _date_for(path: Path, fields: dict, body: str) -> tuple[str, str]:
    """Returns (YYYY-MM-DD, how_we_got_it)."""
    for field in ("date", "started_at", "when"):
        value = fields.get(field)
        if isinstance(value, str):
            parsed = parse_date(value)
            if parsed:
                return parsed, f"frontmatter:{field}"

    # A bare date on its own line, as Granola exports it.
    for match in _DATE_LINE.finditer(body[:2000]):
        parsed = parse_date(match.group(1))
        if parsed:
            return parsed, "body"

    mtime = date.fromtimestamp(path.stat().st_mtime).isoformat()
    return mtime, "mtime"


def _participants_for(fields: dict) -> list[str]:
    for field in ("participants", "attendees", "people", "with"):
        value = fields.get(field)
        if isinstance(value, list):
            return [str(v) for v in value if str(v).strip()]
        if isinstance(value, str) and value.strip():
            return [p.strip() for p in value.split(",") if p.strip()]
    return []


def read_file(path: Path, inbox: Path | None = None) -> dict:
    """Parse one file into a meeting record. Never raises on a missing field."""
    text = path.read_text(encoding="utf-8", errors="replace")
    fields, body = parse_frontmatter(text)

    root = inbox or config.INBOX
    try:
        rel = str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        rel = path.name

    external_id = str(fields.get("id") or "").strip() or rel
    occurred_at, date_source = _date_for(path, fields, body)

    if not body.strip():
        raise InboxError(f"{path.name}: file has no body after frontmatter")

    return {
        "source": str(fields.get("source") or "meeting").strip().lower(),
        "external_id": external_id,
        "occurred_at": occurred_at,
        "date_source": date_source,
        "title": _title_for(path, fields, body),
        "participants": _participants_for(fields),
        # Verbatim, unmodified: the quote check in §5 runs against this text.
        "body": body,
        "raw_path": str(path),
        "actor": str(fields.get("author") or "") or None,
    }


def scan(inbox: Path | None = None) -> tuple[list[dict], list[str]]:
    """Read every markdown file in the inbox. Returns (records, problems)."""
    root = inbox or config.INBOX
    if not root.exists():
        return [], [f"inbox does not exist: {root}"]

    records: list[dict] = []
    problems: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in MARKDOWN_SUFFIXES:
            continue
        if path.name.startswith("."):
            continue
        try:
            records.append(read_file(path, root))
        except (InboxError, OSError, UnicodeError) as exc:
            problems.append(str(exc))

    seen: dict[tuple[str, str], str] = {}
    for record in records:
        key = (record["source"], record["external_id"])
        if key in seen:
            problems.append(
                f"duplicate id {record['external_id']!r}: "
                f"{seen[key]} and {record['raw_path']} — set a distinct `id:` in frontmatter"
            )
        seen[key] = record["raw_path"]

    return records, problems
