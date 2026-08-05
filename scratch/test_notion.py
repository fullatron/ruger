"""The Notion push/pull connector, against a fake Notion.

    .venv/bin/python scratch/test_notion.py

Costs nothing and needs no token: a stdlib HTTP server stands in for
api.notion.com and records every request, so the assertions can be about what
we *sent*, not only about what came back.

The load-bearing case is `push never re-sends Status`. Content flows out and
status flows back; if a push ever included Status it would drag a card out of
Done the moment you moved it, which is the same class of bug that
`sync.reextract_episode` exists to prevent.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp())

DB_ID = "11111111-1111-4111-8111-111111111111"
PARENT_ID = "22222222-2222-4222-8222-222222222222"

# Real env vars, not just .env: config.reload() would otherwise repoint the
# database at ~/.pkm and this test would chew on real data.
os.environ.update(
    PKM_DB=str(TMP / "t.db"), PKM_INBOX=str(TMP / "inbox"),
    PKM_TRASH=str(TMP / "trash"), PKM_ENV_FILE=str(TMP / ".env"),
    PKM_ME="Alex",
    PKM_NOTION_TOKEN="ntn_faketoken_for_tests", PKM_NOTION_DB=DB_ID,
)
for _k in ("PKM_PROVIDER", "PKM_MODEL", "PKM_API_KEY", "PKM_BASE_URL"):
    os.environ.pop(_k, None)

sys.path.insert(0, str(ROOT))

from pkm import config, db, extract, sync  # noqa: E402
from pkm.connectors import notion  # noqa: E402

config.ENV_FILE = TMP / ".env"
config.reload()

PASSES = {"n": 0}


def check(label, actual, expected):
    ok = actual == expected
    PASSES["n"] += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {actual!r}"
          + ("" if ok else f" != {expected!r}"))
    if not ok:
        raise SystemExit(1)


# --- the fake Notion ---------------------------------------------------------

STATE: dict = {}


def reset_state():
    STATE.clear()
    STATE.update({
        "pages": {},
        "databases": {DB_ID: {"id": DB_ID, "properties": dict(notion.SCHEMA)}},
        "patches": [],      # every PATCH body received
        "creates": [],      # every POST /pages body received
        "requests": [],     # (method, path)
        "next": 0,
        "fail_next": None,  # (status_code, body) served once, then cleared
        "title_prop": "Task",
    })
    STATE["databases"][DB_ID]["properties"]["Task"] = {"title": {}}


def new_id() -> str:
    STATE["next"] += 1
    return f"{STATE['next']:08d}-0000-4000-8000-000000000000"


def as_returned(name: str, spec: dict) -> dict:
    """A property as Notion *returns* it, not as you create it.

    Easy to get wrong in a double and it matters: `{"title": {}}` is the shape
    you POST, but a GET comes back carrying `id`, `name` and `type`, and
    `title_property` keys off `type`. A double that omits it would let a real
    bug through.
    """
    kind = next(iter(spec), "rich_text")
    return {"id": f"p{abs(hash(name)) % 10000:04d}", "name": name,
            "type": kind, **spec}


class Fake(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    # -- plumbing
    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _send(self, status: int, payload: dict, headers: dict | None = None):
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def _error(self, status: int, message: str):
        self._send(status, {"object": "error", "status": status,
                            "code": "test_error", "message": message})

    def _guard(self) -> bool:
        """Every real Notion call carries these two. Prove we send them."""
        if not (self.headers.get("Authorization") or "").startswith("Bearer "):
            self._error(401, "API token is invalid.")
            return False
        if self.headers.get("Notion-Version") != notion.VERSION:
            self._error(400, "Notion-Version header missing or unsupported.")
            return False
        return True

    def _intercept(self) -> bool:
        pending = STATE.get("fail_next")
        if not pending:
            return False
        STATE["fail_next"] = None
        status, headers = pending
        self._send(status, {"object": "error", "status": status,
                            "code": "rate_limited", "message": "slow down"},
                   headers)
        return True

    # -- routes
    def do_GET(self):
        STATE["requests"].append(("GET", self.path))
        if not self._guard() or self._intercept():
            return
        if self.path == "/v1/users/me":
            return self._send(200, {
                "object": "user", "name": "Ruger test",
                "bot": {"workspace_name": "Test workspace", "owner": {}},
            })
        if self.path.startswith("/v1/databases/"):
            dbid = self.path.rsplit("/", 1)[-1]
            database = STATE["databases"].get(dbid)
            if not database:
                return self._error(404, "Could not find database.")
            props = dict(database["properties"])
            # Model a renamed title column.
            if STATE["title_prop"] != "Task":
                props.pop("Task", None)
                props[STATE["title_prop"]] = {"title": {}}
            return self._send(200, {"object": "database", "id": dbid,
                                    "properties": {n: as_returned(n, s)
                                                   for n, s in props.items()}})
        return self._error(404, "no such route")

    def do_POST(self):
        body = self._body()
        STATE["requests"].append(("POST", self.path))
        if not self._guard() or self._intercept():
            return

        if self.path == "/v1/search":
            return self._send(200, {"results": [{
                "object": "page", "id": PARENT_ID,
                "url": f"https://notion.so/{PARENT_ID}",
                "properties": {"title": {"type": "title", "title": [
                    {"plain_text": "Work"}]}},
            }]})

        if self.path == "/v1/databases":
            dbid = new_id()
            STATE["databases"][dbid] = {"id": dbid,
                                        "properties": dict(body["properties"])}
            return self._send(200, {"object": "database", "id": dbid,
                                    "url": f"https://notion.so/{dbid}"})

        if self.path == "/v1/pages":
            STATE["creates"].append(body)
            pid = new_id()
            STATE["pages"][pid] = {
                "object": "page", "id": pid,
                "url": f"https://notion.so/{pid.replace('-', '')}",
                "archived": False,
                "database_id": body["parent"]["database_id"],
                "properties": dict(body["properties"]),
                "children": body.get("children", []),
            }
            return self._send(200, STATE["pages"][pid])

        if self.path.endswith("/query"):
            dbid = self.path.split("/")[3]
            live = [p for p in STATE["pages"].values()
                    if p["database_id"] == dbid and not p["archived"]]
            size = int(body.get("page_size") or 100)
            start = int(body.get("start_cursor") or 0)
            chunk = live[start:start + size]
            nxt = start + size
            return self._send(200, {
                "object": "list", "results": chunk,
                "has_more": nxt < len(live),
                "next_cursor": str(nxt) if nxt < len(live) else None,
            })

        return self._error(404, "no such route")

    def do_PATCH(self):
        body = self._body()
        STATE["requests"].append(("PATCH", self.path))
        if not self._guard() or self._intercept():
            return

        if self.path.startswith("/v1/pages/"):
            pid = self.path.rsplit("/", 1)[-1]
            page = STATE["pages"].get(pid)
            if not page:
                return self._error(404, "Could not find page.")
            STATE["patches"].append({"page": pid, "body": body})
            if "archived" in body:
                page["archived"] = bool(body["archived"])
            page["properties"].update(body.get("properties") or {})
            return self._send(200, page)

        if self.path.startswith("/v1/databases/"):
            dbid = self.path.rsplit("/", 1)[-1]
            database = STATE["databases"].get(dbid)
            if not database:
                return self._error(404, "Could not find database.")
            STATE["patches"].append({"database": dbid, "body": body})
            database["properties"].update(body.get("properties") or {})
            return self._send(200, {"object": "database", "id": dbid})

        return self._error(404, "no such route")


# --- local fixture -----------------------------------------------------------

NOTE = """---
title: Weekly with Maya
date: 2026-08-03
---

