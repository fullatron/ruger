"""Moving cards by saying so (§11).

"mark the deck one done and push the invoice to friday" against the rows that
exist. Two model calls at most: a small router deciding create-vs-command, then
this, which returns edits keyed by id.

**This is the only path where a model changes records that already exist.**
Everything else it produces faces the verbatim-quote check; an instruction quotes
nothing, so the safety lives here instead:

  - an id the model returns must exist, be open, and have been in the list it was
    shown. Anything else is dropped, not guessed at;
  - only four fields can move — status, due date, owner, task text — and every one
    of them is reversible. There is deliberately no delete;
  - every value is validated here, not trusted: a status outside the three, a date
    that is not `YYYY-MM-DD`, an empty rename are all refused;
  - what changed is reported back, field by field, so a wrong move is visible in
    the notification rather than discovered a week later.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date

from . import config, db, dedup, extract

ROUTE_PROMPT = extract.PROMPTS / "route_capture.md"
PROMPT = extract.PROMPTS / "apply_instruction.md"

STATUSES = ("todo", "doing", "done")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Long enough to cover a real board, short enough that the model is choosing
# rather than searching. Beyond this the oldest open rows are left out.
MAX_TASKS = 60

ROUTE_SCHEMA = {
    "type": "object",
    "properties": {"kind": {"type": "string", "enum": ["create", "command"]}},
    "required": ["kind"],
    "additionalProperties": False,
}

SCHEMA = {
    "type": "object",
    "properties": {
        "changes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "status": {"type": "string", "enum": list(STATUSES)},
                    "due_date": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "owner": {"type": "string"},
                    "task": {"type": "string"},
                },
                "required": ["id"],
                "additionalProperties": False,
            },
        },
        "unclear": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["changes", "unclear"],
    "additionalProperties": False,
}


def route(text: str, *, ask=None) -> str:
    """'create' or 'command'. Falls back to 'create' whenever unsure.

    A spurious task is visible and deletable; treating new work as a command would
    silently drop it. So every failure mode here resolves to 'create'.
    """
    caller = ask or (lambda values: extract.ask_json(ROUTE_PROMPT, values, ROUTE_SCHEMA))
    try:
        answer = caller({"TEXT": text})
    except Exception:                            # noqa: BLE001 — see the docstring
        return "create"
    if isinstance(answer, dict) and str(answer.get("kind", "")).strip() == "command":
        return "command"
    return "create"


def open_tasks(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """What an instruction may refer to: open rows, most recently touched first."""
    return conn.execute(
        """SELECT c.*, e.title AS meeting FROM commitments c
             JOIN episodes e ON e.id = c.episode_id
            WHERE c.status IN ('todo', 'doing')
         ORDER BY c.updated_at DESC, c.id DESC
            LIMIT ?""",
        (MAX_TASKS,),
    ).fetchall()


def task_lines(rows: list) -> str:
    out = []
    for row in rows:
        bits = [f"id {int(row['id'])}:", row["task"]]
        who = "me" if row["direction"] == "mine" else (row["owner"] or "someone")
        bits.append(f"({who}, {row['status']}, due {row['due_date'] or 'none'})")
        out.append("  " + " ".join(bits))
    return "\n".join(out) if out else "  (nothing open)"


def plan(text: str, rows: list, *, ask=None, today: str | None = None) -> dict:
    """Ask for edits. Returns {'changes': [...], 'unclear': [...]} — unvalidated."""
    caller = ask or (lambda values: extract.ask_json(PROMPT, values, SCHEMA))
    try:
        answer = caller({
            "TEXT": text,
            "TODAY": today or date.today().isoformat(),
            "TASKS": task_lines(rows),
        })
    except Exception as exc:                     # noqa: BLE001
        return {"changes": [], "unclear": [], "error": str(exc)}

    if not isinstance(answer, dict):
        return {"changes": [], "unclear": [], "error": "instruction returned the wrong shape"}
    changes = answer.get("changes")
    return {
        "changes": changes if isinstance(changes, list) else [],
        "unclear": [str(u) for u in (answer.get("unclear") or [])
                    if isinstance(answer.get("unclear"), list)],
        "error": None,
    }


def validate(changes: list, rows: list) -> tuple[list[dict], list[str]]:
    """Keep only edits that name a real open row and carry usable values."""
    allowed = {int(r["id"]): r for r in rows}
    kept: list[dict] = []
    refused: list[str] = []

    for item in changes:
        if not isinstance(item, dict):
            refused.append("an edit was not an object")
            continue
        try:
            task_id = int(item.get("id"))
        except (TypeError, ValueError):
            refused.append(f"unusable id {item.get('id')!r}")
            continue

        row = allowed.get(task_id)
        if row is None:
            # Either invented, or a row that was never offered. Both are refusals.
            refused.append(f"id {task_id} is not one of the open tasks")
            continue

        fields: dict[str, object] = {}

        if "status" in item:
            status = str(item["status"]).strip().lower()
            if status not in STATUSES:
                refused.append(f"id {task_id}: {item['status']!r} is not a status")
            elif status != row["status"]:
                fields["status"] = status

        if "due_date" in item:
            raw = item["due_date"]
            if raw is None or str(raw).strip() == "":
                if row["due_date"]:
                    fields["due_date"] = None
            elif _DATE.match(str(raw).strip()):
                if str(raw).strip() != (row["due_date"] or ""):
                    fields["due_date"] = str(raw).strip()
            else:
                refused.append(f"id {task_id}: {raw!r} is not a date")

        if "owner" in item:
            owner = " ".join(str(item["owner"]).split())
            if not owner:
                refused.append(f"id {task_id}: empty owner")
            else:
                direction = "mine" if dedup.normalise_owner(owner) == "me" else "theirs"
                if owner != row["owner"] or direction != row["direction"]:
                    fields["owner"] = "me" if direction == "mine" else owner
                    fields["direction"] = direction

        if "task" in item:
            renamed = " ".join(str(item["task"]).split())
            if not renamed:
                refused.append(f"id {task_id}: empty rename")
            elif len(renamed) > 500:
                refused.append(f"id {task_id}: rename is too long")
            elif renamed != row["task"]:
                fields["task"] = renamed

        if fields:
            kept.append({"id": task_id, "row": row, "fields": fields})

    return kept, refused


def describe(change: dict) -> str:
    """One line a person can check at a glance."""
    row, fields = change["row"], change["fields"]
    bits = []
    if "status" in fields:
        bits.append(f"{row['status']} → {fields['status']}")
    if "due_date" in fields:
        bits.append(f"due {fields['due_date'] or 'cleared'}")
    if "owner" in fields:
        bits.append(f"owner {fields['owner']}")
    if "task" in fields:
        bits.append(f'renamed to "{fields["task"]}"')
    return f"{row['task']}: {', '.join(bits)}"


def apply(conn: sqlite3.Connection, changes: list) -> list[dict]:
    """Write the validated edits. Status is set separately from content fields.

    `set_status` and `update_commitment` are different calls on purpose:
    `update_commitment` flags a row as human-edited so re-extraction stops
    overwriting its wording, and a status move is not a reason to freeze wording.
    """
    applied = []
    for change in changes:
        fields = dict(change["fields"])
        status = fields.pop("status", None)
        with db.transaction(conn):
            if fields:
                db.update_commitment(conn, change["id"], fields)
            if status is not None:
                db.set_status(conn, change["id"], str(status))
        applied.append(change)
    return applied


def run(conn: sqlite3.Connection, text: str, *, ask=None, plan_fn=None,
        today: str | None = None, push: bool = True) -> dict:
    """Instruction in, edits out, Notion updated for anything that moved column."""
    rows = open_tasks(conn)
    proposed = (plan_fn or plan)(text, rows, ask=ask, today=today)

    changes, refused = validate(proposed["changes"], rows)
    applied = apply(conn, changes)

    result = {
        "kind": "command",
        "error": proposed.get("error"),
        "changes": [{"id": c["id"], "task": c["row"]["task"], "fields": c["fields"],
                     "line": describe(c)} for c in applied],
        "unclear": proposed["unclear"],
        "refused": refused,
        "pushed": 0,
        "push_error": None,
    }

    moved = [c["id"] for c in applied if "status" in c["fields"]]
    edited = [c["id"] for c in applied]
    if push and edited:
        result.update(_push(conn, set(edited), force_status=set(moved)))

    _log(conn, text, result)
    return result


def _log(conn: sqlite3.Connection, text: str, result: dict) -> None:
    """Record the instruction itself, whatever came of it.

    Especially when nothing did: "nothing matched" written down is a system that
    heard you and disagreed, which is a different thing from a system that is
    broken, and you cannot tell them apart from silence.
    """
    changes = result["changes"]
    if changes:
        detail = "; ".join(c["line"] for c in changes[:3])
        if len(changes) > 3:
            detail += f"; and {len(changes) - 3} more"
    elif result.get("error"):
        detail = f"could not read it: {result['error']}"
    elif result["unclear"]:
        detail = f"nothing changed — {result['unclear'][0]}"
    else:
        detail = "nothing matched"

    with db.transaction(conn):
        db.log_event(conn, "instructed", " ".join(str(text or "").split())[:300],
                     commitment_id=changes[0]["id"] if len(changes) == 1 else None,
                     detail=detail)


def _push(conn: sqlite3.Connection, ids: set[int], force_status: set[int]) -> dict:
    """Send the edited rows out: content always, Status for the ones that moved.

    Both flags are overrides, and an instruction is what justifies them. D9 keeps
    Status out of a routine push so it cannot drag a card back out of a column;
    D21 keeps content out so a re-extraction cannot revert your edits in Notion.
    Neither applies when a person just said "push the invoice to friday" — that is
    explicit intent about this card, scoped to this card.

    `resend` was missing here at first, and the result was silent: the due date
    changed locally, the notification said so, and Notion never heard about it.
    """
    from .connectors import notion

    try:
        stats = notion.push(conn, only=ids, resend=True, force_status=force_status)
    except notion.NotionError as exc:
        return {"pushed": 0, "push_error": str(exc)}
    return {"pushed": stats["created"] + stats["updated"],
            "push_error": stats["errors"][0] if stats["errors"] else None}


def summarise(result: dict) -> str:
    """One line for a notification."""
    if result.get("error"):
        return f"Could not read that instruction: {result['error']}"

    changes = result["changes"]
    if not changes:
        if result["unclear"]:
            return f"Not sure which task: {result['unclear'][0]}"
        if result["refused"]:
            return f"Nothing changed ({result['refused'][0]})"
        return "Nothing matched that instruction"

    if len(changes) == 1:
        return changes[0]["line"]
    return f"{len(changes)} tasks updated: " + "; ".join(
        c["task"] for c in changes[:3])
