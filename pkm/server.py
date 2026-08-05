"""Single localhost page. Python stdlib http.server — no Node, no build step.

    GET    /                  board.html
    GET    /api/tasks         all commitments with meeting + evidence joined
    PATCH  /api/tasks/{id}    {"status": "todo"|"doing"|"done"} — drag or checkbox
    POST   /api/sync          inbox ingest -> extraction -> return count

    POST   /api/tasks         create a task by hand, attached to a meeting
    DELETE /api/tasks/{id}    remove a task

    POST   /api/notes         {title, date, participants, body} -> store + extract
    POST   /api/uploads       {files:[{filename, content}]}     -> store + extract
    GET    /api/notes         everything stored, with what it produced
    GET    /api/notes/{id}    one source: transcript, its tasks, its drops
    POST   /api/notes/{id}/reextract   run extraction again and MERGE
    DELETE /api/notes/{id}    forget a note (file moves to ~/.pkm/trash)

    GET    /api/version       API version, so a stale server can be detected
    GET    /api/config        current provider settings (key masked)
    PUT    /api/config        save provider settings to .env
    POST   /api/config/test   spend one tiny call to prove they work
    GET    /api/config/models list models an OpenAI-compatible endpoint serves
    POST   /api/config/reveal return the stored key in clear text (guarded)

Security. The server binds 127.0.0.1, but "localhost" is not a trust boundary:
any page in your browser can POST to it. Since this process holds API keys,
every mutating request must carry `X-Ruger: 1`. A cross-origin page cannot set
a custom header without a CORS preflight, and this server approves none — so
those requests never arrive. Same-origin requests from board.html are unaffected.
"""

from __future__ import annotations

import json
import re
import traceback
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config, db, notes, settings, sync

BOARD_HTML = Path(__file__).resolve().parent / "board.html"
_TASK_PATH = re.compile(r"^/api/tasks/(\d+)$")
_NOTE_PATH = re.compile(r"^/api/notes/(\d+)$")
_NOTE_REEXTRACT = re.compile(r"^/api/notes/(\d+)/reextract$")
MAX_BODY = 8 * 1024 * 1024      # uploads are inlined as JSON

# board.html is read from disk on every request, so a long-running server
# serves the NEWEST page while still routing on the code it started with. The
# page then calls endpoints that do not exist yet and fails in confusing ways.
# Bump this whenever routes change; the page compares it and says so plainly.
API_VERSION = "0.3"
GUARD_HEADER = "X-Ruger"


