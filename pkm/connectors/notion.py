"""Push commitments to a Notion database, and read status back.

Ruger's job ends at a commitment that survived the verbatim-quote check. Where
you *work* that list is a separate question, and a Notion database is a better
board than anything this repo should build: it already has views, filters,
mobile, reminders and sharing.

So this is a two-way sync where each direction owns different fields:

    push   local content  ->  Notion    task, owner, direction, due, evidence
    pull   Notion status  ->  local     which column the human dragged it to

Status is written exactly **once**, when the page is created. After that Notion
owns it. A push that re-sent status would undo the drag you just did, which is
the same class of mistake `sync.reextract_episode` exists to avoid: an automated
refresh must never quietly discard human work.

Nothing here is on the ingest path, so a Notion outage cannot stop a meeting
being extracted. The local database stays the source of truth for content, and
the Notion database is derived from it and rebuildable.
"""

from __future__ import annotations

import json
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from .. import config, db

API = "https://api.notion.com/v1"

# Pinned deliberately. Later API versions split a database into "data sources"
# and change the shape of a page's `parent`, so the version is part of the
# contract this file implements, not an incidental header. Bump it and the
# create/query calls need revisiting together.
VERSION = "2022-06-28"

TIMEOUT = 30.0
PAGE_SIZE = 100

# Notion allows roughly three requests a second and answers 429 above that.
# A gap this size keeps a push of a few hundred tasks inside the budget without
# needing the retry path at all.
GAP = 0.34
MAX_RETRIES = 4

# A single rich_text item caps at 2000 characters.
TEXT_LIMIT = 2000

TITLE_PROP = "Task"
ID_PROP = "Ruger ID"
STATUS_PROP = "Status"

STATUS_TO_NOTION = {"todo": "To do", "doing": "In progress", "done": "Done"}

# §18. Where a finished card goes once it has stopped being interesting. This is
# a Status option, not a fourth local status: locally an archived commitment is
# simply done, with an `archived_at` stamp, which is why the aliases below fold
# it back to `done` when it is read.
ARCHIVE_OPTION = "Archive"
ARCHIVE_NAMES = {"archive", "archived", "filed"}

# How long a card sits in Done before it is filed away.
ARCHIVE_AFTER_DAYS = 3

# Read back generously: renaming a column in Notion is a normal thing to do, and
# it must not silently stop status syncing. Anything unrecognised is left alone.
STATUS_ALIASES = {
    "to do": "todo", "todo": "todo", "to-do": "todo", "not started": "todo",
    "backlog": "todo", "open": "todo", "next": "todo", "inbox": "todo",
    "in progress": "doing", "doing": "doing", "in-progress": "doing",
    "wip": "doing", "started": "doing", "active": "doing",
    "done": "done", "complete": "done", "completed": "done", "shipped": "done",
    "closed": "done",
    # Archived is still done. Without these three a card this very code moved
    # into Archive would read back as unrecognised and be counted `unreadable`
    # forever, which is the feature reporting itself as broken.
    "archive": "done", "archived": "done", "filed": "done",
}

# D3's mine/theirs survives the trip as Notion's own chip colours.
SCHEMA = {
    TITLE_PROP: {"title": {}},
    STATUS_PROP: {"select": {"options": [
        {"name": "To do", "color": "default"},
        {"name": "In progress", "color": "blue"},
        {"name": "Done", "color": "green"},
        {"name": ARCHIVE_OPTION, "color": "gray"},
    ]}},
    "Direction": {"select": {"options": [
        {"name": "Mine", "color": "purple"},
        {"name": "Theirs", "color": "green"},
    ]}},
    "Owner": {"rich_text": {}},
    "Due": {"date": {}},
    "Raised": {"number": {"format": "number"}},
    "Evidence": {"rich_text": {}},
    "Meeting": {"rich_text": {}},
    "Met on": {"date": {}},
    "Source": {"select": {"options": [
        {"name": "Extracted", "color": "default"},
        {"name": "Added by hand", "color": "orange"},
    ]}},
    ID_PROP: {"number": {"format": "number"}},
}


class NotionError(Exception):
    """Anything that stopped a sync. Message is safe to show a user."""


# --- transport ---------------------------------------------------------------


