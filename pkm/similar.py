"""D4's LLM tie-break: is this the same job, worded differently? (§11)

Jaccard over stopworded tokens catches a restatement that reused the wording. It
cannot catch "Send Nila the KT doc" against "Share the handover sheet with Nila",
which share almost nothing after stopwording and are one errand. That gap was
recorded in the PRD with a real example, and this closes it.

Three properties this has to keep, in order:

  1. **It never blocks ingest.** A provider outage falls back to the lexical
     answer. Losing a meeting because a tie-break call timed out would be a far
     worse bug than the duplicate it was trying to prevent.
  2. **It is only asked when the answer might change something** — no lexical
     match, but overlap high enough to be plausible. Below the band, everything on
     the board would be a candidate and every extraction would cost N calls.
  3. **It merges only on a confident, unambiguous yes.** A wrong merge hides a
     real commitment inside another card; a wrong split leaves two cards, which you
     can see and fix.
"""

from __future__ import annotations

import sqlite3

from . import config, dedup, extract

PROMPT = extract.PROMPTS / "same_task.md"

# Measured, not guessed: on a real board the paraphrase pairs worth asking about
# scored 0.08 and 0.09, because two descriptions of one errand often share only the
# person's name after stopwording. A floor of 0.2 meant the judge was never
# consulted and the sweep reported "no duplicates" without having asked anything.
#
# This can be low because `resolve` asks ONE question with every candidate in it,
# not one per pair — so the floor only decides whether that single call happens at
# all, and 0.0 overlap still skips it.
AMBIGUOUS_FLOOR = 0.05

# More than this and the prompt turns into a haystack, which is where a judge
# starts guessing. Most recently raised first: an old promise is less likely to be
# what someone just repeated.
MAX_CANDIDATES = 12

SCHEMA = {
    "type": "object",
    "properties": {
        "same_as": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "confidence": {"type": "string", "enum": ["high", "low"]},
        "why": {"type": "string"},
    },
    "required": ["same_as", "confidence", "why"],
    "additionalProperties": False,
}


def enabled() -> bool:
    """Off with `PKM_FUZZY_DEDUP=0`, for a run that must cost nothing."""
    return str(config.env("PKM_FUZZY_DEDUP", "1")).strip().lower() not in (
        "0", "false", "no", "off")


def _lines(rows: list) -> str:
    out = []
    for row in rows:
        due = row["due_date"] or "no date"
        out.append(f"  id {int(row['id'])}: {row['task']}  ({row['status']}, {due})")
    return "\n".join(out)


def judge(task: str, owner: str, rows: list, *, ask=None) -> tuple[object | None, str]:
    """Which row is the same job as `task`? Returns (row, why) or (None, why).

    `ask` is injectable so the decision path can be exercised without a model.
    """
    if not rows:
        return None, "no candidates"

    shortlist = rows[:MAX_CANDIDATES]
    caller = ask or (lambda values: extract.ask_json(PROMPT, values, SCHEMA))

    try:
        answer = caller({
            "TASK": task,
            "OWNER": owner or "me",
            "CANDIDATES": _lines(shortlist),
        })
    except Exception as exc:                    # noqa: BLE001 — see property 1
        return None, f"judge unavailable: {exc}"

    if not isinstance(answer, dict):
        return None, "judge returned the wrong shape"
    if str(answer.get("confidence", "")).strip().lower() != "high":
        return None, f"low confidence: {answer.get('why') or 'no reason given'}"

    raw = answer.get("same_as")
    if raw is None:
        return None, str(answer.get("why") or "judged different")

    # The id has to be one we offered. A model naming a row that was not in the
    # shortlist is hallucinating, and merging into it would be unrecoverable.
    try:
        wanted = int(raw)
    except (TypeError, ValueError):
        return None, f"unusable id {raw!r}"

    for row in shortlist:
        if int(row["id"]) == wanted:
            return row, str(answer.get("why") or "judged the same")
    return None, f"id {wanted} was not among the candidates"


