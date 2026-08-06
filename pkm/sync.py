"""The one command: inbox ingest -> extraction -> dedup -> board rows."""

from __future__ import annotations

import json
import sqlite3

from . import config, db, dedup, episodes, extract, similar


def apply_extraction(
    conn: sqlite3.Connection,
    episode: sqlite3.Row | dict,
    kept: list[dict],
    dropped: list[dict],
) -> dict:
    """Store validated commitments, merging restatements (D4).

    Clears this episode's own commitments first, so re-extracting a changed
    transcript never leaves orphans and never double-counts mentions.
    """
    episode_id = int(episode["id"])
    occurred_at = episode["started_at"]
    event_id = episodes.primary_event_id(conn, episode_id)

    result = {"created": 0, "merged": 0, "dropped": len(dropped), "created_ids": []}

    db.clear_commitments_for_episode(conn, episode_id)

    for item in kept:
        candidates = db.open_commitments_for_owner(conn, item["owner_norm"])
        # Lexical first; the judge only sees pairs that are plausible and did not
        # match on wording (§11). It cannot block: on any failure this is the
        # lexical answer.
        match, score, _how = similar.resolve(item, candidates)

        if match is not None:
            # Same owner, near-identical text, still open: this is the same
            # promise made again. Append a mention, do not create a second row.
            db.add_mention(
                conn,
                int(match["id"]),
                episode_id,
                occurred_at,
                item["quote"],
                item["speaker"],
            )
            db.touch_commitment_due(conn, int(match["id"]), item["due_date"])
            result["merged"] += 1
            continue

        result["created_ids"].append(db.insert_commitment(
            conn,
            {
                **item,
                "episode_id": episode_id,
                "event_id": event_id,
                "occurred_at": occurred_at,
            },
        ))
        result["created"] += 1

    for item in dropped:
        db.record_drop(conn, episode_id, item.get("_reason", "unknown"), item)

    return result


def extract_episode(conn: sqlite3.Connection, episode, *, extract_fn=None) -> dict:
    """Extract one episode and store the result.

    On failure the episode is deliberately left unextracted — `extracted_at`
    stays NULL — so the next sync retries it. Overloaded endpoints fail often
    enough that losing a meeting to one bad response is not acceptable.
    """
    run = extract_fn or (lambda ep: extract.extract(ep))
    label = episode["title"] or f"episode {episode['id']}"

    try:
        outcome = run(episode)
    except Exception as exc:
        return {"error": f"{label}: {exc}", "created": 0, "merged": 0,
                "dropped": 0, "drops": [], "commitment_ids": []}

    with db.transaction(conn):
        applied = apply_extraction(
            conn, episode, outcome.get("kept", []), outcome.get("dropped", [])
        )
        db.mark_extracted(
            conn,
            int(episode["id"]),
            outcome.get("usage", {}).get("model") or config.MODEL,
        )

    return {
        "error": None,
        "created": applied["created"],
        "merged": applied["merged"],
        "dropped": applied["dropped"],
        "drops": [
            {"reason": d.get("_reason", "unknown"),
             "task": d.get("task"), "quote": d.get("quote")}
            for d in outcome.get("dropped", [])
        ],
        # The ids the insert actually returned. This used to diff the set of ids
        # before and after, which silently returned nothing whenever a row had
        # just been cleared from this episode: SQLite reuses the freed rowid, so
        # "after" equalled "before" and a real capture reported zero tasks.
        "commitment_ids": applied["created_ids"],
        "usage": outcome.get("usage"),
    }


def _same_evidence(a: str | None, b: str | None) -> bool:
    """Do two quotes point at the same line of the transcript?

    Compared through the same normaliser the verbatim check uses, so a model
    that re-quotes the line with different formatting still counts as the same
    piece of evidence.
    """
    if not a or not b:
        return False
    return extract._loosen(a, markup=True) == extract._loosen(b, markup=True)