def _token() -> str:
    token = (config.NOTION_TOKEN or "").strip()
    if not token:
        raise NotionError(
            "no Notion token. Create an internal integration at "
            "notion.so/my-integrations, then set PKM_NOTION_TOKEN."
        )
    return token


def _request(method: str, path: str, payload: dict | None = None,
             *, base: str | None = None) -> dict:
    """One Notion call, with backoff for the two failures worth retrying.

    429 is expected under normal use and carries `Retry-After`; 5xx is a bad
    minute at Notion's end. Everything else is a bug in our request and is
    raised straight away rather than hammered.
    """
    url = f"{(base or API).rstrip('/')}{path}"
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
        "Authorization": f"Bearer {_token()}",
        "Notion-Version": VERSION,
        "Accept": "application/json",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"

    last = ""
    for attempt in range(MAX_RETRIES):
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
                return json.loads(res.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            detail = _error_message(raw) or raw[:200]
            if exc.code == 401:
                raise NotionError(
                    "Notion rejected the token (401). Check PKM_NOTION_TOKEN."
                ) from exc
            if exc.code == 404:
                raise NotionError(
                    f"Notion says that object does not exist, or the integration "
                    f"has not been given access to it (404). {detail}"
                ) from exc
            if exc.code == 429 or exc.code >= 500:
                last = f"{exc.code}: {detail}"
                wait = _retry_after(exc) or (GAP * (2 ** attempt))
                if attempt == MAX_RETRIES - 1:
                    break
                time.sleep(min(wait, 10.0))
                continue
            raise NotionError(f"Notion {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            last = f"could not reach Notion: {exc.reason}"
            if attempt == MAX_RETRIES - 1:
                break
            time.sleep(GAP * (2 ** attempt))
        except json.JSONDecodeError as exc:
            raise NotionError(f"Notion sent a non-JSON reply: {exc}") from exc

    raise NotionError(f"Notion is not answering. Last error: {last}")


def _error_message(raw: str) -> str:
    try:
        return " ".join(str(json.loads(raw).get("message", "")).split())[:300]
    except (json.JSONDecodeError, AttributeError):
        return ""


def _retry_after(exc: urllib.error.HTTPError) -> float:
    try:
        return float(exc.headers.get("Retry-After") or 0)
    except (TypeError, ValueError):
        return 0.0


# --- property mapping --------------------------------------------------------


def _text(value: str | None) -> list:
    """A rich_text / title payload, truncated to Notion's per-item limit."""
    body = " ".join(str(value or "").split())
    if not body:
        return []
    if len(body) > TEXT_LIMIT:
        body = body[: TEXT_LIMIT - 1] + "…"
    return [{"type": "text", "text": {"content": body}}]


def _date(value: str | None) -> dict | None:
    value = (value or "").strip()
    return {"start": value} if value else None


def content_properties(row: sqlite3.Row | dict) -> dict:
    """Everything push owns. Deliberately excludes Status (Notion owns that)."""
    row = dict(row)
    quote = row.get("quote") or ""
    return {
        TITLE_PROP: {"title": _text(row.get("task"))},
        "Direction": {"select": {"name": "Mine" if row.get("direction") == "mine"
                                 else "Theirs"}},
        "Owner": {"rich_text": _text(row.get("owner"))},
        "Due": {"date": _date(row.get("due_date"))},
        "Raised": {"number": int(row.get("mention_count") or 1)},
        # D5: the evidence travels with the task, so a wrong extraction is still
        # obvious at a glance in its new home.
        "Evidence": {"rich_text": _text(
            f"“{quote}”" + (f" — {row['speaker']}" if row.get("speaker") else "")
            if quote else ""
        )},
        "Meeting": {"rich_text": _text(row.get("meeting"))},
        "Met on": {"date": _date(row.get("meeting_date"))},
        "Source": {"select": {"name": "Added by hand"
                              if row.get("origin") == "manual" else "Extracted"}},
        ID_PROP: {"number": int(row["id"])},
    }


def page_body(row: sqlite3.Row | dict) -> list:
    """Blocks written once, when the page is created.

    The quote goes in the body as well as the property so the page reads as a
    record of what was actually said, not just a task someone typed.
    """
    row = dict(row)
    blocks = []
    if row.get("quote"):
        blocks.append({
            "object": "block", "type": "quote",
            "quote": {"rich_text": _text(row["quote"])},
        })
    else:
        blocks.append({
            "object": "block", "type": "paragraph",
            "paragraph": {"rich_text": _text(
                "Added by hand in Ruger. Nobody said this in a meeting, so it "
                "carries no quote."
            )},
        })

    trail = " · ".join(filter(None, [
        row.get("speaker"), row.get("meeting"), row.get("meeting_date"),
    ]))
    blocks.append({
        "object": "block", "type": "paragraph",
        "paragraph": {"rich_text": _text(trail or "Source unknown")},
    })
    return blocks


def append_subtasks(page_id: str, texts: list[str]) -> list[str]:
    """Append to-do blocks to a page body. Returns the new block ids, in order.

    §13: steps live in the page body rather than as sub-item relations, because
    sub-items must be enabled on the database and the API cannot enable them —
    the same class of assumption that put seven untitled cards on the first live
    board. A checklist works on any database, including a stock template.

    This appends. It never rewrites the body, so a step you added in Notion by
    hand is not disturbed.
    """
    children = [{"object": "block", "type": "to_do",
                 "to_do": {"rich_text": _text(t), "checked": False}}
                for t in texts if str(t or "").strip()]
    if not children:
        return []
    result = _request("PATCH", f"/blocks/{page_id}/children", {"children": children})
    return [b.get("id", "") for b in result.get("results", [])]


def _prop(page: dict, name: str) -> dict:
    return (page.get("properties") or {}).get(name) or {}


def read_status_name(page: dict) -> str:
    """The Status option exactly as Notion spells it, before any aliasing.

    Needed as well as `read_status` because Archive collapses to `done` on the
    way in (§18) and the two cases have to be told apart: a card sitting in
    Archive must not be swept again, and a card dragged out of it must start its
    three days over.
    """
    prop = _prop(page, STATUS_PROP)
    chosen = prop.get("select") or prop.get("status") or {}
    return ((chosen or {}).get("name") or "").strip()


def read_status(page: dict) -> str | None:
    """Which local status a Notion page is sitting in, if we can tell."""
    return STATUS_ALIASES.get(read_status_name(page).lower())


def is_archived(page_or_name: dict | str) -> bool:
    name = (page_or_name if isinstance(page_or_name, str)
            else read_status_name(page_or_name))
    return name.strip().lower() in ARCHIVE_NAMES


def archive_option(profile: dict) -> str:
    """This database's own word for the archive column, or '' if it has none."""
    for option in profile.get("status_options") or []:
        if option.strip().lower() in ARCHIVE_NAMES:
            return option
    return ""


def read_ruger_id(page: dict) -> int | None:
    value = _prop(page, ID_PROP).get("number")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# --- setup -------------------------------------------------------------------


def whoami() -> dict:
    """Prove the token works without writing anything."""
    bot = _request("GET", "/users/me")
    owner = ((bot.get("bot") or {}).get("owner") or {}).get("user") or {}
    return {
        "name": bot.get("name") or "(unnamed integration)",
        "workspace": (bot.get("bot") or {}).get("workspace_name") or "",
        "user": owner.get("name") or "",
    }


def shared_pages(limit: int = 25) -> list[dict]:
    """Pages the integration can see. Empty means nothing was shared with it."""
    found = _request("POST", "/search", {
        "filter": {"value": "page", "property": "object"},
        "page_size": min(limit, PAGE_SIZE),
    })
    pages = []
    for item in found.get("results", []):
        if item.get("object") != "page":
            continue
        title = ""
        for prop in (item.get("properties") or {}).values():
            if prop.get("type") == "title":
                title = "".join(t.get("plain_text", "") for t in prop.get("title", []))
                break
        pages.append({"id": item.get("id"), "title": title or "(untitled)",
                      "url": item.get("url")})
    return pages


def create_database(parent_page_id: str, title: str = "Ruger — commitments") -> dict:
    """Create the board. Returns {'id', 'url'}."""
    parent = config.notion_id(parent_page_id)
    if not parent:
        raise NotionError("need a parent page id to create the database in")
    created = _request("POST", "/databases", {
        "parent": {"type": "page_id", "page_id": parent},
        "title": _text(title),
        "description": _text(
            "Pushed by Ruger from meeting notes. Move a card between Status "
            "options and `pkm pull` brings that back."
        ),
        "properties": SCHEMA,
    })
    return {"id": created.get("id", ""), "url": created.get("url", "")}


def ensure_schema(database_id: str) -> list[str]:
    """Add any missing property to an existing database. Returns what it added.

    Lets you point Ruger at a database you already have rather than only one it
    created. Existing properties are never modified, so a Status column you have
    renamed or recoloured survives.
    """
    database_id = config.notion_id(database_id)
    current = _request("GET", f"/databases/{database_id}")
    have = set((current.get("properties") or {}).keys())

    missing = {name: spec for name, spec in SCHEMA.items() if name not in have}
    # The title property always exists under some name; never try to add one.
    missing.pop(TITLE_PROP, None)
    if not missing:
        return []
    _request("PATCH", f"/databases/{database_id}", {"properties": missing})
    return sorted(missing)


def _status_spec(database_id: str) -> tuple[str, str, dict]:
    """The Status column exactly as Notion stores it: name, type, raw spec."""
    props = (_request("GET", f"/databases/{database_id}").get("properties") or {})
    chosen: tuple[str, str, dict] | None = None
    for name, spec in props.items():
        kind = spec.get("type")
        if kind not in ("status", "select"):
            continue
        if name == STATUS_PROP:
            chosen = (name, kind, spec.get(kind) or {})
        elif chosen is None and kind == "status":
            chosen = (name, kind, spec.get(kind) or {})
    if not chosen:
        raise NotionError(
            "that database has no Status column, so there is nowhere to archive "
            "to. Add a Status or Select property called Status in Notion."
        )
    return chosen


def ensure_archive_option(database_id: str | None = None) -> dict:
    """Make sure the board has somewhere to file finished work (§18).

    Notion's documentation says a `status` property cannot be edited over the
    API. Measured against a real workspace on 2026-08-06 that is only half true:
    **adding an option is accepted and applied; moving it between groups is
    accepted and silently ignored.** So this adds the option and then *reads the
    database back* to find out what actually happened, rather than believing the
    200 — the same rule the providers follow for a schema that was requested but
    perhaps not honoured.

    Returns {'option', 'created', 'group', 'grouped', 'note'}. `grouped` is False
    when Notion filed the new option under an unfinished group, which is cosmetic
    but visible: on a grouped board view the archive would sit next to To-do.
    Nothing here can fix that, so it is reported instead of hidden.
    """
    database_id = config.notion_id(database_id) or _database()
    prop, kind, spec = _status_spec(database_id)

    existing = next((o.get("name", "") for o in spec.get("options") or []
                     if is_archived(o.get("name", ""))), "")
    created = False
    if not existing:
        # Send the options back before adding to them. A select **replaces** the
        # list it is given, so an option omitted here is deleted, and every card
        # sitting in it loses its status. Identify by id where there is one and
        # by name otherwise; never drop an entry for want of a field.
        options = [{k: v for k, v in (("id", o.get("id")), ("name", o.get("name")),
                                      ("color", o.get("color"))) if v}
                   for o in spec.get("options") or []]
        _request("PATCH", f"/databases/{database_id}", {"properties": {
            prop: {kind: {"options": options + [{"name": ARCHIVE_OPTION,
                                                 "color": "gray"}]}}}})
        prop, kind, spec = _status_spec(database_id)
        existing = next((o.get("name", "") for o in spec.get("options") or []
                         if is_archived(o.get("name", ""))), "")
        if not existing:
            raise NotionError(
                f"Notion accepted the change but the {prop} column still has no "
                f"{ARCHIVE_OPTION} option. Add one by hand in Notion: open the "
                f"{prop} property, then Edit property, then + Add option."
            )
        created = True

    group, grouped = "", True
    if kind == "status":
        by_id = {o.get("id"): o.get("name", "") for o in spec.get("options") or []}
        for candidate in spec.get("groups") or []:
            if any(is_archived(by_id.get(oid, ""))
                   for oid in candidate.get("option_ids") or []):
                group = candidate.get("name", "")
                break
        # Notion decides which group a new option lands in and will not let the
        # API move it. Anything that is not the completed group reads wrong.
        grouped = STATUS_ALIASES.get(group.strip().lower()) == "done" if group else True

    note = ""
    if not grouped:
        note = (f"Notion put “{existing}” in the “{group}” group. Drag it into "
                f"“Complete” in the {prop} property to keep the board tidy — the "
                f"API cannot move it, and nothing else depends on it.")
    return {"option": existing, "created": created, "group": group,
            "grouped": grouped, "note": note, "property": prop, "type": kind}


def title_property(database_id: str) -> str:
    """Whatever the database calls its title column."""
    return board_profile(database_id)["title"]


def board_profile(database_id: str | None = None) -> dict:
    """Learn how *this* database is shaped before writing a single page to it.

    An adopted database rarely matches the one Ruger would have built. Two
    differences are fatal if assumed away:

      - the title column can be called anything ("Name" is Notion's default), and
      - Status may be Notion's real `status` type rather than the `select` Ruger
        creates. It takes a different payload shape, and its options *cannot be
        created over the API* — so writing "To do" to a column offering
        "Not started" is a 400 on every page.

    So the shape is read once per push and every write is fitted to it.
    """
    database_id = config.notion_id(database_id) or _database()
    props = (_request("GET", f"/databases/{database_id}").get("properties") or {})

    title = TITLE_PROP
    status: tuple[str, str, dict] | None = None
    for name, spec in props.items():
        kind = spec.get("type")
        if kind == "title":
            title = name
        elif kind in ("status", "select"):
            # A column actually called Status wins; otherwise take the first
            # real status-type column, which is what a Notion template gives you.
            if name == STATUS_PROP:
                status = (name, kind, spec)
            elif status is None and kind == "status":
                status = (name, kind, spec)

    options: list[str] = []
    if status:
        options = [o.get("name", "") for o in
                   (status[2].get(status[1]) or {}).get("options") or []]

    return {
        "title": title,
        "status_prop": status[0] if status else "",
        "status_type": status[1] if status else "",
        "status_options": options,
        "properties": sorted(props),
    }


def status_payload(local: str, profile: dict) -> dict | None:
    """Which of the column's existing options means `local`, in its own shape.

    Returns None when nothing matches, and push then simply leaves Status off
    the create: a card that lands in the default column is recoverable, a card
    that 400'd and never arrived is not.
    """
    prop, kind = profile.get("status_prop"), profile.get("status_type")
    if not prop or not kind:
        return None

    options = profile.get("status_options") or []
    wanted = STATUS_TO_NOTION.get(local, "To do")

    pick = next((o for o in options if o == wanted), None)
    if pick is None:
        # Read the column's own vocabulary back through the same aliases pull
        # uses, so "Not started" is recognised as todo without configuration.
        # Archive is excluded: it aliases to `done` on the way in, and without
        # this guard a database offering only Archive would open every finished
        # card straight into it, which is a sweep the human never asked for.
        pick = next((o for o in options
                     if STATUS_ALIASES.get(o.strip().lower()) == local
                     and not is_archived(o)), None)
    if pick is None and not options:
        pick = wanted
    if pick is None:
        return None
    return {prop: {kind: {"name": pick}}}


# --- push --------------------------------------------------------------------


def _database() -> str:
    database_id = config.NOTION_DB
    if not database_id:
        raise NotionError(
            "no Notion database configured. Run `pkm notion setup` or set "
            "PKM_NOTION_DB."
        )
    return database_id


def push(conn: sqlite3.Connection, *, dry_run: bool = False,
         limit: int | None = None, only: set[int] | None = None,
         force_status: set[int] | None = None, resend: bool = False) -> dict:
    """Create the pages that are missing. **Existing pages are left alone.**

    §12: Notion owns a card once it exists. Ruger writes everything at creation
    and never overwrites it again, so a title you fixed or a date you moved in
    Notion stays fixed. Before this, push re-sent content on every run and
    silently reverted those edits within five minutes.

    `resend=True` is the deliberate override, for when a re-extraction really
    should overwrite the card. It is not what the timer does.

    Idempotent by construction — the Notion page id is stored on the row, so a
    second push with no new commitments creates nothing.

    `only` narrows it to specific commitment ids. A capture uses that so one
    dictated sentence costs one API call rather than a re-send of the whole
    board: push is O(board) by design, which is fine on a timer and far too slow
    to sit behind a notification.

    `force_status` is the one way Status is ever re-sent, and it must stay narrow.
    Normally Notion owns which column a card sits in (D9) precisely so a routine
    re-push cannot drag a card out of Done. An instruction — "mark the deck one
    done" — is explicit human intent about that column, so those ids get their
    Status written. Nothing else does, ever.
    """
    database_id = _database()
    rows = db.commitments_to_push(conn)
    if only is not None:
        rows = [r for r in rows if int(r["id"]) in only]
    if limit:
        rows = rows[:limit]

    # One read, before any write, so every page is fitted to this database.
    profile = {"title": TITLE_PROP} if dry_run else board_profile(database_id)
    title = profile["title"]

    stats = {"total": len(rows), "created": 0, "updated": 0, "skipped": 0,
             "failed": 0, "errors": [], "plan": [], "url": ""}

    for row in rows:
        properties = content_properties(row)
        if title != TITLE_PROP:
            properties[title] = properties.pop(TITLE_PROP)

        existing = row["external_id"]
        moving = bool(force_status and int(row["id"]) in force_status)

        if not existing:
            action = "create"
        elif resend:
            action = "resend"
        elif moving:
            action = "status"
        else:
            action = "skip"

        stats["plan"].append({
            "id": int(row["id"]), "action": action, "task": row["task"],
            "owner": row["owner"], "status": row["status"],
            "due_date": row["due_date"], "url": row["external_url"],
        })
        if dry_run:
            key = {"create": "created", "skip": "skipped"}.get(action, "updated")
            stats[key] += 1
            continue

        if action == "skip":
            # The page exists and nobody asked to overwrite it. Notion owns it.
            stats["skipped"] += 1
            continue

        try:
            if existing:
                opening = status_payload(row["status"], profile) if moving else None
                if action == "status":
                    # Only the column moved, so send only the column. Including
                    # content here would revert whatever was edited in Notion,
                    # which is the whole bug §12 exists to fix.
                    payload = dict(opening or {})
                    if not payload:
                        stats["skipped"] += 1
                        continue
                else:
                    payload = dict(properties)
                    payload.update(opening or {})
                _request("PATCH", f"/pages/{existing}", {"properties": payload})
                with db.transaction(conn):
                    db.log_event(conn, "resent" if action == "resend" else "status",
                                 row["task"], commitment_id=int(row["id"]),
                                 # No detail for a resend: the label already says
                                 # it, and repeating it reads as a rendering bug.
                                 detail=(None if action == "resend"
                                         else f"set to {row['status']} in Notion"),
                                 external_url=row["external_url"])
                stats["updated"] += 1
            else:
                # Status is set here and only here. Every later push leaves it
                # alone so a card the human dragged stays where they put it.
                opening = status_payload(row["status"], profile)
                if opening:
                    properties.update(opening)
                page = _request("POST", "/pages", {
                    "parent": {"database_id": database_id},
                    "properties": properties,
                    "children": page_body(row),
                })
                with db.transaction(conn):
                    db.mark_pushed(conn, int(row["id"]), page.get("id", ""),
                                   page.get("url"))
                    db.log_event(conn, "created", row["task"],
                                 commitment_id=int(row["id"]),
                                 detail=f"opened as {row['status']}",
                                 external_url=page.get("url"))
                # Steps added before the page existed have been waiting for a
                # body to be appended to (§13).
                waiting = [s for s in db.subtasks_for(conn, int(row["id"]))
                           if not s["block_id"]]
                if waiting:
                    blocks = append_subtasks(page.get("id", ""),
                                             [s["text"] for s in waiting])
                    with db.transaction(conn):
                        for subtask, block_id in zip(waiting, blocks):
                            db.mark_subtask_block(conn, int(subtask["id"]), block_id)
                stats["created"] += 1
        except NotionError as exc:
            # One bad row must not abandon the rest of the board.
            stats["failed"] += 1
            stats["errors"].append(f"{row['task'][:60]}: {exc}")
        time.sleep(GAP)

    return stats


# --- the archive (§18) --------------------------------------------------------


def sweep(conn: sqlite3.Connection, *, days: int = ARCHIVE_AFTER_DAYS,
          dry_run: bool = False, limit: int | None = None,
          now: datetime | None = None) -> dict:
    """Move cards that have been Done for more than `days` into Archive.

    This is the second deliberate exception to D9's "Notion owns status", after
    an explicit instruction. It is narrow in the same way: only rows already in
    done, only after a window long enough that nobody is still looking at them,
    only ever *into* the archive, and never out. Dragging one back is a decision
    this respects — `db.clear_archived` restarts its clock so the sweep does not
    quietly undo the drag five minutes later.

    Cheap when there is nothing to do: the candidate query is one indexed read
    and it returns before any HTTP call, so an idle tick costs nothing.
    """
    stats = {"days": days, "cutoff": "", "candidates": 0, "option": "",
             "archived": 0, "failed": 0, "ready": True, "off": False, "note": "",
             "errors": [], "moved": []}
    if days <= 0:
        # The kill switch, for anyone who would rather clear their own Done
        # column. Zero means off rather than "file it the same day": a setting
        # that quiets a feature must never be the setting that fires it hardest.
        stats["off"] = True
        stats["note"] = "archiving is off (PKM_ARCHIVE_AFTER_DAYS=0)"
        return stats

    stats["cutoff"] = cutoff = (
        (now or datetime.now(timezone.utc)) - timedelta(days=days)
    ).isoformat(timespec="seconds")
    rows = db.commitments_to_archive(conn, before=cutoff, limit=limit)
    stats["candidates"] = len(rows)
    if not rows:
        return stats

    profile = board_profile()
    option = archive_option(profile)
    if not option:
        # Refuse rather than improvise. Writing an option that does not exist is
        # a 400 per card, and inventing a different column would be a schema
        # change nobody asked for.
        stats["ready"] = False
        stats["note"] = (
            f"the Status column has no {ARCHIVE_OPTION} option, so there is "
            f"nowhere to file {len(rows)} finished card(s). Run "
            f"`python -m pkm archive --setup`."
        )
        return stats

    stats["option"] = option
    prop, kind = profile["status_prop"], profile["status_type"]

    for row in rows:
        since = (row["done_at"] or "")[:10]
        stats["moved"].append({"id": int(row["id"]), "task": row["task"],
                               "done_at": row["done_at"], "url": row["external_url"]})
        if dry_run:
            continue
        try:
            _request("PATCH", f"/pages/{row['external_id']}",
                     {"properties": {prop: {kind: {"name": option}}}})
            with db.transaction(conn):
                db.mark_archived(conn, int(row["id"]))
                db.log_event(conn, "archived", row["task"],
                             commitment_id=int(row["id"]),
                             detail=f"done since {since}, moved to {option}",
                             external_url=row["external_url"])
            stats["archived"] += 1
        except NotionError as exc:
            # One card Notion will not take must not strand the rest.
            stats["failed"] += 1
            stats["errors"].append(f"{row['task'][:60]}: {exc}")
        time.sleep(GAP)

    return stats


# --- pull --------------------------------------------------------------------


def all_pages(database_id: str | None = None) -> list[dict]:
    """Every page in the database, following pagination."""
    database_id = config.notion_id(database_id) or _database()
    pages: list[dict] = []
    cursor = None
    while True:
        payload: dict = {"page_size": PAGE_SIZE}
        if cursor:
            payload["start_cursor"] = cursor
        batch = _request("POST", f"/databases/{database_id}/query", payload)
        pages.extend(batch.get("results", []))
        if not batch.get("has_more"):
            return pages
        cursor = batch.get("next_cursor")
        if not cursor:
            return pages
        time.sleep(GAP)


# A pull that finds most of the board missing is far more likely to be a bad
# query — wrong database, revoked access, an empty page of results — than a person
# deleting nearly everything at once. Above this fraction it reports and refuses.
FORGET_LIMIT = 0.5


def pull(conn: sqlite3.Connection, *, dry_run: bool = False,
         prune: bool = False, forget_missing: bool = True) -> dict:
    """Bring Notion's status back, and report what has drifted.

    Notion wins on status, because that is where the human now works. Nothing
    else is read back: content stays owned by the extraction and by edits made
    here, so a stray keystroke in Notion cannot rewrite a verified quote.

    `forget_missing` (§14): a card you deleted in Notion is deleted here too.
    Under D21 Notion owns the card, so removing it there is the clearest possible
    statement of intent — and the old rule of keeping it and warning forever left
    the two boards permanently disagreeing, with a warning on every tick that
    nothing could ever clear. The log keeps the record.
    """
    pages = all_pages()
    local = {int(r["id"]): r for r in db.commitments_to_push(conn)}

    stats = {"pages": len(pages), "changed": 0, "unchanged": 0, "orphaned": 0,
             "archived": 0, "unlinked": 0, "unreadable": 0, "moves": [],
             "orphans": [], "forgotten": 0, "kept": [], "filed": 0, "unfiled": 0}

    seen: set[int] = set()
    for page in pages:
        cid = read_ruger_id(page)
        if cid is None:
            # A page someone added in Notion by hand. Ruger has no row for it
            # and will not invent one: the board is derived from meetings.
            stats["unlinked"] += 1
            continue
        seen.add(cid)

        row = local.get(cid)
        if row is None:
            stats["orphaned"] += 1
            stats["orphans"].append({"id": cid, "page": page.get("id"),
                                     "url": page.get("url")})
            if prune and not dry_run:
                _request("PATCH", f"/pages/{page.get('id')}", {"archived": True})
                stats["archived"] += 1
                time.sleep(GAP)
            continue

        status = read_status(page)
        if status is None:
            stats["unreadable"] += 1
            continue

        if status == row["status"]:
            stats["unchanged"] += 1
        else:
            stats["changed"] += 1
            stats["moves"].append({"id": cid, "task": row["task"],
                                   "from": row["status"], "to": status})
            if not dry_run:
                with db.transaction(conn):
                    # Reopening a card clears its archive stamp here.
                    db.set_status(conn, cid, status)
                    # The log's whole job: what came back, and when.
                    db.log_event(conn, "status", row["task"], commitment_id=cid,
                                 detail=f"{row['status']} → {status} in Notion",
                                 external_url=row["external_url"])

        # Archive membership is tracked by the option's *name*, because it
        # aliases to `done` on the way in and the status alone cannot tell the
        # two apart (§18). Someone who files a card by hand should not have it
        # swept a second time, and someone who drags one back out should get the
        # full window again rather than watching it re-file itself.
        if not dry_run and status == "done":
            filed, was = is_archived(page), bool(row["archived_at"])
            if filed and not was:
                with db.transaction(conn):
                    db.mark_archived(conn, cid)
                stats["filed"] += 1
            elif was and not filed:
                with db.transaction(conn):
                    db.clear_archived(conn, cid)
                stats["unfiled"] += 1

    # A row whose page vanished from the database: you deleted the card.
    missing = [(cid, row) for cid, row in local.items()
               if row["external_id"] and cid not in seen]
    stats["missing"] = [{"id": cid, "task": row["task"]} for cid, row in missing]

    pushed = sum(1 for r in local.values() if r["external_id"])
    # Refuse to act on a result that looks like a broken query rather than a
    # decision: no pages at all, or most of the board gone in one run.
    suspicious = (not pages) or (pushed and len(missing) / pushed > FORGET_LIMIT)

    if missing and forget_missing and not dry_run and not suspicious:
        for cid, row in missing:
            with db.transaction(conn):
                db.delete_commitment(conn, cid)
                db.log_event(conn, "deleted", row["task"], commitment_id=cid,
                             detail="you deleted the card in Notion",
                             external_url=row["external_url"])
            stats["forgotten"] += 1
    elif missing and suspicious:
        # Said out loud rather than swallowed: this is the case where doing
        # nothing is right but silence would look like the feature not working.
        stats["kept"] = [
            f"{len(missing)} page(s) missing out of {pushed} pushed — that looks "
            f"like a query problem, not a decision, so nothing was removed"
        ]

    return stats
