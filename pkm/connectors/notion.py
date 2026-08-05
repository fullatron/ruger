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

# Read back generously: renaming a column in Notion is a normal thing to do, and
# it must not silently stop status syncing. Anything unrecognised is left alone.
STATUS_ALIASES = {
    "to do": "todo", "todo": "todo", "to-do": "todo", "not started": "todo",
    "backlog": "todo", "open": "todo", "next": "todo", "inbox": "todo",
    "in progress": "doing", "doing": "doing", "in-progress": "doing",
    "wip": "doing", "started": "doing", "active": "doing",
    "done": "done", "complete": "done", "completed": "done", "shipped": "done",
    "closed": "done",
}

# D3's mine/theirs survives the trip as Notion's own chip colours.
SCHEMA = {
    TITLE_PROP: {"title": {}},
    STATUS_PROP: {"select": {"options": [
        {"name": "To do", "color": "default"},
        {"name": "In progress", "color": "blue"},
        {"name": "Done", "color": "green"},
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


def _prop(page: dict, name: str) -> dict:
    return (page.get("properties") or {}).get(name) or {}


def read_status(page: dict) -> str | None:
    """Which local status a Notion page is sitting in, if we can tell."""
    prop = _prop(page, STATUS_PROP)
    chosen = prop.get("select") or prop.get("status") or {}
    name = (chosen or {}).get("name") or ""
    return STATUS_ALIASES.get(name.strip().lower())


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
        pick = next((o for o in options
                     if STATUS_ALIASES.get(o.strip().lower()) == local), None)
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
         force_status: set[int] | None = None) -> dict:
    """Send every commitment to Notion: create what is missing, update the rest.

    Idempotent by construction — the Notion page id is stored on the row, so a
    second push with no local changes creates nothing.

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

    stats = {"total": len(rows), "created": 0, "updated": 0, "failed": 0,
             "errors": [], "plan": [], "url": ""}

    for row in rows:
        properties = content_properties(row)
        if title != TITLE_PROP:
            properties[title] = properties.pop(TITLE_PROP)

        existing = row["external_id"]
        action = "update" if existing else "create"
        stats["plan"].append({
            "id": int(row["id"]), "action": action, "task": row["task"],
            "owner": row["owner"], "status": row["status"],
            "due_date": row["due_date"], "url": row["external_url"],
        })
        if dry_run:
            stats["created" if action == "create" else "updated"] += 1
            continue

        try:
            if existing:
                if force_status and int(row["id"]) in force_status:
                    opening = status_payload(row["status"], profile)
                    if opening:
                        properties.update(opening)
                _request("PATCH", f"/pages/{existing}", {"properties": properties})
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
                stats["created"] += 1
        except NotionError as exc:
            # One bad row must not abandon the rest of the board.
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


def pull(conn: sqlite3.Connection, *, dry_run: bool = False,
         prune: bool = False) -> dict:
    """Bring Notion's status back, and report what has drifted.

    Notion wins on status, because that is where the human now works. Nothing
    else is read back: content stays owned by the extraction and by edits made
    here, so a stray keystroke in Notion cannot rewrite a verified quote.
    """
    pages = all_pages()
    local = {int(r["id"]): r for r in db.commitments_to_push(conn)}

    stats = {"pages": len(pages), "changed": 0, "unchanged": 0, "orphaned": 0,
             "archived": 0, "unlinked": 0, "unreadable": 0, "moves": [],
             "orphans": []}

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
            continue

        stats["changed"] += 1
        stats["moves"].append({"id": cid, "task": row["task"],
                               "from": row["status"], "to": status})
        if not dry_run:
            with db.transaction(conn):
                db.set_status(conn, cid, status)

    # A row whose page vanished from the database: the page was deleted in
    # Notion. Reported, never deleted here — the same rule refresh follows.
    stats["missing"] = [
        {"id": cid, "task": row["task"]}
        for cid, row in local.items()
        if row["external_id"] and cid not in seen
    ]
    return stats
