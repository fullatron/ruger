"""SQLite access. Board state lives here, not in the browser (D6)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

from . import config


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Open a connection with the schema applied. `:memory:` works too."""
    target = config.DB_PATH if path is None else path
    if target != ":memory:":
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    return conn


# Columns added after the first release. `CREATE TABLE IF NOT EXISTS` does
# nothing for an existing table, so new columns need an explicit ALTER or a
# database with real data in it breaks on the next query.
_ADDED_COLUMNS = {
    "commitments": [
        ("origin", "TEXT NOT NULL DEFAULT 'extracted'"),
        ("edited", "INTEGER NOT NULL DEFAULT 0"),
        ("external_id", "TEXT"),
        ("external_url", "TEXT"),
        ("pushed_at", "TEXT"),
    ],
}


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(config.SCHEMA_PATH.read_text())
    for table, columns in _ADDED_COLUMNS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, spec in columns:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {spec}")
    _backfill_log(conn)


def _backfill_log(conn: sqlite3.Connection) -> None:
    """Give the log a past (§12).

    Commitments pushed before this table existed carry a `pushed_at` and a page
    id but no event, so the log would open empty on a board with a dozen cards
    already in Notion — reading as "nothing has been sent" when everything has.

    Runs once: after it, the table is no longer empty.
    """
    if conn.execute("SELECT 1 FROM sync_events LIMIT 1").fetchone():
        return
    rows = conn.execute(
        """SELECT id, task, pushed_at, external_url, status FROM commitments
            WHERE external_id IS NOT NULL AND pushed_at IS NOT NULL
         ORDER BY pushed_at"""
    ).fetchall()
    for row in rows:
        conn.execute(
            """INSERT INTO sync_events (commitment_id, action, task, detail,
                                        external_url, at)
               VALUES (?, 'created', ?, ?, ?, ?)""",
            (int(row["id"]), row["task"], "sent before the log existed",
             row["external_url"], row["pushed_at"]),
        )


@contextmanager
def transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# --- events -----------------------------------------------------------------


def upsert_event(conn: sqlite3.Connection, ev: dict) -> tuple[int, bool]:
    """Insert or update one raw item.

    Returns (event_id, changed). `changed` is False when an identical row was
    already there — that is what makes a second sync over the same files add
    zero rows (§7).
    """
    h = content_hash(ev["body"])
    row = conn.execute(
        "SELECT id, content_hash FROM events WHERE source = ? AND external_id = ?",
        (ev["source"], ev["external_id"]),
    ).fetchone()

    if row is None:
        cur = conn.execute(
            """INSERT INTO events
                 (source, external_id, occurred_at, actor, title, body,
                  raw_path, content_hash, ingested_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ev["source"],
                ev["external_id"],
                ev["occurred_at"],
                ev.get("actor"),
                ev.get("title"),
                ev["body"],
                ev.get("raw_path"),
                h,
                now(),
            ),
        )
        return int(cur.lastrowid), True

    if row["content_hash"] == h:
        return int(row["id"]), False

    conn.execute(
        """UPDATE events
              SET occurred_at = ?, actor = ?, title = ?, body = ?,
                  raw_path = ?, content_hash = ?, ingested_at = ?
            WHERE id = ?""",
        (
            ev["occurred_at"],
            ev.get("actor"),
            ev.get("title"),
            ev["body"],
            ev.get("raw_path"),
            h,
            now(),
            row["id"],
        ),
    )
    return int(row["id"]), True


# --- episodes ---------------------------------------------------------------


def upsert_episode(conn: sqlite3.Connection, ep: dict, event_ids: list[int]) -> tuple[int, bool]:
    """Insert or update an episode and its event links."""
    h = content_hash(ep["transcript"])
    row = conn.execute(
        "SELECT id, content_hash FROM episodes WHERE source = ? AND external_id = ?",
        (ep["source"], ep["external_id"]),
    ).fetchone()

    participants = json.dumps(ep.get("participants") or [])

    if row is None:
        cur = conn.execute(
            """INSERT INTO episodes
                 (source, external_id, kind, title, started_at, ended_at,
                  participants, transcript, content_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ep["source"],
                ep["external_id"],
                ep["kind"],
                ep.get("title"),
                ep["started_at"],
                ep.get("ended_at"),
                participants,
                ep["transcript"],
                h,
            ),
        )
        episode_id, changed = int(cur.lastrowid), True
    else:
        episode_id = int(row["id"])
        changed = row["content_hash"] != h
        if changed:
            conn.execute(
                """UPDATE episodes
                      SET kind = ?, title = ?, started_at = ?, ended_at = ?,
                          participants = ?, transcript = ?, content_hash = ?
                    WHERE id = ?""",
                (
                    ep["kind"],
                    ep.get("title"),
                    ep["started_at"],
                    ep.get("ended_at"),
                    participants,
                    ep["transcript"],
                    h,
                    episode_id,
                ),
            )

    for event_id in event_ids:
        conn.execute(
            "INSERT OR IGNORE INTO episode_events (episode_id, event_id) VALUES (?, ?)",
            (episode_id, event_id),
        )
    return episode_id, changed