class Handler(BaseHTTPRequestHandler):
    server_version = "ruger/0.2"
    protocol_version = "HTTP/1.1"

    # --- plumbing ---------------------------------------------------------

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ValueError(f"body too large (max {MAX_BODY // 1024 // 1024} MB)")
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("body must be a JSON object")
        return payload

    def _guarded(self) -> bool:
        """Reject cross-origin writes. See the module docstring."""
        if self.headers.get(GUARD_HEADER) != "1":
            self._error(403, f"missing {GUARD_HEADER} header — refusing a cross-origin write")
            return False
        origin = self.headers.get("Origin")
        if origin:
            host = urlparse(origin).netloc
            if host not in (self.headers.get("Host"), f"127.0.0.1:{config.SERVER_PORT}",
                            f"localhost:{config.SERVER_PORT}"):
                self._error(403, f"cross-origin request from {origin} refused")
                return False
        return True

    def log_message(self, fmt: str, *args) -> None:
        print(f"  {self.command} {self.path.split('?')[0]} -> {args[1] if len(args) > 1 else ''}")

    def _guard_errors(self, fn):
        try:
            fn()
        except (ValueError, json.JSONDecodeError) as exc:
            self._error(400, str(exc))
        except notes.NoteError as exc:
            self._error(400, str(exc))
        except Exception:
            traceback.print_exc()
            self._error(500, "internal error — see the terminal for the traceback")

    # --- GET --------------------------------------------------------------

    def do_GET(self) -> None:
        self._guard_errors(self._do_get)

    do_HEAD = do_GET

    def _do_get(self) -> None:
        path = urlparse(self.path).path
        query = parse_qs(urlparse(self.path).query)

        if path in ("/", "/index.html", "/board.html"):
            if not BOARD_HTML.exists():
                return self._error(500, f"missing {BOARD_HTML.name}")
            return self._send(200, BOARD_HTML.read_bytes(), "text/html; charset=utf-8")

        if path == "/api/tasks":
            with closing(db.connect()) as conn:
                return self._json(200, {"tasks": db.all_tasks(conn)})

        if path == "/api/notes":
            with closing(db.connect()) as conn:
                return self._json(200, {
                    "notes": notes.list_notes(conn),
                    "inbox": str(config.INBOX),
                })

        match = _NOTE_PATH.match(path)
        if match:
            with closing(db.connect()) as conn:
                return self._json(200, notes.note_detail(conn, int(match.group(1))))

        if path == "/api/version":
            return self._json(200, {"api": API_VERSION})

        if path == "/api/config":
            return self._json(200, settings.current())

        if path == "/api/config/models":
            return self._json(200, _list_models(query.get("q", [""])[0]))

        self._error(404, "not found")

    # --- PATCH ------------------------------------------------------------

    def do_PATCH(self) -> None:
        if not self._guarded():
            return
        self._guard_errors(self._do_patch)

    def _do_patch(self) -> None:
        match = _TASK_PATH.match(urlparse(self.path).path)
        if not match:
            return self._error(404, "not found")

        payload = self._read_json()
        task_id = int(match.group(1))
        status = payload.get("status")
        content = {k: payload[k] for k in db.EDITABLE_FIELDS if k in payload}

        if status is not None and status not in ("todo", "doing", "done"):
            return self._error(400, "status must be todo, doing or done")
        if content.get("direction") not in (None, "mine", "theirs"):
            return self._error(400, "direction must be mine or theirs")
        if "task" in content and not str(content["task"]).strip():
            return self._error(400, "the task cannot be empty")
        if status is None and not content:
            return self._error(400, "nothing to change")

        with closing(db.connect()) as conn:
            if db.get_commitment(conn, task_id) is None:
                return self._error(404, "no such task")
            with db.transaction(conn):
                if content:
                    db.update_commitment(conn, task_id, content)
                if status is not None:
                    db.set_status(conn, task_id, status)
            task = next((t for t in db.all_tasks(conn) if t["id"] == task_id), None)
        self._json(200, {"task": task})

    # --- PUT --------------------------------------------------------------

    def do_PUT(self) -> None:
        if not self._guarded():
            return
        self._guard_errors(self._do_put)

    def _do_put(self) -> None:
        if urlparse(self.path).path != "/api/config":
            return self._error(404, "not found")
        result = settings.apply(self._read_json())
        self._json(200 if result.get("ok") else 400, result)

    # --- DELETE -----------------------------------------------------------

    def do_DELETE(self) -> None:
        if not self._guarded():
            return
        self._guard_errors(self._do_delete)

    def _do_delete(self) -> None:
        path = urlparse(self.path).path

        match = _TASK_PATH.match(path)
        if match:
            with closing(db.connect()) as conn:
                with db.transaction(conn):
                    if not db.delete_commitment(conn, int(match.group(1))):
                        return self._error(404, "no such task")
            return self._json(200, {"deleted": int(match.group(1))})

        match = _NOTE_PATH.match(path)
        if not match:
            return self._error(404, "not found")
        with closing(db.connect()) as conn:
            self._json(200, notes.delete_note(conn, int(match.group(1))))

    # --- POST -------------------------------------------------------------

    def do_POST(self) -> None:
        if not self._guarded():
            return
        self._guard_errors(self._do_post)

    def _do_post(self) -> None:
        path = urlparse(self.path).path

        if path == "/api/sync":
            print("  running sync (ingest -> extraction)...")
            with closing(db.connect()) as conn:
                return self._json(200, sync.run_sync(conn, verbose=True))

        if path == "/api/notes":
            payload = self._read_json()
            saved = notes.save_note(
                title=payload.get("title", ""),
                body=payload.get("body", ""),
                when=payload.get("date"),
                participants=payload.get("participants") or [],
            )
            print(f"  stored {saved.name}; extracting...")
            with closing(db.connect()) as conn:
                result = notes.ingest_paths(conn, [saved])
                result["saved"] = [saved.name]
                result["tasks"] = db.all_tasks(conn)
            return self._json(200, result)

        if path == "/api/uploads":
            payload = self._read_json()
            files = payload.get("files") or []
            if not isinstance(files, list) or not files:
                return self._error(400, "no files given")
            if len(files) > 50:
                return self._error(400, "too many files at once (max 50)")

            saved, problems = [], []
            for item in files:
                if not isinstance(item, dict):
                    continue
                try:
                    saved.append(
                        notes.save_upload(item.get("filename", ""), item.get("content", ""))
                    )
                except notes.NoteError as exc:
                    problems.append(str(exc))

            if not saved:
                return self._error(400, "; ".join(problems) or "nothing could be stored")

            print(f"  stored {len(saved)} file(s); extracting...")
            with closing(db.connect()) as conn:
                result = notes.ingest_paths(conn, saved)
                result["saved"] = [p.name for p in saved]
                result["problems"] = list(result["problems"]) + problems
                result["tasks"] = db.all_tasks(conn)
            return self._json(200, result)

        if path == "/api/tasks":
            payload = self._read_json()
            episode_id = payload.get("episode_id")
            if not isinstance(episode_id, int):
                return self._error(400, "episode_id is required")
            with closing(db.connect()) as conn:
                task_id = notes.add_task(conn, episode_id, payload)
                task = next((t for t in db.all_tasks(conn) if t["id"] == task_id), None)
            return self._json(200, {"task": task})

        match = _NOTE_REEXTRACT.match(path)
        if match:
            episode_id = int(match.group(1))
            with closing(db.connect()) as conn:
                episode = conn.execute(
                    "SELECT * FROM episodes WHERE id = ?", (episode_id,)
                ).fetchone()
                if episode is None:
                    return self._error(404, "no such note")
                print(f"  re-extracting {episode['title']!r}...")
                result = sync.reextract_episode(conn, episode)
                if result.get("error"):
                    return self._error(502, result["error"])
                result["detail"] = notes.note_detail(conn, episode_id)
            return self._json(200, result)

        if path == "/api/config/test":
            return self._json(200, settings.test_credentials())

        if path == "/api/config/reveal":
            # Guarded POST rather than a GET field: a plain GET is reachable by
            # any page in the browser, and this returns the key in clear text.
            return self._json(200, {"api_key": config.API_KEY or ""})

        self._error(404, "not found")


def _list_models(pattern: str) -> dict:
    """Model ids from an OpenAI-compatible endpoint, for the settings page."""
    if config.PROVIDER == "anthropic":
        return {"models": config.PROVIDER_PRESETS["anthropic"]["models"], "total": 3}
    try:
        from openai import OpenAI

        client = OpenAI(api_key=config.API_KEY, base_url=config.BASE_URL or None, timeout=60.0)
        ids = sorted(m.id for m in client.models.list().data)
    except Exception as exc:
        return {"error": " ".join(str(exc).split())[:200], "models": []}

    matches = [i for i in ids if pattern.lower() in i.lower()] if pattern else ids
    return {"models": matches[:200], "total": len(ids), "matched": len(matches)}


def serve(host: str | None = None, port: int | None = None) -> None:
    host = host or config.SERVER_HOST
    port = port or config.SERVER_PORT
    with closing(db.connect()) as conn:  # create the DB up front so /api/tasks never 500s
        count = conn.execute("SELECT COUNT(*) AS n FROM commitments").fetchone()["n"]

    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"ruger  http://{host}:{port}   ({count} tasks, db: {config.DB_PATH})")
    print("Ctrl-C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        httpd.server_close()
