"""Adding notes from the UI.

A pasted note is written to `~/.pkm/inbox` as a markdown file *first*, then
ingested through the ordinary connector. That keeps D2 intact — there is still
exactly one ingest path, files remain the durable artifact, and the database
stays a derived thing you can delete and rebuild.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from datetime import date
from pathlib import Path

from . import config, db, episodes, sync
from .connectors import inbox

MAX_BODY_CHARS = 400_000


class NoteError(Exception):
    pass


def slugify(text: str, fallback: str = "note") -> str:
    """Filesystem-safe stem, in the title's own script.

    The old version encoded to ASCII and dropped what did not survive, so every
    non-Latin title became the bare fallback — and two Hindi meetings on one day
    collided on a single filename. Letters and marks are kept; separators, dots
    and control characters are not, which is the part that made it safe.
    """
    text = unicodedata.normalize("NFC", text or "").lower()
    kept = "".join(
        ch if (ch.isalnum() or unicodedata.category(ch).startswith("M")) else "-"
        for ch in text
    )
    kept = re.sub(r"-{2,}", "-", kept).strip("-")
    return (kept[:60].strip("-") or fallback)


def _unique_path(root: Path, stem: str) -> Path:
    candidate = root / f"{stem}.md"
    n = 2
    while candidate.exists():
        candidate = root / f"{stem}-{n}.md"
        n += 1
    return candidate


def _frontmatter_value(value: str) -> str:
    """Keep a one-line scalar from breaking the frontmatter block."""
    return " ".join(str(value or "").split()).replace('"', "'")


def save_note(
    title: str,
    body: str,
    when: str | None = None,
    participants: list[str] | None = None,
    inbox_path: Path | None = None,
) -> Path:
    """Write a pasted note into the inbox. Returns the file path."""
    body = (body or "").strip()
    if not body:
        raise NoteError("the note is empty")
    if len(body) > MAX_BODY_CHARS:
        raise NoteError(f"note is too large ({len(body):,} characters)")

    title = " ".join((title or "").split())
    when = (when or "").strip()
    parsed_date = inbox.parse_date(when) if when else None
    if when and not parsed_date:
        raise NoteError(f"could not read the date {when!r} — use YYYY-MM-DD")
    parsed_date = parsed_date or date.today().isoformat()

    if not title:
        # Fall back to the first heading or the first line of the note.
        first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
        title = " ".join(first.lstrip("#").split())[:80] or f"Note {parsed_date}"

    root = Path(inbox_path or config.INBOX)
    root.mkdir(parents=True, exist_ok=True)
    path = _unique_path(root, f"{parsed_date}-{slugify(title)}")

    people = [p.strip() for p in (participants or []) if str(p).strip()]
    lines = [
        "---",
        f'title: "{_frontmatter_value(title)}"',
        f"date: {parsed_date}",
    ]
    if people:
        lines.append("participants: [" + ", ".join(_frontmatter_value(p) for p in people) + "]")
    lines += ["---", "", body, ""]

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def save_upload(filename: str, content: str, inbox_path: Path | None = None) -> Path:
    """Store an uploaded markdown file under its own (sanitised) name."""
    content = (content or "").strip()
    if not content:
        raise NoteError(f"{filename}: file is empty")
    if len(content) > MAX_BODY_CHARS:
        raise NoteError(f"{filename}: too large ({len(content):,} characters)")

    root = Path(inbox_path or config.INBOX)
    root.mkdir(parents=True, exist_ok=True)
    stem = slugify(Path(filename or "note").stem, fallback="upload")
    path = _unique_path(root, stem)
    # Written verbatim: the verbatim-quote check runs against these bytes.
    path.write_text(content if content.endswith("\n") else content + "\n", encoding="utf-8")
    return path


def ingest_paths(
    conn: sqlite3.Connection,
    paths: list[Path],
    *,
    extract_fn=None,
    inbox_path: Path | None = None,
) -> dict:
    """Ingest the whole inbox, then extract only the episodes these files created.

    Ingest is cheap and idempotent, so re-scanning costs nothing; extraction is
    the part worth scoping, since it is one model call per meeting.
    """
    ingested = episodes.ingest_inbox(conn, inbox_path)

    wanted = {str(Path(p).resolve()) for p in paths}
    targets = [
        row
        for row in db.episodes_needing_extraction(conn)
        if _episode_from_paths(conn, int(row["id"]), wanted)
    ]

    result = {
        "files": len(paths),
        "problems": ingested["problems"],
        "episodes": [],
        "created": 0,
        "merged": 0,
        "dropped": 0,
        # Aggregated across episodes so the board can flash exactly what is new.
        "commitment_ids": [],
        "errors": [],
        "usage": [],
    }

    for episode in targets:
        outcome = sync.extract_episode(conn, episode, extract_fn=extract_fn)
        if outcome.get("error"):
            result["errors"].append(outcome["error"])
            continue
        result["created"] += outcome["created"]
        result["merged"] += outcome["merged"]
        result["dropped"] += outcome["dropped"]
        result["commitment_ids"].extend(outcome["commitment_ids"])
        if outcome.get("usage"):
            result["usage"].append(outcome["usage"])
        result["episodes"].append(
            {
                "id": int(episode["id"]),
                "title": episode["title"],
                "date": episode["started_at"],
                "created": outcome["created"],
                "merged": outcome["merged"],
                "dropped": outcome["dropped"],
                "drops": outcome["drops"],
                "commitment_ids": outcome["commitment_ids"],
            }
        )

    return result


def _episode_from_paths(conn: sqlite3.Connection, episode_id: int, wanted: set[str]) -> bool:
    for event in episodes.episode_events(conn, episode_id):
        raw = event["raw_path"]
        if raw and str(Path(raw).resolve()) in wanted:
            return True
    return False


def list_notes(conn: sqlite3.Connection) -> list[dict]:
    """Everything stored, with what each meeting produced."""
    rows = conn.execute(
        """SELECT e.id, e.title, e.started_at, e.source, e.extracted_at, e.muted,
                  e.extraction_model, LENGTH(e.transcript) AS chars,
                  (SELECT COUNT(*) FROM commitments c WHERE c.episode_id = e.id) AS commitments,
                  (SELECT COUNT(*) FROM extraction_drops d WHERE d.episode_id = e.id) AS drops,
                  (SELECT ev.raw_path FROM episode_events le
                     JOIN events ev ON ev.id = le.event_id
                    WHERE le.episode_id = e.id ORDER BY ev.id LIMIT 1) AS raw_path
             FROM episodes e
         ORDER BY e.started_at DESC, e.id DESC"""
    ).fetchall()
    return [dict(r) for r in rows]


def note_detail(conn: sqlite3.Connection, episode_id: int) -> dict:
    """One source: its exact stored transcript, its tasks, and what was dropped."""
    row = conn.execute(
        """SELECT e.*, (SELECT ev.raw_path FROM episode_events le
                          JOIN events ev ON ev.id = le.event_id
                         WHERE le.episode_id = e.id ORDER BY ev.id LIMIT 1) AS raw_path
             FROM episodes e WHERE e.id = ?""",
        (episode_id,),
    ).fetchone()
    if row is None:
        raise NoteError("no such note")

    note = dict(row)
    try:
        note["participants"] = json.loads(note.get("participants") or "[]")
    except (TypeError, json.JSONDecodeError):
        note["participants"] = []

    tasks = [t for t in db.all_tasks(conn) if t["episode_id"] == episode_id]
    drops = [
        {"reason": d["reason"], **_drop_payload(d["payload"])}
        for d in conn.execute(
            "SELECT reason, payload FROM extraction_drops WHERE episode_id = ? ORDER BY id",
            (episode_id,),
        ).fetchall()
    ]
    return {"note": note, "tasks": tasks, "drops": drops}


def _drop_payload(raw: str) -> dict:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {"task": None, "quote": None}
    return {"task": payload.get("task"), "quote": payload.get("quote")}


def add_task(conn: sqlite3.Connection, episode_id: int, payload: dict) -> int:
    """Create a task by hand, attached to a meeting.

    §8 keeps this out of v0, but controlling the output means being able to add
    what extraction missed. A manual row carries no quote: nobody said it, and
    pretending otherwise would undermine the evidence rule (D5).
    """
    from . import dedup, episodes as ep_mod

    row = conn.execute(
        "SELECT id, started_at FROM episodes WHERE id = ?", (episode_id,)
    ).fetchone()
    if row is None:
        raise NoteError("no such note")

    task = " ".join(str(payload.get("task") or "").split())
    if not task:
        raise NoteError("the task cannot be empty")
    if len(task) > 500:
        raise NoteError("that task is too long")

    direction = str(payload.get("direction") or "mine").strip().lower()
    if direction not in ("mine", "theirs"):
        raise NoteError("direction must be mine or theirs")

    owner = "me" if direction == "mine" else " ".join(
        str(payload.get("owner") or "").split())
    if not owner:
        raise NoteError("name who owns it, or set direction to mine")

    due = str(payload.get("due_date") or "").strip() or None
    if due:
        due = inbox.parse_date(due)
        if not due:
            raise NoteError("could not read that due date")

    status = str(payload.get("status") or "todo").strip()
    if status not in ("todo", "doing", "done"):
        raise NoteError("status must be todo, doing or done")

    with db.transaction(conn):
        return db.insert_manual_commitment(conn, {
            "episode_id": episode_id,
            "event_id": ep_mod.primary_event_id(conn, episode_id),
            "task": task,
            "task_norm": dedup.normalise_text(task),
            "direction": direction,
            "owner": owner,
            "owner_norm": dedup.normalise_owner(owner, direction),
            "due_date": due,
            "status": status,
            "occurred_at": row["started_at"],
        })


def delete_tasks(conn: sqlite3.Connection, episode_id: int, *,
                 mute: bool = True, archive: bool = True) -> dict:
    """Drop every task a note produced, keeping the note (§17).

    For the case this exists to serve: a note that was never about your work.
    Feedback on somebody else's product reads exactly like a list of things to
    do, and one recording can put a dozen of them on your board.

    Three things have to happen together or it does not solve anything:

      - the rows go, and the log records each one;
      - **their Notion pages are archived**, or you are left deleting a dozen
        cards by hand, which is the problem you were trying to escape;
      - the note is **muted**, or the next transcript rewrite re-extracts the lot.
        Wispr rewrites one every time you press summarise.

    The note itself is kept: the recording is still worth having, and deleting it
    would take the transcript and the evidence with it.
    """
    row = conn.execute(
        "SELECT id, title FROM episodes WHERE id = ?", (episode_id,)
    ).fetchone()
    if row is None:
        raise NoteError("no such note")

    doomed = db.commitments_for_episode(conn, episode_id)
    result = {"note": row["title"], "deleted": 0, "archived": 0,
              "tasks": [r["task"] for r in doomed], "muted": bool(mute),
              "errors": []}

    for task in doomed:
        with db.transaction(conn):
            db.delete_commitment(conn, int(task["id"]))
            db.log_event(conn, "deleted", task["task"],
                         commitment_id=int(task["id"]),
                         detail=f"not my work — cleared from “{row['title']}”",
                         external_url=task["external_url"])
        result["deleted"] += 1

    if mute:
        with db.transaction(conn):
            db.set_muted(conn, episode_id, True)

    if archive:
        from .connectors import notion

        for task in doomed:
            if not task["external_id"]:
                continue
            try:
                notion._request("PATCH", f"/pages/{task['external_id']}",
                                {"archived": True})
                result["archived"] += 1
            except notion.NotionError as exc:
                # One page refusing must not strand the rest, and the row is
                # already gone here: `pull --prune` will catch the remainder.
                result["errors"].append(f"{task['task'][:40]}: {exc}")

    return result


def delete_note(conn: sqlite3.Connection, episode_id: int) -> dict:
    """Remove a note: its rows go, its file moves to the trash folder.

    The file is moved rather than unlinked — a bad paste should be recoverable,
    and leaving it in the inbox would just re-ingest on the next sync.
    """
    row = conn.execute(
        "SELECT id, title FROM episodes WHERE id = ?", (episode_id,)
    ).fetchone()
    if row is None:
        raise NoteError("no such note")

    paths = [
        Path(e["raw_path"])
        for e in episodes.episode_events(conn, episode_id)
        if e["raw_path"]
    ]

    with db.transaction(conn):
        db.clear_commitments_for_episode(conn, episode_id)
        conn.execute("DELETE FROM extraction_drops WHERE episode_id = ?", (episode_id,))
        # episode_events and events cascade from the episode row.
        event_ids = [
            int(e["id"]) for e in episodes.episode_events(conn, episode_id)
        ]
        conn.execute("DELETE FROM episodes WHERE id = ?", (episode_id,))
        for event_id in event_ids:
            conn.execute("DELETE FROM events WHERE id = ?", (event_id,))

    moved = []
    trash = Path(config.TRASH)
    for path in paths:
        try:
            if path.exists():
                trash.mkdir(parents=True, exist_ok=True)
                target = _unique_path(trash, path.stem)
                path.replace(target)
                moved.append(str(target))
        except OSError as exc:
            moved.append(f"could not move {path.name}: {exc}")

    return {"deleted": row["title"], "moved_to_trash": moved}
