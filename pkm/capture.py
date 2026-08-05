"""A task the moment you think of it (§10).

Text arrives from the menu bar dialog, gets written into `~/.pkm/inbox`, and then
goes through the ordinary pipeline: ingest, extract, dedup, push. There is no
direct-to-Notion path and no second extractor, which is D2 holding for the third
time — after the paste box and the Wispr importer.

The one thing that is different is which prompt reads it and how short a quote may
be, both of which live in `extract.py` and key off `kind='capture'`. See D14 and
D15 for why that is a `kind` and not a new `source`.

Ruger records no audio. The dialog is a text field; dictating into it is the
operating system's job, or Wispr Flow's. D12.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

from . import config, db, episodes, notes, sync

# Generous on purpose: a capture is often a paragraph, sometimes several, and
# dictation runs long because talking is faster than typing. ~20k characters is
# roughly 3,000 words, well past anything anyone dictates in one go.
#
# The cap is not a style rule, it is a context-window guard: the whole note goes
# into one prompt, and a transcript-sized paste belongs in the Add notes tab where
# it is stored as a meeting and read by the meeting prompt instead.
MAX_CHARS = 20_000

TITLE_WORDS = 8


class CaptureError(Exception):
    pass


def title_for(text: str, when: datetime | None = None) -> str:
    """A recognisable name for the note this becomes.

    The title is what the card shows as its source (D5), so it has to read as
    something you said rather than as a filename. First few words, and the clock
    when there are no usable ones.
    """
    words = " ".join(str(text or "").split()).split(" ")
    head = " ".join(w for w in words[:TITLE_WORDS] if w).strip(" ,.;:!?-")
    stamp = (when or datetime.now()).strftime("%H:%M")
    if not head:
        return f"Capture {stamp}"
    if len(words) > TITLE_WORDS:
        head += "…"
    return f"Capture · {head}"


def write_note(text: str, when: datetime | None = None,
               inbox: Path | None = None) -> Path:
    """Write the captured text into the inbox as a capture-kind note.

    Body is the text exactly as given: the verbatim-quote check runs against these
    bytes, so tidying it here would silently invalidate every quote the model
    returns.
    """
    text = (text or "").strip()
    if not text:
        raise CaptureError("nothing was captured")
    if len(text) > MAX_CHARS:
        raise CaptureError(
            f"that is {len(text):,} characters, over the {MAX_CHARS:,} a capture "
            f"takes. Paste it into the Add notes tab instead, which stores it as a "
            f"meeting.")

    when = when or datetime.now()
    root = Path(inbox or config.INBOX)
    root.mkdir(parents=True, exist_ok=True)

    title = title_for(text, when)
    # Seconds are in the id because two captures a minute apart are two notes, and
    # the id is what stops a re-run from creating a second episode for one of them.
    stamp = when.strftime("%Y%m%d-%H%M%S")
    path = root / f"{when.date().isoformat()}-capture-{stamp}.md"

    body = "\n".join([
        "---",
        f'title: "{notes._frontmatter_value(title)}"',
        f"date: {when.date().isoformat()}",
        "kind: capture",
        f"id: capture-{stamp}",
        "---",
        "",
        text,
        "",
    ])
    path.write_text(body, encoding="utf-8")
    return path


def notify(title: str, message: str) -> None:
    """A macOS notification. Best effort: a capture must not fail on cosmetics."""
    def clean(value: str) -> str:
        # Both fields land inside an AppleScript string literal.
        return str(value or "").replace("\\", "").replace('"', "'")

    try:
        subprocess.run(
            ["/usr/bin/osascript", "-e",
             f'display notification "{clean(message)}" with title "{clean(title)}"'],
            check=False, capture_output=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def summarise(result: dict) -> str:
    """One line for a notification or a terminal."""
    if result.get("error"):
        return result["error"]

    tasks = result["tasks"]
    if not tasks and not result["merged"]:
        return "Nothing to do in that one" + (
            f" ({result['dropped']} dropped)" if result["dropped"] else "")

    bits = []
    if tasks:
        bits.append(f"{len(tasks)} task{'s' if len(tasks) != 1 else ''}")
    if result["merged"]:
        bits.append(f"{result['merged']} merged")
    if result["dropped"]:
        bits.append(f"{result['dropped']} dropped")

    head = " · ".join(bits)
    where = "added to Notion" if result["pushed"] else "on the board"
    first = tasks[0]["task"] if tasks else ""
    return f"{head} {where}" + (f": {first}" if len(tasks) == 1 and first else "")


def run(text: str, *, conn: sqlite3.Connection | None = None, push: bool = True,
        when: datetime | None = None, inbox: Path | None = None,
        extract_fn=None) -> dict:
    """Capture text, extract tasks from it, and send them to Notion.

    Returns what happened, including the tasks themselves, so the caller can say
    so without querying anything.
    """
    path = write_note(text, when=when, inbox=inbox)

    own_conn = conn is None
    conn = conn or db.connect()
    try:
        ingested = episodes.ingest_inbox(conn, inbox)
        episode = _episode_for(conn, path)
        if episode is None:
            return {"error": "the captured note did not reach the database",
                    "file": str(path), "tasks": [], "merged": 0, "dropped": 0,
                    "pushed": 0, "problems": ingested["problems"]}

        outcome = sync.extract_episode(conn, episode, extract_fn=extract_fn)
        if outcome.get("error"):
            # The note stays in the inbox, so the next sync retries it: a
            # provider outage should cost you the notification, not the thought.
            return {"error": outcome["error"], "file": str(path), "tasks": [],
                    "merged": 0, "dropped": 0, "pushed": 0,
                    "episode_id": int(episode["id"])}

        ids = set(outcome["commitment_ids"])
        tasks = [t for t in db.all_tasks(conn) if t["id"] in ids]

        result = {
            "error": None,
            "file": str(path),
            "episode_id": int(episode["id"]),
            "title": episode["title"],
            "tasks": tasks,
            "merged": outcome["merged"],
            "dropped": outcome["dropped"],
            "drops": outcome["drops"],
            "pushed": 0,
            "push_error": None,
            "usage": outcome.get("usage"),
        }

        if push and tasks:
            result.update(_push(conn, ids))
        return result
    finally:
        if own_conn:
            conn.close()


def _push(conn: sqlite3.Connection, ids: set[int]) -> dict:
    """Send just these commitments to Notion.

    A Notion failure is reported, never raised: the task is already extracted and
    on the local board, and the next `push` will carry it out. Losing the capture
    over a network blip would be the worst possible trade.
    """
    from .connectors import notion

    try:
        stats = notion.push(conn, only=ids)
    except notion.NotionError as exc:
        return {"pushed": 0, "push_error": str(exc)}
    return {"pushed": stats["created"] + stats["updated"],
            "push_error": stats["errors"][0] if stats["errors"] else None}


def _episode_for(conn: sqlite3.Connection, path: Path) -> sqlite3.Row | None:
    """The episode this file produced, found by the raw path it was ingested from."""
    wanted = str(Path(path).resolve())
    row = conn.execute(
        """SELECT e.* FROM episodes e
             JOIN episode_events le ON le.episode_id = e.id
             JOIN events ev ON ev.id = le.event_id
            WHERE ev.raw_path = ?
         ORDER BY e.id DESC LIMIT 1""",
        (wanted,),
    ).fetchone()
    if row is not None:
        return row
    # Fall back to the id we wrote, in case the path was stored unresolved.
    return conn.execute(
        "SELECT * FROM episodes WHERE external_id = ? ORDER BY id DESC LIMIT 1",
        (f"capture-{re.sub(r'[^0-9-]', '', path.stem.split('capture-')[-1])}",),
    ).fetchone()