def reextract_episode(conn: sqlite3.Connection, episode, *, extract_fn=None) -> dict:
    """Run extraction again for one episode and MERGE the result.

    Deliberately not `apply_extraction`, which clears the episode's rows first.
    A refresh has to survive the work already done on the board, so:

      - manual rows are never touched;
      - rows a human edited keep their wording, and only their evidence is
        refreshed;
      - untouched extracted rows are updated in place, keeping status and
        mention history;
      - anything new is added;
      - rows the model no longer returns are kept and reported, never silently
        deleted — a worse second extraction must not lose a real commitment.
    """
    run = extract_fn or (lambda ep: extract.extract(ep))
    episode_id = int(episode["id"])
    label = episode["title"] or f"episode {episode_id}"

    # Asking for a refresh is asking for this note to be read, which un-mutes it
    # (§17). Otherwise "Refresh tasks" on a muted note would appear to do nothing.
    with db.transaction(conn):
        db.set_muted(conn, episode_id, False)

    try:
        outcome = run(episode)
    except Exception as exc:
        return {"error": f"{label}: {exc}"}

    kept = outcome.get("kept", [])
    dropped = outcome.get("dropped", [])
    occurred_at = episode["started_at"]
    event_id = episodes.primary_event_id(conn, episode_id)

    stats = {"error": None, "created": 0, "updated": 0, "merged": 0,
             "protected": 0, "unmatched": 0, "dropped": len(dropped),
             "drops": [{"reason": d.get("_reason", "unknown"), "task": d.get("task"),
                        "quote": d.get("quote")} for d in dropped],
             "commitment_ids": [], "usage": outcome.get("usage")}

    with db.transaction(conn):
        mine = [r for r in db.commitments_for_episode(conn, episode_id)
                if r["origin"] == "extracted"]
        unclaimed = {int(r["id"]): r for r in mine}

        for item in kept:
            # Identity within one meeting is the QUOTE, not the task text. The
            # quote is a verbatim line from the transcript, so it survives any
            # rewording — including a rename by a human, which would otherwise
            # drop below the dedup threshold and produce a duplicate.
            match = next(
                (r for r in unclaimed.values() if _same_evidence(r["quote"], item["quote"])),
                None,
            )
            if match is None:
                candidates = [r for r in unclaimed.values()
                              if r["owner_norm"] == item["owner_norm"]]
                match, _ = dedup.find_match(item, candidates)

            if match is not None:
                row = unclaimed.pop(int(match["id"]))
                if row["edited"]:
                    # Keep the human's wording; refresh only the evidence.
                    db.update_commitment(conn, int(row["id"]),
                                         {"quote": item["quote"], "speaker": item["speaker"]},
                                         mark_edited=False)
                    stats["protected"] += 1
                else:
                    db.update_commitment(conn, int(row["id"]), {
                        "task": item["task"], "direction": item["direction"],
                        "owner": item["owner"],
                        "due_date": item["due_date"] or row["due_date"],
                        "quote": item["quote"], "speaker": item["speaker"],
                    }, mark_edited=False)
                    stats["updated"] += 1
                continue

            # Not from this meeting: it may still be a restatement of an open
            # promise made elsewhere (D4).
            elsewhere = [r for r in db.open_commitments_for_owner(conn, item["owner_norm"])
                         if int(r["episode_id"]) != episode_id]
            match, _score, _how = similar.resolve(item, elsewhere)
            if match is not None:
                db.add_mention(conn, int(match["id"]), episode_id, occurred_at,
                               item["quote"], item["speaker"])
                db.touch_commitment_due(conn, int(match["id"]), item["due_date"])
                stats["merged"] += 1
                continue

            new_id = db.insert_commitment(conn, {
                **item, "episode_id": episode_id, "event_id": event_id,
                "occurred_at": occurred_at,
            })
            stats["created"] += 1
            stats["commitment_ids"].append(new_id)

        stats["unmatched"] = len(unclaimed)

        conn.execute("DELETE FROM extraction_drops WHERE episode_id = ?", (episode_id,))
        for item in dropped:
            db.record_drop(conn, episode_id, item.get("_reason", "unknown"), item)

        db.mark_extracted(conn, episode_id,
                          (outcome.get("usage") or {}).get("model") or config.MODEL)

    return stats