def episodes_needing_extraction(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Never extracted, or the transcript changed since we last did."""
    return conn.execute(
        """SELECT * FROM episodes
            WHERE extracted_at IS NULL OR extracted_hash IS NOT content_hash
            ORDER BY started_at""",
    ).fetchall()


def mark_extracted(conn: sqlite3.Connection, episode_id: int, model: str) -> None:
    conn.execute(
        """UPDATE episodes
              SET extracted_at = ?, extraction_model = ?, extracted_hash = content_hash
            WHERE id = ?""",
        (now(), model, episode_id),
    )


# --- commitments ------------------------------------------------------------


def open_commitments_for_owner(conn: sqlite3.Connection, owner_norm: str) -> list[sqlite3.Row]:
    """D4 candidates: same owner, status='open' rows only (todo + doing)."""
    return conn.execute(
        """SELECT * FROM commitments
            WHERE owner_norm = ? AND status IN ('todo', 'doing')""",
        (owner_norm,),
    ).fetchall()


def insert_commitment(conn: sqlite3.Connection, c: dict) -> int:
    ts = now()
    cur = conn.execute(
        """INSERT INTO commitments
             (episode_id, event_id, task, task_norm, direction, owner, owner_norm,
              due_date, quote, speaker, status, mention_count, mentions,
              created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'todo', 1, ?, ?, ?)""",
        (
            c["episode_id"],
            c.get("event_id"),
            c["task"],
            c["task_norm"],
            c["direction"],
            c["owner"],
            c["owner_norm"],
            c.get("due_date"),
            c["quote"],
            c.get("speaker"),
            json.dumps([c["occurred_at"]]),
            ts,
            ts,
        ),
    )
    commitment_id = int(cur.lastrowid)
    add_mention(conn, commitment_id, c["episode_id"], c["occurred_at"], c["quote"], c.get("speaker"))
    return commitment_id


def add_mention(
    conn: sqlite3.Connection,
    commitment_id: int,
    episode_id: int,
    occurred_at: str,
    quote: str,
    speaker: str | None,
) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO commitment_mentions
             (commitment_id, episode_id, occurred_at, quote, speaker, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (commitment_id, episode_id, occurred_at, quote, speaker, now()),
    )
    resync_mentions(conn, commitment_id)


def resync_mentions(conn: sqlite3.Connection, commitment_id: int) -> int:
    """Recompute mentions/mention_count from commitment_mentions (D4).

    One row per episode that raised it — a task repeated in two meetings reads
    2x, not 2x per quote.
    """
    dates = [
        r["occurred_at"]
        for r in conn.execute(
            """SELECT MIN(occurred_at) AS occurred_at
                 FROM commitment_mentions
                WHERE commitment_id = ?
             GROUP BY episode_id
             ORDER BY occurred_at""",
            (commitment_id,),
        ).fetchall()
    ]
    conn.execute(
        "UPDATE commitments SET mentions = ?, mention_count = ?, updated_at = ? WHERE id = ?",
        (json.dumps(dates), max(len(dates), 1), now(), commitment_id),
    )
    return len(dates)


def touch_commitment_due(conn: sqlite3.Connection, commitment_id: int, due_date: str | None) -> None:
    """Fill in a due date a later meeting supplied. Never clears an existing one."""
    if not due_date:
        return
    conn.execute(
        "UPDATE commitments SET due_date = ?, updated_at = ? WHERE id = ? AND due_date IS NULL",
        (due_date, now(), commitment_id),
    )


def commitments_for_episode(conn: sqlite3.Connection, episode_id: int) -> list[sqlite3.Row]:
    """Every commitment this episode produced, any status."""
    return conn.execute(
        "SELECT * FROM commitments WHERE episode_id = ? ORDER BY id", (episode_id,)
    ).fetchall()


def get_commitment(conn: sqlite3.Connection, commitment_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM commitments WHERE id = ?", (commitment_id,)
    ).fetchone()


# Fields a person may edit, and how to normalise each one.
EDITABLE_FIELDS = ("task", "direction", "owner", "due_date", "quote", "speaker")


def update_commitment(conn: sqlite3.Connection, commitment_id: int, fields: dict,
                      *, mark_edited: bool = True) -> bool:
    """Patch content fields. Recomputes the dedup keys that depend on them.

    `mark_edited` flags the row so re-extraction stops overwriting the wording;
    refresh passes False when it is the one doing the writing.
    """
    from . import dedup

    row = get_commitment(conn, commitment_id)
    if row is None:
        return False

    updates: dict[str, object] = {}
    for key in EDITABLE_FIELDS:
        if key in fields:
            value = fields[key]
            updates[key] = value.strip() if isinstance(value, str) else value

    if not updates:
        return False

    # task_norm and owner_norm are derived, so they must move together with
    # the fields they come from or dedup silently stops matching.
    if "task" in updates:
        updates["task_norm"] = dedup.normalise_text(str(updates["task"]))
    direction = str(updates.get("direction", row["direction"]))
    if "owner" in updates or "direction" in updates:
        owner = str(updates.get("owner", row["owner"]))
        if direction == "mine":
            owner = "me"
            updates["owner"] = owner
        updates["owner_norm"] = dedup.normalise_owner(owner, direction)
    # Only touch due_date when the caller actually sent it. `updates.get(...)`
    # here would write NULL on every patch that never mentioned it, silently
    # clearing a date the person had set.
    if "due_date" in updates and not updates["due_date"]:
        updates["due_date"] = None

    if mark_edited:
        updates["edited"] = 1
    updates["updated_at"] = now()

    assignments = ", ".join(f"{k} = ?" for k in updates)
    conn.execute(
        f"UPDATE commitments SET {assignments} WHERE id = ?",
        (*updates.values(), commitment_id),
    )
    return True


def delete_commitment(conn: sqlite3.Connection, commitment_id: int) -> bool:
    cur = conn.execute("DELETE FROM commitments WHERE id = ?", (commitment_id,))
    conn.execute("DELETE FROM commitment_mentions WHERE commitment_id = ?", (commitment_id,))
    return cur.rowcount > 0


def insert_manual_commitment(conn: sqlite3.Connection, c: dict) -> int:
    """A task a person added by hand. It has no quote, because nobody said it.

    D5 puts evidence on every extracted card; a manual row is explicitly not
    extracted, and `origin` records that so the card can say so rather than
    showing an empty quote.
    """
    ts = now()
    cur = conn.execute(
        """INSERT INTO commitments
             (episode_id, event_id, task, task_norm, direction, owner, owner_norm,
              due_date, quote, speaker, status, mention_count, mentions,
              origin, edited, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', NULL, ?, 1, ?, 'manual', 0, ?, ?)""",
        (
            c["episode_id"],
            c.get("event_id"),
            c["task"],
            c["task_norm"],
            c["direction"],
            c["owner"],
            c["owner_norm"],
            c.get("due_date"),
            c.get("status", "todo"),
            json.dumps([c["occurred_at"]]) if c.get("occurred_at") else json.dumps([]),
            ts,
            ts,
        ),
    )
    return int(cur.lastrowid)


def set_status(conn: sqlite3.Connection, commitment_id: int, status: str) -> bool:
    if status not in ("todo", "doing", "done"):
        raise ValueError(f"bad status: {status!r}")
    cur = conn.execute(
        "UPDATE commitments SET status = ?, updated_at = ? WHERE id = ?",
        (status, now(), commitment_id),
    )
    return cur.rowcount > 0


# --- the external board (Notion) --------------------------------------------


def commitments_to_push(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every commitment, with its meeting, in a stable order for pushing.

    Everything goes out, not just the unpushed ones: a push updates content on
    pages that already exist, so an edit made here reaches the board.
    """
    return conn.execute(
        """SELECT c.*, e.title AS meeting, e.started_at AS meeting_date
             FROM commitments c
             JOIN episodes e ON e.id = c.episode_id
         ORDER BY e.started_at, c.id"""
    ).fetchall()


def mark_pushed(conn: sqlite3.Connection, commitment_id: int,
                external_id: str, external_url: str | None = None) -> None:
    """Record where a commitment landed. This is what keeps push idempotent."""
    conn.execute(
        """UPDATE commitments
              SET external_id = ?, external_url = ?, pushed_at = ?
            WHERE id = ?""",
        (external_id, external_url, now(), commitment_id),
    )


def clear_push_state(conn: sqlite3.Connection) -> int:
    """Forget every remote link, so the next push recreates the board.

    Needed when the Notion database is deleted or replaced: the stored page ids
    then point at nothing and every update would 404.
    """
    cur = conn.execute(
        """UPDATE commitments
              SET external_id = NULL, external_url = NULL, pushed_at = NULL
            WHERE external_id IS NOT NULL"""
    )
    return cur.rowcount


def record_drop(conn: sqlite3.Connection, episode_id: int, reason: str, payload: dict) -> None:
    conn.execute(
        """INSERT INTO extraction_drops (episode_id, reason, payload, created_at)
           VALUES (?, ?, ?, ?)""",
        (episode_id, reason, json.dumps(payload, ensure_ascii=False), now()),
    )


def clear_commitments_for_episode(conn: sqlite3.Connection, episode_id: int) -> None:
    """Re-extraction of a changed transcript: drop only this episode's originals.

    Mentions this episode contributed to commitments that live elsewhere are
    removed too, and those counts resynced, so re-running never inflates a pip
    count.
    """
    affected = [
        r["commitment_id"]
        for r in conn.execute(
            "SELECT DISTINCT commitment_id FROM commitment_mentions WHERE episode_id = ?",
            (episode_id,),
        ).fetchall()
    ]
    conn.execute("DELETE FROM commitments WHERE episode_id = ?", (episode_id,))
    conn.execute("DELETE FROM commitment_mentions WHERE episode_id = ?", (episode_id,))
    for cid in affected:
        still = conn.execute("SELECT 1 FROM commitments WHERE id = ?", (cid,)).fetchone()
        if still:
            resync_mentions(conn, cid)


# --- board read model -------------------------------------------------------

TASKS_QUERY = """
SELECT c.id,
       c.task,
       c.direction,
       c.owner,
       c.due_date,
       c.quote,
       c.speaker,
       c.status,
       c.mention_count,
       c.mentions,
       c.origin,
       c.edited,
       c.external_id,
       c.external_url,
       c.pushed_at,
       c.created_at,
       c.updated_at,
       e.id          AS episode_id,
       e.title       AS meeting,
       e.started_at  AS meeting_date,
       e.source      AS source
  FROM commitments c
  JOIN episodes e ON e.id = c.episode_id
 ORDER BY c.mention_count DESC,
          (c.due_date IS NULL), c.due_date,
          e.started_at DESC, c.id
"""


# --- the log (§12) ------------------------------------------------------------

ACTIONS = ("created", "resent", "status", "deleted", "merged")


def log_event(conn: sqlite3.Connection, action: str, task: str, *,
              commitment_id: int | None = None, detail: str | None = None,
              external_url: str | None = None) -> int:
    """Record something that happened with Notion.

    The board reads this rather than tracking state of its own: Notion owns a
    card once it exists, so what is worth showing locally is the history of what
    was sent and what came back.
    """
    if action not in ACTIONS:
        raise ValueError(f"unknown log action: {action!r}")
    cur = conn.execute(
        """INSERT INTO sync_events (commitment_id, action, task, detail,
                                    external_url, at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (commitment_id, action, task, detail, external_url, now()),
    )
    return int(cur.lastrowid)


def recent_events(conn: sqlite3.Connection, limit: int = 300) -> list[dict]:
    """The log, newest first, joined to whatever still exists.

    A LEFT JOIN because the row may be gone — that is the case the log exists
    for. `alive` tells the page whether there is anything left to open.
    """
    rows = conn.execute(
        """SELECT s.id, s.commitment_id, s.action, s.task, s.detail,
                  s.external_url, s.at,
                  c.id IS NOT NULL      AS alive,
                  c.status              AS status,
                  c.due_date            AS due_date,
                  c.direction           AS direction,
                  c.owner               AS owner,
                  c.quote               AS quote,
                  c.speaker             AS speaker,
                  c.origin              AS origin,
                  e.title               AS meeting,
                  e.id                  AS episode_id
             FROM sync_events s
             LEFT JOIN commitments c ON c.id = s.commitment_id
             LEFT JOIN episodes e    ON e.id = c.episode_id
         ORDER BY s.at DESC, s.id DESC
            LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def merge_commitments(conn: sqlite3.Connection, keep_id: int, drop_id: int) -> dict:
    """Fold one commitment into another. Returns what the caller has to clean up.

    Every mention moves across, so the survivor's count reflects both askers and
    the detail panel still shows who said what — a merge must not erase the
    evidence that two people wanted this (D5).

    The dropped row's Notion page id is handed back rather than acted on: this
    module does not make network calls.
    """
    if keep_id == drop_id:
        raise ValueError("cannot merge a commitment into itself")

    keep = get_commitment(conn, keep_id)
    drop = get_commitment(conn, drop_id)
    if keep is None or drop is None:
        raise ValueError("both commitments must exist")

    conn.execute(
        "UPDATE OR IGNORE commitment_mentions SET commitment_id = ? WHERE commitment_id = ?",
        (keep_id, drop_id),
    )
    # UPDATE OR IGNORE leaves behind any row that would have collided with the
    # survivor's own (episode, quote); those are the same mention twice.
    conn.execute("DELETE FROM commitment_mentions WHERE commitment_id = ?", (drop_id,))

    # An earlier due date is the one that matters, and one side may have none.
    dates = [d for d in (keep["due_date"], drop["due_date"]) if d]
    if dates:
        conn.execute("UPDATE commitments SET due_date = ? WHERE id = ?",
                     (min(dates), keep_id))

    conn.execute("DELETE FROM commitments WHERE id = ?", (drop_id,))
    resync_mentions(conn, keep_id)

    return {"keep": keep_id, "dropped": drop_id, "task": drop["task"],
            "external_id": drop["external_id"], "external_url": drop["external_url"]}


def board_summary(conn: sqlite3.Connection, today: str | None = None) -> dict:
    """Counts for `pkm status` and the menu bar item.

    `SUM(<condition>)` returns NULL over zero rows rather than 0, so every value
    is coerced — an empty board must read as zeros, not as None leaking into a
    format string.
    """
    stamp = today or date.today().isoformat()
    row = conn.execute(
        """SELECT COUNT(*)                    AS total,
                  SUM(status = 'todo')        AS todo,
                  SUM(status = 'doing')       AS doing,
                  SUM(status = 'done')        AS done,
                  SUM(direction = 'mine')     AS mine,
                  SUM(direction = 'theirs')   AS theirs,
                  SUM(external_id IS NOT NULL) AS pushed,
                  SUM(due_date IS NOT NULL AND due_date < ?
                      AND status <> 'done')   AS overdue
             FROM commitments""",
        (stamp,),
    ).fetchone()
    episodes_row = conn.execute(
        """SELECT COUNT(*) AS notes, MAX(extracted_at) AS last_extracted
             FROM episodes"""
    ).fetchone()

    out = {key: int(row[key] or 0) for key in
           ("total", "todo", "doing", "done", "mine", "theirs", "pushed", "overdue")}
    out["notes"] = int(episodes_row["notes"] or 0)
    out["last_extracted"] = episodes_row["last_extracted"]
    return out


def all_tasks(conn: sqlite3.Connection) -> list[dict]:
    """Every commitment with meeting + evidence joined (GET /api/tasks)."""
    tasks = []
    for row in conn.execute(TASKS_QUERY).fetchall():
        task = dict(row)
        task["mentions"] = json.loads(task["mentions"] or "[]")
        task["history"] = [
            dict(h)
            for h in conn.execute(
                """SELECT m.occurred_at, m.quote, m.speaker,
                          ep.title AS meeting, ep.id AS episode_id
                     FROM commitment_mentions m
                     JOIN episodes ep ON ep.id = m.episode_id
                    WHERE m.commitment_id = ?
                 ORDER BY m.occurred_at, m.id""",
                (row["id"],),
            ).fetchall()
        ]
        tasks.append(task)
    return tasks
