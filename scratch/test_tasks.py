"""Manual tasks, editing, deleting, and merge-refresh.

    .venv/bin/python scratch/test_tasks.py

The refresh cases are the important ones: re-extraction must not undo work
already done on the board.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp())
os.environ.update(PKM_DB=str(TMP / "t.db"), PKM_INBOX=str(TMP / "inbox"),
                  PKM_TRASH=str(TMP / "trash"), PKM_ENV_FILE=str(TMP / ".env"),
                  PKM_ME="Alex")
for _k in ("PKM_PROVIDER", "PKM_MODEL", "PKM_API_KEY", "PKM_BASE_URL"):
    os.environ.pop(_k, None)

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scratch"))

from pkm import config, db, extract, notes, server, sync  # noqa: E402

config.ENV_FILE = TMP / ".env"
config.reload()

BASE = None

NOTE = """---
title: Weekly with Maya
date: 2026-08-03
id: t1
---

Alex: I'll audit all the team LinkedIn profiles by Friday.
Maya: I'll send you the Beacon login today.
Maya: I'll book the trade show banner next week.
"""

# Two extraction rounds. The second finds one more commitment and rewords one.
ROUND = {"n": 0}
ROUNDS = [
    [
        {"task": "Audit the team LinkedIn profiles", "direction": "mine", "owner": "me",
         "due_date": "2026-08-07",
         "quote": "I'll audit all the team LinkedIn profiles by Friday.", "speaker": "Alex"},
        {"task": "Send the Beacon login", "direction": "theirs", "owner": "Maya",
         "due_date": None,
         "quote": "I'll send you the Beacon login today.", "speaker": "Maya"},
    ],
    [
        # same first task, reworded and with a date
        {"task": "Audit all team LinkedIn profiles and align messaging", "direction": "mine",
         "owner": "me", "due_date": "2026-08-07",
         "quote": "I'll audit all the team LinkedIn profiles by Friday.", "speaker": "Alex"},
        {"task": "Send the Beacon login", "direction": "theirs", "owner": "Maya",
         "due_date": "2026-08-03",
         "quote": "I'll send you the Beacon login today.", "speaker": "Maya"},
        # newly found on the second pass
        {"task": "Book the trade show banner", "direction": "theirs", "owner": "Maya",
         "due_date": None,
         "quote": "I'll book the trade show banner next week.", "speaker": "Maya"},
    ],
]


def fake_extract(episode):
    items = ROUNDS[min(ROUND["n"], len(ROUNDS) - 1)]
    kept, dropped = extract.validate({"commitments": items}, episode)
    return {"kept": kept, "dropped": dropped,
            "usage": {"model": "fake", "structured": "fake",
                      "input_tokens": 1, "output_tokens": 1}}


def request(method, path, payload=None):
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {} if method == "GET" else {"X-Ruger": "1"}
    if body:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            raw = res.read()
            return res.status, (json.loads(raw) if "json" in res.headers.get("Content-Type", "") else raw.decode())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw.decode()


def check(label, actual, expected):
    ok = actual == expected
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {actual!r}" + ("" if ok else f" != {expected!r}"))
    if not ok:
        raise SystemExit(1)


def by_task(tasks, needle):
    return next((t for t in tasks if needle.lower() in t["task"].lower()), None)


def main() -> None:
    global BASE
    original = sync.reextract_episode

    def stubbed(conn, episode, **kw):
        kw.setdefault("extract_fn", fake_extract)
        return original(conn, episode, **kw)

    sync.reextract_episode = stubbed

    inbox = Path(os.environ["PKM_INBOX"])
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "t1.md").write_text(NOTE, encoding="utf-8")
    with closing(db.connect()) as conn:
        sync.run_sync(conn, extract_fn=fake_extract, inbox_path=inbox)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    BASE = f"http://127.0.0.1:{httpd.server_address[1]}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"server on {BASE}\n")

    try:
        print("GET /api/notes/{id} — the source, its transcript and its tasks:")
        note_id = request("GET", "/api/notes")[1]["notes"][0]["id"]
        status, detail = request("GET", f"/api/notes/{note_id}")
        check("status", status, 200)
        check("transcript returned verbatim",
              "I'll book the trade show banner next week." in detail["note"]["transcript"], True)
        check("its tasks come with it", len(detail["tasks"]), 2)
        check("participants parsed to a list", isinstance(detail["note"]["participants"], list), True)
        status, _ = request("GET", "/api/notes/999999")
        check("unknown source -> 400", status, 400)

        print("\nPOST /api/tasks — adding one by hand:")
        status, body = request("POST", "/api/tasks", {
            "episode_id": note_id, "task": "Chase Iris about the office",
            "direction": "theirs", "owner": "Iris", "due_date": "2026-08-12"})
        check("status", status, 200)
        manual = body["task"]
        check("marked manual", manual["origin"], "manual")
        check("no quote, because nobody said it", manual["quote"], "")
        check("owner kept", manual["owner"], "Iris")
        check("shows on the board", len(request("GET", "/api/tasks")[1]["tasks"]), 3)

        print("\n  rejections:")
        check("empty task", request("POST", "/api/tasks",
              {"episode_id": note_id, "task": "  "})[0], 400)
        check("theirs with no owner", request("POST", "/api/tasks",
              {"episode_id": note_id, "task": "x", "direction": "theirs"})[0], 400)
        check("bad direction", request("POST", "/api/tasks",
              {"episode_id": note_id, "task": "x", "direction": "sideways"})[0], 400)
        check("unknown meeting", request("POST", "/api/tasks",
              {"episode_id": 999999, "task": "x"})[0], 400)
        check("no episode_id", request("POST", "/api/tasks", {"task": "x"})[0], 400)

        print("\nthe board cannot edit anything any more (§12):")
        tasks = request("GET", "/api/tasks")[1]["tasks"]
        audit = by_task(tasks, "audit")
        for field in ({"status": "doing"}, {"task": "Something else"},
                      {"due_date": "2026-08-20"}, {"owner": "Theo"}):
            check(f"PATCH {list(field)[0]} refused",
                  request("PATCH", f"/api/tasks/{audit['id']}", field)[0], 405)
        after = by_task(request("GET", "/api/tasks")[1]["tasks"], "audit")
        check("nothing changed", (after["status"], after["task"]),
              (audit["status"], audit["task"]))

        print("\n  editing still exists, but it belongs to an instruction now:")
        # `update_commitment` is what `instruct.py` calls. The refresh-protection
        # tests below depend on a row being human-edited, and this is the path
        # that now sets that flag.
        with closing(db.connect()) as conn:
            with db.transaction(conn):
                db.update_commitment(conn, audit["id"], {
                    "task": "Audit the LinkedIn profiles myself",
                    "due_date": "2026-08-20"})
                db.set_status(conn, audit["id"], "doing")
            row = db.get_commitment(conn, audit["id"])
            check("text changed", row["task"], "Audit the LinkedIn profiles myself")
            check("flagged as edited", row["edited"], 1)
            check("due date changed", row["due_date"], "2026-08-20")
            check("status moved", row["status"], "doing")

        print("\n  a renamed task still dedups (derived keys moved with it):")
        with closing(db.connect()) as conn:
            row = db.get_commitment(conn, audit["id"])
            check("task_norm recomputed", "linkedin" in row["task_norm"], True)
            check("owner_norm recomputed", row["owner_norm"], "me")

        print("\nPOST /api/notes/{id}/reextract — refresh must not undo my work:")
        ROUND["n"] = 1
        status, r = request("POST", f"/api/notes/{note_id}/reextract")
        check("status", status, 200)
        check("the new commitment was added", r["created"], 1)
        check("my edited task kept its wording", r["protected"], 1)
        check("the untouched one was updated", r["updated"], 1)

        after = request("GET", "/api/tasks")[1]["tasks"]
        check("board grew by exactly one", len(after), 4)
        edited = next(t for t in after if t["id"] == audit["id"])
        check("edited wording survived", edited["task"], "Audit the LinkedIn profiles myself")
        check("edited status survived", edited["status"], "doing")
        check("my due date survived", edited["due_date"], "2026-08-20")
        check("its evidence was refreshed", edited["quote"].startswith("I'll audit all"), True)

        beacon = by_task(after, "beacon")
        check("unedited task took the model's new date", beacon["due_date"], "2026-08-03")
        check("manual task untouched by refresh",
              by_task(after, "iris")["task"], "Chase Iris about the office")
        check("manual task still manual", by_task(after, "iris")["origin"], "manual")
        check("newly found task present", by_task(after, "banner") is not None, True)

        print("\n  refreshing again changes nothing:")
        status, r2 = request("POST", f"/api/notes/{note_id}/reextract")
        check("nothing created", r2["created"], 0)
        check("board size stable", len(request("GET", "/api/tasks")[1]["tasks"]), 4)
        check("still one protected", r2["protected"], 1)

        print("\n  a task the model stops returning is kept, not deleted:")
        ROUND["n"] = 0          # back to the two-item round
        status, r3 = request("POST", f"/api/notes/{note_id}/reextract")
        check("reported as no longer found", r3["unmatched"], 1)
        check("but still on the board", len(request("GET", "/api/tasks")[1]["tasks"]), 4)

        print("\nDELETE /api/tasks/{id}:")
        victim = by_task(request("GET", "/api/tasks")[1]["tasks"], "banner")
        check("deleted", request("DELETE", f"/api/tasks/{victim['id']}")[0], 200)
        check("gone from the board", len(request("GET", "/api/tasks")[1]["tasks"]), 3)
        check("unknown id -> 404", request("DELETE", "/api/tasks/999999")[0], 404)
        with closing(db.connect()) as conn:
            check("its mentions went too",
                  conn.execute("SELECT COUNT(*) AS n FROM commitment_mentions WHERE commitment_id = ?",
                               (victim["id"],)).fetchone()["n"], 0)

        print("\nPOST /api/config/reveal:")
        status, body = request("POST", "/api/config/reveal")
        check("status", status, 200)
        check("returns a field", "api_key" in body, True)
        check("GET /api/config still hides it", "api_key" in request("GET", "/api/config")[1], False)

        print("\nDELETE /api/notes/{id}/tasks — a note that was never my work (§17):")
        # The case: feedback on somebody else's product reads exactly like a list
        # of things to do, and one recording put seven of them on the board.
        before = len(request("GET", "/api/tasks")[1]["tasks"])
        check("there are tasks to clear", before > 0, True)

        archived: list[str] = []
        from pkm.connectors import notion as notion_mod

        original_request = notion_mod._request

        def fake_request(method, path, payload=None, **kw):
            if method == "PATCH" and payload == {"archived": True}:
                archived.append(path)
                return {}
            raise AssertionError(f"unexpected call {method} {path}")

        # Pretend the rows reached Notion, so the archive path is exercised.
        with closing(db.connect()) as conn:
            with db.transaction(conn):
                for row in db.commitments_for_episode(conn, note_id):
                    db.mark_pushed(conn, int(row["id"]), f"page-{row['id']}",
                                   f"https://notion.so/{row['id']}")

        notion_mod._request = fake_request
        try:
            status, r = request("DELETE", f"/api/notes/{note_id}/tasks")
        finally:
            notion_mod._request = original_request

        check("status", status, 200)
        check("every task went", r["deleted"], before)
        check("and every Notion page was archived", r["archived"], before)
        check("each archive call named a page", len(archived), before)
        check("and every one was a page path",
              all(p.startswith("/pages/page-") for p in archived), True)
        check("the board is empty", len(request("GET", "/api/tasks")[1]["tasks"]), 0)

        print("\n  the note is kept, and muted so it is not read again:")
        check("the note survives", r["detail"]["note"]["title"] is not None, True)
        check("its transcript survives",
              "audit" in r["detail"]["note"]["transcript"], True)
        check("it is muted", r["muted"], True)
        with closing(db.connect()) as conn:
            check("and muted in the database",
                  db.get_commitment(conn, 1) is None
                  and conn.execute("SELECT muted FROM episodes WHERE id = ?",
                                   (note_id,)).fetchone()["muted"], 1)
            check("so a changed transcript does not hand them back",
                  [int(e["id"]) for e in db.episodes_needing_extraction(conn)], [])

        print("\n  and the log records every one:")
        # Only the bulk ones: an earlier section of this file deletes a task too.
        events = [e for e in request("GET", "/api/events")[1]["events"]
                  if e["action"] == "deleted" and "not my work" in (e["detail"] or "")]
        check("one event per task", len(events), before)
        check("naming the note it came from",
              "cleared from" in events[0]["detail"], True)

        print("\n  refreshing un-mutes it, because you asked for it to be read:")
        ROUND["n"] = 0
        request("POST", f"/api/notes/{note_id}/reextract")
        with closing(db.connect()) as conn:
            check("no longer muted",
                  conn.execute("SELECT muted FROM episodes WHERE id = ?",
                               (note_id,)).fetchone()["muted"], 0)

        print("\npage wiring:")
        html = request("GET", "/")[1]
        for needle in ("data-source=", "src-refresh", "src-add", "provider-pick",
                       "s-reveal", "transcript", "data-del-task", "/api/events",
                       "ev-task", "day-head"):
            check(f"page references {needle}", needle in html, True)

        # The removed half. `pk-status` is deliberately not checked here: it still
        # matches `pk-status-msg`, so it would pass while meaning nothing.
        for gone in ('data-check="', 'class="drop"', 'draggable="true"',
                     'id="pk-due"', 'data-group='):
            check(f"page no longer has {gone}", gone in html, False)
    finally:
        sync.reextract_episode = original
        httpd.shutdown()
        httpd.server_close()

    print("\nOK — manual tasks, editing, deleting, and a refresh that preserves work.")


if __name__ == "__main__":
    main()