def resolve(candidate: dict, existing: list, *, ask=None) -> tuple[object | None, float, str]:
    """Lexical first, then the judge. Returns (row, score, how).

    `how` is 'lexical' | 'llm' | 'none', which is what the CLI reports so a merge
    is never a mystery.
    """
    match, score = dedup.find_match(candidate, existing)
    if match is not None:
        return match, score, "lexical"

    if not enabled() or score < AMBIGUOUS_FLOOR:
        return None, score, "none"

    ranked = sorted(existing, key=lambda r: int(r["id"]), reverse=True)
    match, _why = judge(candidate["task"], candidate.get("owner", ""), ranked, ask=ask)
    return (match, score, "llm") if match is not None else (None, score, "none")


# --- sweeping a board that already has duplicates on it ----------------------


def open_rows(conn: sqlite3.Connection) -> list:
    return conn.execute(
        """SELECT * FROM commitments
            WHERE status IN ('todo', 'doing')
         ORDER BY owner_norm, id"""
    ).fetchall()


def pairs_for_review(conn: sqlite3.Connection, floor: float = 0.0) -> list[tuple]:
    """Open rows sharing an owner, ordered so the survivor comes first.

    The survivor is the row already pushed to Notion when only one is, else the
    older one: it carries the history, and keeping it means the Notion card people
    may already have opened is the one that stays.

    `floor` defaults to 0 because this only enumerates and scores — no model call
    happens here, so filtering early would just hide pairs from the judge.
    """
    rows = open_rows(conn)

    out = []
    for i, left in enumerate(rows):
        for right in rows[i + 1:]:
            if left["owner_norm"] != right["owner_norm"]:
                continue
            score = dedup.jaccard(left["task_norm"], right["task_norm"])
            if score < floor:
                continue
            keep, drop = _order(left, right)
            out.append((keep, drop, score))
    return out


def _order(left, right) -> tuple:
    pushed_left, pushed_right = bool(left["external_id"]), bool(right["external_id"])
    if pushed_left != pushed_right:
        return (left, right) if pushed_left else (right, left)
    # A wording a human edited is the one worth keeping.
    if bool(left["edited"]) != bool(right["edited"]):
        return (left, right) if left["edited"] else (right, left)
    return (left, right) if int(left["id"]) <= int(right["id"]) else (right, left)


def review(conn: sqlite3.Connection, *, ask=None) -> list[dict]:
    """Every pair the judge calls the same job. Reads only; changes nothing.

    One question per row, with every same-owner sibling as a candidate — not one
    per pair. On a board of n rows that is n calls rather than n², and it is why
    this can afford to consider pairs with no token overlap at all, which is where
    the real paraphrases turned out to be.
    """
    rows = open_rows(conn)
    claimed: set[int] = set()
    found: list[dict] = []

    for row in rows:
        row_id = int(row["id"])
        if row_id in claimed:
            continue
        others = [r for r in rows
                  if r["owner_norm"] == row["owner_norm"]
                  and int(r["id"]) != row_id and int(r["id"]) not in claimed]
        if not others:
            continue

        candidate = {"task": row["task"], "task_norm": row["task_norm"],
                     "owner": row["owner"]}
        match, score = dedup.find_match(candidate, others)
        how, why = "lexical", "near-identical wording"

        if match is None:
            ranked = sorted(others, key=lambda r: int(r["id"]), reverse=True)
            match, why = judge(row["task"], row["owner"], ranked, ask=ask)
            how = "llm"
            if match is not None:
                score = dedup.jaccard(row["task_norm"], match["task_norm"])

        if match is None:
            continue

        keep, drop = _order(row, match)
        claimed.update({int(keep["id"]), int(drop["id"])})
        found.append({"keep": keep, "drop": drop, "score": score,
                      "how": how, "why": why})
    return found