Alex: I'll audit all the team LinkedIn profiles by Friday.
Maya: I'll send you the Beacon login today.
Maya: I'll book the trade show banner next week.
"""

ITEMS = [
    {"task": "Audit the team LinkedIn profiles", "direction": "mine", "owner": "me",
     "due_date": "2026-08-07",
     "quote": "I'll audit all the team LinkedIn profiles by Friday.",
     "speaker": "Alex"},
    {"task": "Send the Beacon login", "direction": "theirs", "owner": "Maya",
     "due_date": None, "quote": "I'll send you the Beacon login today.",
     "speaker": "Maya"},
    {"task": "Book the trade show banner", "direction": "theirs", "owner": "Maya",
     "due_date": None, "quote": "I'll book the trade show banner next week.",
     "speaker": "Maya"},
]


def fake_extract(episode):
    kept, dropped = extract.validate({"commitments": ITEMS}, episode)
    return {"kept": kept, "dropped": dropped,
            "usage": {"model": "fake", "structured": "fake",
                      "input_tokens": 1, "output_tokens": 1}}


def status_of(page: dict) -> str | None:
    prop = (page.get("properties") or {}).get("Status") or {}
    return ((prop.get("select") or {}) or {}).get("name")


def prop_text(page: dict, name: str) -> str:
    prop = (page.get("properties") or {}).get(name) or {}
    items = prop.get("rich_text") or prop.get("title") or []
    return "".join(i.get("text", {}).get("content", "") for i in items)


# --- the tests ---------------------------------------------------------------


def test_id_parsing():
    print("config.notion_id — take whatever the user pasted:")
    check("bare 32 hex", config.notion_id("11111111111141118111111111111111"),
          "11111111-1111-4111-8111-111111111111")
    check("dashed uuid", config.notion_id(DB_ID), DB_ID)
    check("pasted url with a slug",
          config.notion_id("https://www.notion.so/me/Ruger-Board-"
                           "11111111111141118111111111111111"),
          "11111111-1111-4111-8111-111111111111")
    # The ?v= id is a *view*, a different object. Taking it would 404 forever.
    check("view id in the query string is ignored",
          config.notion_id("https://notion.so/me/Board-"
                           "11111111111141118111111111111111"
                           "?v=99999999999949998999999999999999"),
          "11111111-1111-4111-8111-111111111111")
    check("empty", config.notion_id(""), "")
    check("no id in it", config.notion_id("https://notion.so/my-page"), "")
    check("None", config.notion_id(None), "")


def test_property_mapping():
    print("\ncontent_properties — what push is allowed to own:")
    row = {"id": 7, "task": "Audit the profiles", "direction": "mine", "owner": "me",
           "due_date": "2026-08-07", "mention_count": 2, "quote": "I'll audit them.",
           "speaker": "Alex", "meeting": "Weekly", "meeting_date": "2026-08-03",
           "origin": "extracted", "status": "doing"}
    props = notion.content_properties(row)

    # The whole design in one assertion.
    check("Status is NOT in a content push", "Status" in props, False)
    check("title", props["Task"]["title"][0]["text"]["content"], "Audit the profiles")
    check("direction chip", props["Direction"]["select"]["name"], "Mine")
    check("due date", props["Due"]["date"], {"start": "2026-08-07"})
    check("mention count", props["Raised"]["number"], 2)
    check("ruger id", props["Ruger ID"]["number"], 7)
    check("evidence carries the quote and who said it",
          props["Evidence"]["rich_text"][0]["text"]["content"],
          "“I'll audit them.” — Alex")
    check("source", props["Source"]["select"]["name"], "Extracted")

    check("no due date clears rather than omits",
          notion.content_properties({**row, "due_date": None})["Due"]["date"], None)
    check("theirs",
          notion.content_properties({**row, "direction": "theirs"})["Direction"]["select"]["name"],
          "Theirs")
    check("a hand-added row says so",
          notion.content_properties({**row, "origin": "manual"})["Source"]["select"]["name"],
          "Added by hand")
    check("a hand-added row has no quote to show",
          notion.content_properties({**row, "origin": "manual", "quote": "",
                                     "speaker": None})["Evidence"]["rich_text"],
          [])

    print("\n  text is folded and truncated to Notion's per-item limit:")
    check("whitespace collapsed", notion._text("  a \n\n b  ")[0]["text"]["content"], "a b")
    long = notion._text("x" * 5000)[0]["text"]["content"]
    check("truncated", len(long), notion.TEXT_LIMIT)
    check("marked as truncated", long.endswith("…"), True)
    check("empty gives an empty list, not a blank item", notion._text(""), [])


def test_status_reading():
    print("\nread_status — tolerate a renamed column:")
    def page(name, kind="select"):
        return {"properties": {"Status": {kind: {"name": name}}}}

    check("To do", notion.read_status(page("To do")), "todo")
    check("In progress", notion.read_status(page("In progress")), "doing")
    check("Done", notion.read_status(page("Done")), "done")
    check("Not started", notion.read_status(page("Not started")), "todo")
    check("WIP", notion.read_status(page("WIP")), "doing")
    check("Completed", notion.read_status(page("Completed")), "done")
    check("case and padding", notion.read_status(page("  dOnE ")), "done")
    check("a real status-type property also works",
          notion.read_status(page("Done", "status")), "done")
    check("something we do not know is left alone, not guessed",
          notion.read_status(page("Blocked")), None)
    check("no status set at all", notion.read_status({"properties": {}}), None)

    print("\nread_ruger_id:")
    check("number", notion.read_ruger_id({"properties": {"Ruger ID": {"number": 12}}}), 12)
    check("absent", notion.read_ruger_id({"properties": {}}), None)
    check("cleared by hand",
          notion.read_ruger_id({"properties": {"Ruger ID": {"number": None}}}), None)


def test_push_and_pull(conn):
    print("\npush — first run creates a page per commitment:")
    stats = notion.push(conn)
    check("nothing failed", stats["failed"], 0)
    check("created", stats["created"], 3)
    check("updated", stats["updated"], 0)
    check("pages now in Notion", len(STATE["pages"]), 3)

    rows = db.commitments_to_push(conn)
    check("every row remembers its page", all(r["external_id"] for r in rows), True)
    check("and its url", all(r["external_url"] for r in rows), True)
    check("and when it went", all(r["pushed_at"] for r in rows), True)

    created = STATE["creates"][0]
    check("status IS set on create",
          "Status" in created["properties"], True)
    check("the body carries the quote as a block",
          created["children"][0]["type"], "quote")
    check("and a provenance line",
          "Weekly with Maya" in json.dumps(created["children"][1]), True)

    print("\n  a second push touches nothing at all (§12):")
    # Notion owns a page once it exists, so a re-push is not an update — it is a
    # skip. Before this, push re-sent content every run and reverted anything
    # edited in Notion within five minutes.
    STATE["creates"].clear()
    STATE["patches"].clear()
    stats = notion.push(conn)
    check("created", stats["created"], 0)
    check("updated", stats["updated"], 0)
    check("skipped instead", stats["skipped"], 3)
    check("no new pages", len(STATE["creates"]), 0)
    check("and no writes of any kind", len(STATE["patches"]), 0)
    check("page count unchanged", len(STATE["pages"]), 3)

    print("\n  an edit made in Notion survives a push:")
    page_id = next(iter(STATE["pages"]))
    title_prop = STATE["databases"][DB_ID]["title_prop"] if "title_prop" in \
        STATE["databases"][DB_ID] else "Task"
    STATE["pages"][page_id]["properties"]["Task"] = {
        "title": [{"type": "text", "text": {"content": "Renamed by hand in Notion"},
                   "plain_text": "Renamed by hand in Notion"}]}
    STATE["pages"][page_id]["properties"]["Status"] = {"select": {"name": "Done"}}
    notion.push(conn)
    kept = STATE["pages"][page_id]["properties"]["Task"]["title"][0]["plain_text"]
    check("the rename stuck", kept, "Renamed by hand in Notion")
    check("and so did the status", status_of(STATE["pages"][page_id]), "Done")

    print("\n  --resend is the deliberate override:")
    STATE["patches"].clear()
    stats = notion.push(conn, resend=True)
    check("every page rewritten", stats["updated"], 3)
    check("nothing skipped", stats["skipped"], 0)
    bodies = [p["body"].get("properties", {}) for p in STATE["patches"]]
    check("content went out", any("Task" in b for b in bodies), True)
    check("but even a resend leaves Status alone",
          any("Status" in b for b in bodies), False)
    check("so the card is still Done", status_of(STATE["pages"][page_id]), "Done")

    print("\n  ...except for an id an instruction explicitly moved (§11):")
    # `force_status` is the override D9 anticipated: a person said "mark it done",
    # so Notion is told. It has to stay this narrow — one id, asked for by name.
    moved = notion.read_ruger_id(STATE["pages"][page_id])
    db.set_status(conn, moved, "todo")
    STATE["patches"].clear()
    notion.push(conn, only={moved}, force_status={moved})
    bodies = [p["body"].get("properties", {}) for p in STATE["patches"]]
    check("one page was touched", len(bodies), 1)
    check("and this time Status went with it", "Status" in bodies[0], True)
    check("carrying the local value", status_of(STATE["pages"][page_id]), "To do")

    print("\n  a forced push still leaves every other card alone:")
    STATE["patches"].clear()
    notion.push(conn, force_status={moved})
    forced = [p for p in STATE["patches"]
              if "Status" in p["body"].get("properties", {})]
    check("only the named id carried Status", len(forced), 1)

    # Put back what the pull tests below expect to find: the card dragged to Done
    # in Notion while the local row still says todo.
    db.set_status(conn, moved, "todo")
    STATE["pages"][page_id]["properties"]["Status"] = {"select": {"name": "Done"}}

    print("\npull — Notion's status comes back:")
    stats = notion.pull(conn)
    check("pages read", stats["pages"], 3)
    check("one moved", stats["changed"], 1)
    check("the rest were already in step", stats["unchanged"], 2)
    moved_id = stats["moves"][0]["id"]
    check("from todo", stats["moves"][0]["from"], "todo")
    check("to done", stats["moves"][0]["to"], "done")
    check("stored locally", db.get_commitment(conn, moved_id)["status"], "done")

    print("\n  pulling again is a no-op:")
    stats = notion.pull(conn)
    check("nothing changed", stats["changed"], 0)
    check("all in step", stats["unchanged"], 3)

    print("\n  a status Ruger does not recognise is left alone, not reset:")
    STATE["pages"][page_id]["properties"]["Status"] = {"select": {"name": "Blocked"}}
    stats = notion.pull(conn)
    check("reported as unreadable", stats["unreadable"], 1)
    check("nothing changed", stats["changed"], 0)
    check("local status untouched",
          db.get_commitment(conn, moved_id)["status"], "done")
    STATE["pages"][page_id]["properties"]["Status"] = {"select": {"name": "Done"}}

    print("\n  dry runs touch nothing:")
    STATE["patches"].clear()
    STATE["creates"].clear()
    plan = notion.push(conn, dry_run=True)
    check("plan covers every row", len(plan["plan"]), 3)
    check("all of them skips, because Notion owns them now",
          {p["action"] for p in plan["plan"]}, {"skip"})
    check("no writes were sent", len(STATE["patches"]) + len(STATE["creates"]), 0)

    STATE["pages"][page_id]["properties"]["Status"] = {"select": {"name": "To do"}}
    stats = notion.pull(conn, dry_run=True)
    check("pull dry-run reports the move", stats["changed"], 1)
    check("but does not apply it",
          db.get_commitment(conn, moved_id)["status"], "done")
    STATE["pages"][page_id]["properties"]["Status"] = {"select": {"name": "Done"}}
    notion.pull(conn)


def test_edge_cases(conn):
    print("\na page added in Notion by hand is ignored, never adopted:")
    stray = new_id()
    STATE["pages"][stray] = {
        "object": "page", "id": stray, "url": "https://notion.so/stray",
        "archived": False, "database_id": DB_ID,
        "properties": {"Task": {"title": notion._text("Someone else's task")},
                       "Status": {"select": {"name": "To do"}}},
    }
    before = len(db.commitments_to_push(conn))
    stats = notion.pull(conn)
    check("counted as unlinked", stats["unlinked"], 1)
    check("no local row invented", len(db.commitments_to_push(conn)), before)

    print("\na page whose commitment is gone here:")
    rows = db.commitments_to_push(conn)
    victim = rows[-1]
    victim_page = victim["external_id"]
    with db.transaction(conn):
        db.delete_commitment(conn, int(victim["id"]))

    stats = notion.pull(conn)
    check("reported as orphaned", stats["orphaned"], 1)
    check("not archived without being asked", stats["archived"], 0)
    check("still live in Notion", STATE["pages"][victim_page]["archived"], False)

    stats = notion.pull(conn, prune=True)
    check("archived on --prune", stats["archived"], 1)
    check("gone from Notion", STATE["pages"][victim_page]["archived"], True)

    print("\na page deleted in Notion is reported, never deleted here:")
    rows = db.commitments_to_push(conn)
    gone = rows[0]
    STATE["pages"].pop(gone["external_id"])
    stats = notion.pull(conn)
    check("reported as missing", [m["id"] for m in stats["missing"]],
          [int(gone["id"])])
    check("kept locally", db.get_commitment(conn, int(gone["id"])) is not None, True)

    print("\nunlink — forget every page id, then rebuild:")
    with db.transaction(conn):
        cleared = db.clear_push_state(conn)
    check("links cleared", cleared, len(rows))
    check("no row has a page",
          any(r["external_id"] for r in db.commitments_to_push(conn)), False)
    STATE["pages"].clear()
    stats = notion.push(conn)
    check("recreated from scratch", stats["created"], len(rows))
    check("and nothing was updated", stats["updated"], 0)


def test_transport(conn):
    print("\ntransport — the failures worth retrying:")
    STATE["fail_next"] = (429, {"Retry-After": "0"})
    who = notion.whoami()
    check("429 is retried, not raised", who["workspace"], "Test workspace")

    STATE["fail_next"] = (503, {})
    check("503 is retried too", notion.whoami()["name"], "Ruger test")

    print("\n  and the ones that must not be retried:")
    STATE["fail_next"] = (400, {})
    try:
        notion.whoami()
        check("400 raised", False, True)
    except notion.NotionError as exc:
        check("400 surfaces its message", "slow down" in str(exc), True)

    print("\n  a missing token fails before any request:")
    saved = config.NOTION_TOKEN
    config.NOTION_TOKEN = ""
    try:
        notion.whoami()
        check("raised", False, True)
    except notion.NotionError as exc:
        check("says where to get one", "my-integrations" in str(exc), True)
    config.NOTION_TOKEN = saved

    print("\n  a missing database is a clear error, not a crash:")
    saved_db = config.NOTION_DB
    config.NOTION_DB = ""
    try:
        notion.push(conn)
        check("raised", False, True)
    except notion.NotionError as exc:
        check("names the fix", "notion setup" in str(exc), True)
    config.NOTION_DB = saved_db


def test_schema_and_pagination(conn):
    print("\nensure_schema — adopt a database that already exists:")
    thin = new_id()
    STATE["databases"][thin] = {"id": thin, "properties": {
        "Task": {"title": {}},
        # Already renamed and recoloured by hand: must survive untouched.
        "Status": {"select": {"options": [{"name": "Shipped", "color": "pink"}]}},
    }}
    added = notion.ensure_schema(thin)
    check("added the missing ones", "Owner" in added and "Ruger ID" in added, True)
    check("never tries to add a second title", "Task" in added, False)
    check("left the existing Status alone",
          STATE["databases"][thin]["properties"]["Status"]["select"]["options"][0]["name"],
          "Shipped")
    check("a second call adds nothing", notion.ensure_schema(thin), [])

    print("\n  a renamed title column is detected and used:")
    STATE["title_prop"] = "Name"
    check("found", notion.title_property(DB_ID), "Name")
    STATE["creates"].clear()
    with db.transaction(conn):
        db.clear_push_state(conn)
    STATE["pages"].clear()
    notion.push(conn)
    sent = STATE["creates"][0]["properties"]
    check("push used the real title column", "Name" in sent, True)
    check("and not the assumed one", "Task" in sent, False)
    STATE["title_prop"] = "Task"

    print("\nall_pages follows pagination:")
    saved = notion.PAGE_SIZE
    notion.PAGE_SIZE = 1
    try:
        check("every page came back", len(notion.all_pages(DB_ID)), len(STATE["pages"]))
    finally:
        notion.PAGE_SIZE = saved


def test_adopted_database(conn):
    """The shape a real Notion task template gives you, which is not ours.

    Title called "Name", Status a real `status` column whose options cannot be
    created over the API. Assuming our own shape here is a 400 per page.
    """
    print("\nadopting a database built by hand in Notion:")
    theirs = new_id()
    STATE["databases"][theirs] = {"id": theirs, "properties": {
        "Name": {"title": {}},
        "Status": {"status": {"options": [
            {"name": "Not started"}, {"name": "In progress"}, {"name": "Done"}]}},
        "Assign": {"people": {}},
    }}

    profile = notion.board_profile(theirs)
    check("title column found", profile["title"], "Name")
    check("status column found", profile["status_prop"], "Status")
    check("and its real type", profile["status_type"], "status")
    check("with its own vocabulary", profile["status_options"],
          ["Not started", "In progress", "Done"])

    print("\n  local status maps onto options that actually exist:")
    check("todo -> Not started, in status shape",
          notion.status_payload("todo", profile),
          {"Status": {"status": {"name": "Not started"}}})
    check("doing -> In progress",
          notion.status_payload("doing", profile)["Status"]["status"]["name"],
          "In progress")
    check("done -> Done",
          notion.status_payload("done", profile)["Status"]["status"]["name"], "Done")

    print("\n  a column with no usable option is skipped, not forced:")
    check("no status column at all",
          notion.status_payload("todo", {"status_prop": "", "status_type": ""}), None)
    check("options we cannot interpret",
          notion.status_payload("todo", {"status_prop": "Status",
                                         "status_type": "status",
                                         "status_options": ["Icebox", "Shipped"]}),
          None)

    print("\n  ensure_schema fills the gaps and leaves theirs alone:")
    added = notion.ensure_schema(theirs)
    props = STATE["databases"][theirs]["properties"]
    check("Ruger ID added", "Ruger ID" in added, True)
    check("Status left as their status type", props["Status"].get("status") is not None, True)
    check("their people column untouched", "Assign" in props, True)
    check("no second title added", "Task" in props, False)

    print("\n  and a real push fits itself to that database:")
    saved_db = config.NOTION_DB
    config.NOTION_DB = theirs
    STATE["creates"].clear()
    with db.transaction(conn):
        db.clear_push_state(conn)
    try:
        stats = notion.push(conn)
        check("nothing failed", stats["failed"], 0)
        sent = STATE["creates"][0]["properties"]
        check("used their title column", "Name" in sent, True)
        check("not ours", "Task" in sent, False)

        # Every card must open in the column matching its own local status,
        # spelled the way *this* database spells it.
        theirs_for = {"todo": "Not started", "doing": "In progress", "done": "Done"}
        opened = {c["properties"]["Ruger ID"]["number"]:
                  c["properties"]["Status"]["status"]["name"]
                  for c in STATE["creates"]}
        expected = {int(r["id"]): theirs_for[r["status"]]
                    for r in db.commitments_to_push(conn)}
        check("every card opened in the right column", opened, expected)
        check("and only ever in an option that exists",
              set(opened.values()) <= set(profile["status_options"]), True)
    finally:
        config.NOTION_DB = saved_db


def test_create_database():
    print("\ncreate_database:")
    created = notion.create_database(
        f"https://notion.so/Work-{PARENT_ID.replace('-', '')}", "Ruger — commitments")
    check("returns an id", bool(created["id"]), True)
    props = STATE["databases"][created["id"]]["properties"]
    check("has the full schema", set(props) >= set(notion.SCHEMA), True)
    check("mine is purple, theirs is green (D3 survives the trip)",
          [o["color"] for o in props["Direction"]["select"]["options"]],
          ["purple", "green"])

    try:
        notion.create_database("not a notion url")
        check("bad parent raised", False, True)
    except notion.NotionError as exc:
        check("bad parent rejected", "parent page id" in str(exc), True)

    print("\nshared_pages:")
    pages = notion.shared_pages()
    check("lists what the integration can see", pages[0]["title"], "Work")


def main() -> None:
    reset_state()
    notion.GAP = 0.0            # no need to be polite to a fake

    inbox = Path(os.environ["PKM_INBOX"])
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "weekly.md").write_text(NOTE, encoding="utf-8")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    notion.API = f"http://127.0.0.1:{httpd.server_address[1]}/v1"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"fake notion on {notion.API}\n")

    try:
        with closing(db.connect()) as conn:
            sync.run_sync(conn, extract_fn=fake_extract, inbox_path=inbox)
            check("fixture built", len(db.commitments_to_push(conn)), 3)

            test_id_parsing()
            test_property_mapping()
            test_status_reading()
            test_push_and_pull(conn)
            test_edge_cases(conn)
            test_transport(conn)
            test_schema_and_pagination(conn)
            test_adopted_database(conn)
            test_create_database()

            print("\nevery request carried auth and a pinned API version:")
            check("requests made", len(STATE["requests"]) > 20, True)
    finally:
        httpd.shutdown()
        httpd.server_close()

    print(f"\nOK — {PASSES['n']} assertions. Content goes out, status comes back, "
          f"and neither direction overwrites the other.")


if __name__ == "__main__":
    main()
