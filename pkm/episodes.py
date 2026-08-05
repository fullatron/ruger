"""Grouping raw events into episodes — the unit extraction runs over.

A meeting is one event and one episode. This module exists as its own layer
because Slack will not be: many message events group into one channel-day
episode, and email into one thread episode. That grouping is the only thing
that changes when a connector is added (D1).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import db
from .connectors import inbox


def ingest_inbox(conn: sqlite3.Connection, inbox_path: Path | None = None) -> dict:
    """Read the inbox, write events, group them into episodes.

    Idempotent: running twice over unchanged files adds zero rows (§7, step 1).
    """
    records, problems = inbox.scan(inbox_path)

    stats = {
        "files": len(records),
        "events_new": 0,
        "events_updated": 0,
        "episodes_new": 0,
        "episodes_changed": 0,
        "problems": problems,
        "episode_ids": [],
    }

    with db.transaction(conn):
        for record in records:
            existing = conn.execute(
                "SELECT id FROM events WHERE source = ? AND external_id = ?",
                (record["source"], record["external_id"]),
            ).fetchone()

            event_id, changed = db.upsert_event(conn, record)
            if existing is None:
                stats["events_new"] += 1
            elif changed:
                stats["events_updated"] += 1

            episode_id, ep_changed = _episode_for_meeting(conn, record, event_id)
            if ep_changed:
                is_new = conn.execute(
                    "SELECT extracted_at FROM episodes WHERE id = ?", (episode_id,)
                ).fetchone()["extracted_at"] is None
                stats["episodes_new" if is_new else "episodes_changed"] += 1
            stats["episode_ids"].append(episode_id)

    return stats


def _episode_for_meeting(
    conn: sqlite3.Connection, record: dict, event_id: int
) -> tuple[int, bool]:
    """One meeting export -> one episode, 1:1."""
    episode = {
        "source": record["source"],
        "external_id": record["external_id"],
        "kind": "meeting" if record["source"] == "meeting" else record["source"],
        "title": record["title"],
        "started_at": record["occurred_at"],
        "ended_at": None,
        "participants": record.get("participants") or [],
        # The transcript extraction reads, and the text the verbatim-quote check
        # validates against, are the same bytes on purpose.
        "transcript": record["body"],
    }
    return db.upsert_episode(conn, episode, [event_id])


def episode_events(conn: sqlite3.Connection, episode_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT e.* FROM events e
             JOIN episode_events le ON le.event_id = e.id
            WHERE le.episode_id = ?
         ORDER BY e.occurred_at, e.id""",
        (episode_id,),
    ).fetchall()


def primary_event_id(conn: sqlite3.Connection, episode_id: int) -> int | None:
    row = conn.execute(
        """SELECT event_id FROM episode_events
            WHERE episode_id = ? ORDER BY event_id LIMIT 1""",
        (episode_id,),
    ).fetchone()
    return int(row["event_id"]) if row else None
