"""Wispr Flow importer: one recorded meeting becomes one file in the inbox.

Wispr Flow records the call, transcribes both sides, and (once you press the
button) writes a summary. It keeps all of that locally:

    ~/Library/Application Support/Wispr Flow/
        flow.sqlite                     Meetings.title/summary/notes/speakerMap
        meetings/<uuid>/refined.ndjson  cleaned, speaker-numbered transcript
        meetings/<uuid>/live.ndjson     raw streaming transcript, mic vs system
        meetings/<uuid>/upload.ogg      audio

This module reads that and writes markdown into `~/.pkm/inbox`. It stops there:
ingest is still the ordinary inbox connector, so there remains exactly one path
into `events` (D2) and the database stays derived from files. Nothing here
touches `events` or `commitments`.

Why files rather than the obvious shortcut of inserting rows: the same reason a
pasted note is written to disk first. The inbox is the durable artifact, the
database is rebuildable from it, and one ingest path means one place where the
verbatim-quote check's input is decided.

## refined vs live

`refined.ndjson` is preferred. Wispr's refine pass rewrites the streaming output
into whole sentences and fixes names — it turned "Hey guys, Cavey" into
"Hey, Kavi" — which matters twice over here: the quote check needs contiguous
text to match, and `owner` needs a name spelled the way a human spells it.

The raw `live.ndjson` splits *mid-word* across segments ("Send" + "line"), and
its `text` fields carry a leading space only at real word boundaries. So both
readers join segments with **no separator** and let that leading space do the
work. Joining with `" "` puts "Send line" in the body, the model quotes
"Sendline", the check fails, and a real commitment is dropped silently.

## Attribution

`refined.ndjson` labels turns `speaker.id` 1/2 and drops the mic/system flag, so
identity comes from `Meetings.speakerMap`, whose `assignments` record which
speaker the microphone belonged to. That is ground truth about which promises
are yours, which is the whole reason to prefer a recorder over an exported
summary.

The other party is labelled with their **real name** rather than "Them", because
Ruger's prompt refuses to invent an owner and a generic "Them" is a weak one. A
named owner is what makes a Notion card actionable.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from .. import config

DEFAULT_HOME = "~/Library/Application Support/Wispr Flow"

# Wispr writes speaker references two ways in the same summary: a token, and
# occasionally the literal words. Both have to become a name or the extraction
# model has nobody to assign the task to.
_TOKEN = re.compile(r"<@speaker:(\d+)>")
_LITERAL = re.compile(r"\bSpeaker\s+(\d+)\b")
# Matched before the plain forms: `<@speaker:2>'s last day` has to become "My
# last day", not "Me's last day".
_POSSESSIVE = re.compile(r"(?:<@speaker:(\d+)>|\bSpeaker\s+(\d+)\b)['’]s\b")
# The generated summary arrives wrapped in a collapsible block inside the note
# body. Everything outside it is what the human typed.
_TOGGLE = re.compile(r":::toggle.*?:::", re.DOTALL)
_RULE = re.compile(r"^-{3,}$")


class WisprError(Exception):
    pass


def home(path: str | Path | None = None) -> Path:
    if path:
        return Path(path).expanduser()
    return Path(config.env("PKM_WISPR_HOME", DEFAULT_HOME)).expanduser()


def connect(base: Path) -> sqlite3.Connection:
    """Open `flow.sqlite` read-only, without disturbing a running Wispr.

    The app holds the database open in WAL mode while you are in a call. A
    read-only URI is enough in the normal case; when it is not (a -wal that
    wants recovery cannot be replayed read-only), the three files are copied to
    a temp dir and the copy is opened. Never open it writable: this process has
    no business modifying another app's store.
    """
    db_path = base / "flow.sqlite"
    if not db_path.exists():
        raise WisprError(f"no flow.sqlite under {base} — is Wispr Flow installed?")

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("SELECT 1 FROM Meetings LIMIT 1")
        return conn
    except sqlite3.Error:
        tmp = Path(tempfile.mkdtemp(prefix="wispr-"))
        for suffix in ("", "-wal", "-shm"):
            src = db_path.with_name(db_path.name + suffix)
            if src.exists():
                shutil.copy2(src, tmp / src.name)
        conn = sqlite3.connect(f"file:{tmp / db_path.name}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn


# How long after the recording stops before the meeting is read. `finalized` is
# written at stop, and the last transcript flush lands at about the same moment,
# so reading instantly risks a truncated transcript. Short, because the point of
# running often is a board that is current.
SETTLE_SECONDS = 45


def meetings(conn: sqlite3.Connection, *, settle: int = SETTLE_SECONDS) -> list[sqlite3.Row]:
    """Finished, real, settled meetings, oldest first.

    `finalized` is the signal that the recording is over, and it is written at
    stop — independent of whether a summary was ever generated. That is what lets
    this run unattended: waiting for the summary would mean waiting for a button.
    """
    have = {r["name"] for r in conn.execute("PRAGMA table_info(Meetings)")}
    filters = ["finalized = 1"]
    if "isDeleted" in have:
        filters.append("isDeleted = 0")
    if "isTourDemo" in have:
        filters.append("isTourDemo = 0")

    rows = conn.execute(
        f"SELECT * FROM Meetings WHERE {' AND '.join(filters)} ORDER BY createdAt"
    ).fetchall()

    if not settle:
        return rows

    cutoff_ms = (datetime.now().timestamp() - settle) * 1000
    settled = []
    for row in rows:
        ended = row["endedAt"] if "endedAt" in row.keys() else None
        # No `endedAt` at all means we cannot tell how fresh it is; a finalized
        # row is trusted rather than held back forever.
        if ended and float(ended) > cutoff_ms:
            continue
        settled.append(row)
    return settled


# --- speakers ---------------------------------------------------------------


def speakers(speaker_map: str | None) -> tuple[dict[int, str], int | None]:
    """Returns ({speaker_id: name}, id_of_me).

    `assignments` carries, per speaker, which person each signal thought it was.
    `mic` is the one that matters: the microphone is you by definition, so it
    identifies the owner of every "I'll do it" in the call without any
    configuration. `origin: "self"` on the person is the fallback.
    """
    try:
        data = json.loads(speaker_map or "{}")
    except json.JSONDecodeError:
        return {}, None

    people = data.get("people") or {}
    names: dict[int, str] = {}
    me: int | None = None

    for raw_id, assignment in (data.get("assignments") or {}).items():
        try:
            speaker_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if not isinstance(assignment, dict):
            continue

        for key in ("consensus", "user", "mic", "llm", "dom"):
            person = people.get(assignment.get(key) or "")
            if isinstance(person, dict) and str(person.get("name") or "").strip():
                names[speaker_id] = str(person["name"]).strip()
                break

        if assignment.get("mic"):
            me = speaker_id
        elif me is None:
            person = people.get(assignment.get("consensus") or "")
            if isinstance(person, dict) and person.get("origin") == "self":
                me = speaker_id

    return names, me


def label(speaker_id: int | None, names: dict[int, str], me: int | None) -> str:
    """How a turn is prefixed in the transcript we write."""
    if speaker_id is not None and speaker_id == me:
        return "Me"
    name = names.get(speaker_id) if speaker_id is not None else None
    return name or "Them"


def resolve_speakers(text: str, names: dict[int, str], me: int | None) -> str:
    """Turn `<@speaker:2>` and `Speaker 2` into a name, or into `Me`.

    Your own turns resolve to `Me` rather than to your name on purpose. Ruger
    folds `me`/`i`/`myself`/`self` to the canonical owner *unconditionally*,
    independent of `PKM_ME`, so this attributes your promises to you even if that
    setting is wrong or unset. Substituting the name instead would make correct
    attribution depend on configuration, which is the failure this whole path
    exists to remove.
    """
    def possessive(match: re.Match) -> str:
        raw = match.group(1) or match.group(2)
        try:
            speaker_id = int(raw)
        except (TypeError, ValueError):
            return match.group(0)
        if speaker_id == me:
            return "My"
        name = names.get(speaker_id)
        return f"{name}’s" if name else match.group(0)

    def swap(match: re.Match) -> str:
        try:
            speaker_id = int(match.group(1))
        except ValueError:
            return match.group(0)
        if speaker_id == me:
            return "Me"
        return names.get(speaker_id) or match.group(0)

    text = _POSSESSIVE.sub(possessive, text or "")
    return _LITERAL.sub(swap, _TOKEN.sub(swap, text))


# --- the note body ----------------------------------------------------------


def my_thoughts(notes: str | None) -> str:
    """What the human typed, with the generated summary removed.

    Wispr injects its summary into the note as a `:::toggle` block. Keeping it
    would put the same text in the file twice, and the second copy would give
    the model a second chance to extract the same commitment from wording that
    is not a verbatim quote of anything anyone said.
    """
    body = _TOGGLE.sub("", notes or "")
    lines = [line.rstrip() for line in body.splitlines()]
    while lines and (not lines[0].strip() or _RULE.match(lines[0].strip())):
        lines.pop(0)
    while lines and (not lines[-1].strip() or _RULE.match(lines[-1].strip())):
        lines.pop()
    return "\n".join(lines).strip()


def _segments(path: Path) -> list[dict]:
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        # `meta` announces the clock, `marker` records a pause. Neither is speech.
        if isinstance(item, dict) and "text" in item:
            rows.append(item)
    return rows


def source_of(meeting_dir: Path) -> str | None:
    """Which transcript this meeting will be read from.

    Recorded in the note so you can see at a glance which meetings are still on
    the raw stream and would improve if you pressed summarize. It is deliberately
    NOT called `source`: the inbox connector maps that key onto `events.source`,
    which is constrained to meeting/email/slack, so writing anything else there
    fails the insert.
    """
    if (meeting_dir / "refined.ndjson").exists():
        return "refined"
    if (meeting_dir / "live.ndjson").exists():
        return "live"
    return None


def transcript(meeting_dir: Path, names: dict[int, str], me: int | None) -> str:
    """Speaker-prefixed lines, refined if Wispr has produced it.

    Consecutive turns from one speaker are joined into a single line, and joined
    with no separator: see the module docstring on mid-word splits.
    """
    refined, live = meeting_dir / "refined.ndjson", meeting_dir / "live.ndjson"

    # `them` is whichever numbered speaker is not the microphone, so the raw
    # stream's mic/system flag can be expressed in the same terms as refined.
    them = next((i for i in sorted(names) if i != me), None)

    if refined.exists():
        rows = _segments(refined)

        def speaker_of(row):
            return (row.get("speaker") or {}).get("id")
    elif live.exists():
        rows = _segments(live)
        rows.sort(key=lambda r: r.get("startRecordingMs") or 0)

        def speaker_of(row):
            # The raw stream numbers every segment `1`, so its id is useless.
            # The microphone flag is the attribution.
            source = (row.get("speaker") or {}).get("source")
            return me if source == "mic" else them
    else:
        return ""

    turns: list[list] = []
    for row in rows:
        who, text = speaker_of(row), row.get("text") or ""
        if turns and turns[-1][0] == who:
            turns[-1][1] += text
        else:
            turns.append([who, text])

    out = []
    for who, text in turns:
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            out.append(f"{label(who, names, me)}: {text}")
    return "\n".join(out)


# --- shaping the file -------------------------------------------------------


def scalar(value: str) -> str:
    """Frontmatter is read by a flat key/value parser, not YAML."""
    return " ".join(str(value or "").split()).replace('"', "'").replace(",", " ")


def slug(value: str) -> str:
    """A filesystem-safe stem that keeps the title's own script.

    ASCII-only was quietly destructive: every Devanagari, Han or Arabic title
    slugged to the same fallback, so two meetings on one day produced ONE
    filename and the second overwrote the first. A whole meeting disappeared.

    Unicode letters and digits are kept; separators, dots and controls are not,
    which is what the safety was ever about.
    """
    out, dash = "", True
    for ch in value or "":
        if ch.isalnum() or unicodedata.category(ch).startswith("M"):
            out += ch.lower()
            dash = False
        elif not dash:
            out += "-"
            dash = True
        if len(out) >= 60:
            break
    return out.strip("-") or "meeting"


def meeting_date(row: sqlite3.Row | dict) -> str:
    """Local calendar date. Relative due dates are resolved against this.

    `endedAt` is epoch milliseconds, which is unambiguous; `createdAt` is a
    string in UTC and has to be converted, or a late-evening call in IST is
    filed a day early and every "by Friday" lands a day out.
    """
    ended = (row["endedAt"] if "endedAt" in row.keys() else None) if isinstance(
        row, sqlite3.Row
    ) else row.get("endedAt")
    if ended:
        try:
            return datetime.fromtimestamp(int(ended) / 1000).date().isoformat()
        except (TypeError, ValueError, OSError):
            pass

    raw = str((row["createdAt"] if isinstance(row, sqlite3.Row) else row.get("createdAt")) or "")
    text = raw.strip().replace(" +00:00", "+00:00").replace(" ", "T", 1)
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return datetime.now().date().isoformat()
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone().date().isoformat()


def to_note(row: sqlite3.Row | dict, meeting_dir: Path) -> tuple[str, str] | None:
    """Build (filename, contents). None when there is no transcript to file."""
    get = (lambda k: row[k] if k in row.keys() else None) if isinstance(row, sqlite3.Row) \
        else row.get
    names, me = speakers(get("speakerMap"))

    body = transcript(meeting_dir, names, me)
    if not body.strip():
        return None

    title = " ".join(str(get("title") or "").split()) or "Meeting"
    date = meeting_date(row)
    participants = [names[i] for i in sorted(names)]

    lines = ["---", f'title: "{scalar(title)}"', f"date: {date}",
             f"id: wispr-{get('id')}"]
    if participants:
        lines.append("participants: [" + ", ".join(scalar(p) for p in participants) + "]")
    marker = source_of(meeting_dir)
    if marker:
        lines.append(f"wispr_transcript: {marker}")
    lines.append("---")
    contents = "\n".join(lines) + "\n\n"

    summary = resolve_speakers(str(get("summary") or "").strip(), names, me)
    if summary:
        contents += summary + "\n\n"

    thoughts = resolve_speakers(my_thoughts(get("notes")), names, me)
    if thoughts:
        contents += "## My notes\n\n" + thoughts + "\n\n"

    contents += "## Transcript\n\n" + body + "\n"
    return f"{date}-{slug(title)}.md", contents


def _stale(inbox: Path, note_id: str, keep: str) -> list[Path]:
    """Files in the inbox carrying our id under a different name.

    A retitled meeting produces a new filename. Leaving the old file behind
    means two files claiming one `id`, which the inbox connector reports as a
    duplicate rather than guessing which one you meant.
    """
    from .inbox import MARKDOWN_SUFFIXES, parse_frontmatter

    out = []
    if not inbox.exists():
        return out
    for path in sorted(inbox.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in MARKDOWN_SUFFIXES:
            continue
        if path.name == keep:
            continue
        try:
            fields, _ = parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if str(fields.get("id") or "") == note_id:
            out.append(path)
    return out


def export(
    *,
    wispr_home: str | Path | None = None,
    inbox: Path | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict:
    """Write every finished Wispr meeting into the inbox. Idempotent."""
    base = home(wispr_home)
    target = Path(inbox).expanduser() if inbox else config.INBOX

    stats = {"meetings": 0, "written": 0, "unchanged": 0, "skipped": [],
             "removed": [], "files": []}

    conn = connect(base)
    try:
        rows = meetings(conn)
    finally:
        conn.close()

    if limit:
        rows = rows[-limit:]

    for row in rows:
        stats["meetings"] += 1
        meeting_dir = base / "meetings" / str(row["id"])
        built = to_note(row, meeting_dir)
        if built is None:
            stats["skipped"].append(f"{row['title'] or row['id']}: no transcript on disk")
            continue

        name, contents = built
        path = target / name
        stats["files"].append(name)

        if path.exists() and path.read_text(encoding="utf-8") == contents:
            stats["unchanged"] += 1
            continue

        for old in _stale(target, f"wispr-{row['id']}", name):
            stats["removed"].append(old.name)
            if not dry_run:
                old.unlink()

        if not dry_run:
            target.mkdir(parents=True, exist_ok=True)
            path.write_text(contents, encoding="utf-8")
        stats["written"] += 1

    return stats