def revalidate_drops(conn: sqlite3.Connection, *, apply: bool = False) -> dict:
    """Re-check stored drops against the current checker. No model calls.

    Every rejected commitment is kept in `extraction_drops` with the model's
    original JSON, so when the verbatim-quote check improves, the ones it used
    to reject wrongly can be recovered from what is already on disk instead of
    paying for the meeting again.
    """
    result = {"checked": 0, "recovered": [], "still_dropped": 0, "created": 0, "merged": 0}

    rows = conn.execute(
        """SELECT d.id, d.payload, d.reason, e.id AS episode_id, e.title, e.started_at,
                  e.transcript, e.kind
             FROM extraction_drops d
             JOIN episodes e ON e.id = d.episode_id
         ORDER BY d.id"""
    ).fetchall()

    for row in rows:
        result["checked"] += 1
        try:
            payload = json.loads(row["payload"])
        except json.JSONDecodeError:
            result["still_dropped"] += 1
            continue

        # `kind` rides along so a capture's drops are re-checked against the
        # thresholds that were applied to them (§10, D14), not a meeting's.
        episode = {"transcript": row["transcript"], "started_at": row["started_at"],
                   "kind": row["kind"]}
        kept, _ = extract.validate({"commitments": [payload]}, episode)
        if not kept:
            result["still_dropped"] += 1
            continue

        item = kept[0]
        result["recovered"].append({
            "task": item["task"],
            "owner": item["owner"],
            "meeting": row["title"],
            "matched": item["quote_match"],
            "was": row["reason"],
        })

        if not apply:
            continue

        with db.transaction(conn):
            candidates = db.open_commitments_for_owner(conn, item["owner_norm"])
            match, _score = dedup.find_match(item, candidates)
            if match is not None:
                db.add_mention(conn, int(match["id"]), int(row["episode_id"]),
                               row["started_at"], item["quote"], item["speaker"])
                db.touch_commitment_due(conn, int(match["id"]), item["due_date"])
                result["merged"] += 1
            else:
                db.insert_commitment(conn, {
                    **item,
                    "episode_id": int(row["episode_id"]),
                    "event_id": episodes.primary_event_id(conn, int(row["episode_id"])),
                    "occurred_at": row["started_at"],
                })
                result["created"] += 1
            conn.execute("DELETE FROM extraction_drops WHERE id = ?", (row["id"],))

    return result


def run_sync(conn: sqlite3.Connection, *, extract_fn=None, inbox_path=None, verbose=False) -> dict:
    """Ingest the inbox, then extract every episode that needs it.

    `extract_fn(episode) -> {"kept": [...], "dropped": [...]}` is injectable so
    the pipeline can be exercised without hitting the API.
    """
    run = extract_fn or (lambda ep: extract.extract(ep))

    stats = {
        "files": 0,
        "events_new": 0,
        "episodes_new": 0,
        "extracted": 0,
        "refreshed": 0,
        "created": 0,
        "updated": 0,
        "merged": 0,
        "protected": 0,
        "unmatched": 0,
        "dropped": 0,
        "errors": [],
        "problems": [],
        "usage": [],
    }

    ingested = episodes.ingest_inbox(conn, inbox_path)
    stats.update(
        files=ingested["files"],
        events_new=ingested["events_new"],
        episodes_new=ingested["episodes_new"],
        problems=ingested["problems"],
    )

    for episode in db.episodes_needing_extraction(conn):
        label = episode["title"] or f"episode {episode['id']}"

        # An episode that has never been extracted gets the clearing path. One
        # whose transcript CHANGED is a refresh, and refresh must not undo work
        # already done on the board: `apply_extraction` would delete this
        # episode's rows and re-insert them with new ids, which drops the Notion
        # page id off every one and makes the next push create duplicates.
        # `reextract_episode` merges in place instead.
        #
        # This matters the moment sync runs unattended, because a transcript
        # legitimately changes after the fact: Wispr's refine pass rewrites it
        # when the summary is generated.
        first_time = episode["extracted_at"] is None
        outcome = (extract_episode if first_time else reextract_episode)(
            conn, episode, extract_fn=run
        )

        if outcome["error"]:
            stats["errors"].append(outcome["error"])
            if verbose:
                print(f"  {outcome['error']}")
            continue

        stats["extracted" if first_time else "refreshed"] += 1
        for key in ("created", "updated", "merged", "protected", "unmatched", "dropped"):
            stats[key] += outcome.get(key, 0)
        if outcome.get("usage"):
            stats["usage"].append(outcome["usage"])

        if verbose:
            if first_time:
                print(f"  {label}: +{outcome['created']} new, "
                      f"{outcome['merged']} merged, {outcome['dropped']} dropped")
            else:
                print(f"  {label}: refreshed -> +{outcome['created']} new, "
                      f"{outcome.get('updated', 0)} updated in place, "
                      f"{outcome.get('protected', 0)} kept as edited, "
                      f"{outcome.get('unmatched', 0)} no longer returned, "
                      f"{outcome['dropped']} dropped")

    stats["tasks"] = conn.execute("SELECT COUNT(*) AS n FROM commitments").fetchone()["n"]
    return stats
