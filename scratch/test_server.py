"""Step 3 proof: the three endpoints, over real HTTP, against a temp database.

Asserts §7's "checking a box survives a refresh" — the board keeps no state of
its own (D6), so a PATCH followed by a fresh GET is the whole test.

    .venv/bin/python scratch/test_server.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pkm import config, db, server, sync  # noqa: E402
from test_extraction import WEEK_1, WEEK_2, fake_extract  # noqa: E402

BASE = None


def request(method: str, path: str, payload=None):
    body = json.dumps(payload).encode() if payload is not None else None
    # Mutations must carry the guard header; see server.py's module docstring.
    headers = {} if method == "GET" else {"X-Ruger": "1"}
    if body:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            raw = res.read()
            ctype = res.headers.get("Content-Type", "")
            return res.status, (json.loads(raw) if "json" in ctype else raw.decode())
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


def main() -> None:
    global BASE
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        inbox = root / "inbox"
        inbox.mkdir()
        (inbox / "week1.md").write_text(WEEK_1, encoding="utf-8")
        (inbox / "week2.md").write_text(WEEK_2, encoding="utf-8")

        # Point the app at a throwaway db and inbox — never the real ~/.pkm.
        config.DB_PATH = root / "test.db"
        config.INBOX = inbox
        config.ME_ALIASES = ["Alex", "me", "I"]

        with closing(db.connect()) as conn:
            seeded = sync.run_sync(conn, extract_fn=fake_extract, inbox_path=inbox)
        check("seeded board", seeded["tasks"], 3)

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        BASE = f"http://127.0.0.1:{httpd.server_address[1]}"
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        print(f"\nserver on {BASE}")

        try:
            print("\nGET /")
            status, html = request("GET", "/")
            check("status", status, 200)
            check("serves the board", "<title>Ruger" in html, True)
            check("no fake TASKS constant left in the page", "const TASKS = [{" in html, False)
            check("fetches tasks from the api", '"/api/tasks"' in html and "fetch(path" in html, True)

            print("\nGET /api/tasks")
            status, body = request("GET", "/api/tasks")
            check("status", status, 200)
            tasks = body["tasks"]
            check("task count", len(tasks), 3)

            first = tasks[0]
            for field in ("id", "task", "direction", "owner", "due_date", "quote",
                          "speaker", "status", "mention_count", "mentions",
                          "meeting", "meeting_date", "history"):
                if field not in first:
                    check(f"field {field} present", False, True)
            print(f"        card face fields present; ordered repeats-first: "
                  f"{[t['mention_count'] for t in tasks]}")
            check("evidence joined (quote non-empty)", all(t["quote"] for t in tasks), True)
            check("meeting joined", all(t["meeting"] for t in tasks), True)
            check("repeated card carries 2 history entries",
                  len(next(t for t in tasks if t["mention_count"] == 2)["history"]), 2)

            print("\nPATCH /api/tasks/{id}")
            target = tasks[0]["id"]
            status, body = request("PATCH", f"/api/tasks/{target}", {"status": "doing"})
            check("status", status, 200)
            check("returns the updated task", body["task"]["status"], "doing")

            print("\nGET /api/tasks again (the refresh)")
            _, body = request("GET", "/api/tasks")
            moved = next(t for t in body["tasks"] if t["id"] == target)
            check("checkbox survives a refresh", moved["status"], "doing")

            status, _ = request("PATCH", f"/api/tasks/{target}", {"status": "done"})
            check("done accepted", status, 200)
            _, body = request("GET", "/api/tasks")
            check("done persisted",
                  next(t for t in body["tasks"] if t["id"] == target)["status"], "done")

            print("\nrejections")
            status, body = request("PATCH", f"/api/tasks/{target}", {"status": "banana"})
            check("bad status -> 400", status, 400)
            status, _ = request("PATCH", "/api/tasks/999999", {"status": "todo"})
            check("unknown id -> 404", status, 404)
            status, _ = request("GET", "/api/nope")
            check("unknown route -> 404", status, 404)

            print("\nPOST /api/sync (no new files, nothing to extract)")
            status, body = request("POST", "/api/sync")
            check("status", status, 200)
            check("returns a count", body["tasks"], 3)
            check("nothing re-extracted", body["extracted"], 0)
            check("no duplicates", len(request("GET", "/api/tasks")[1]["tasks"]), 3)
        finally:
            httpd.shutdown()
            httpd.server_close()

        print("\nOK — board served, evidence joined, status persists, sync is idempotent.")


if __name__ == "__main__":
    main()
