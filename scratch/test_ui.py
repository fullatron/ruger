"""The UI-facing endpoints: note ingest, sources, settings, and the CSRF guard.

    .venv/bin/python scratch/test_ui.py

Everything runs against a temp inbox, temp database and temp .env. Those are set
as real environment variables on purpose: saving settings calls config.reload(),
and process env beats the .env file — without this the reload would repoint the
app at the real ~/.pkm database mid-test.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp())
os.environ["PKM_DB"] = str(TMP / "ui.db")
os.environ["PKM_INBOX"] = str(TMP / "inbox")
os.environ["PKM_TRASH"] = str(TMP / "trash")
os.environ["PKM_ENV_FILE"] = str(TMP / ".env")
os.environ.pop("PKM_PROVIDER", None)
os.environ.pop("PKM_MODEL", None)
os.environ.pop("PKM_API_KEY", None)
os.environ.pop("PKM_BASE_URL", None)

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scratch"))

from pkm import config, notes, server, settings  # noqa: E402
from pkm.providers import parse_json_object  # noqa: E402

config.ENV_FILE = TMP / ".env"
config.reload()

BASE = None


def request(method, path, payload=None, headers=None):
    body = json.dumps(payload).encode() if payload is not None else None
    hdrs = {"X-Ruger": "1"} if method != "GET" else {}
    if body:
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    req = urllib.request.Request(BASE + path, data=body, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
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


NOTE_BODY = (
    "Alex: I'll audit all the team LinkedIn profiles by Friday.\n"
    "Maya: I'll send you the Beacon login today.\n"
)


def fake_extract(episode):
    """Stand in for the model: echo back two commitments plus one hallucination."""
    from pkm import extract

    raw = {"commitments": [
        {"task": "Audit the team LinkedIn profiles", "direction": "mine", "owner": "me",
         "due_date": "2026-08-07",
         "quote": "I'll audit all the team LinkedIn profiles by Friday.", "speaker": "Alex"},
        {"task": "Send the Beacon login", "direction": "theirs", "owner": "Maya",
         "due_date": None,
         "quote": "I'll send you the Beacon login today.", "speaker": "Maya"},
        {"task": "Book the offsite", "direction": "theirs", "owner": "Maya", "due_date": None,
         "quote": "I'll book the offsite venue next week.", "speaker": "Maya"},
    ]}
    kept, dropped = extract.validate(raw, episode)
    return {"kept": kept, "dropped": dropped, "usage": {"model": "fake", "structured": "fake",
                                                        "input_tokens": 10, "output_tokens": 5}}


def main() -> None:
    global BASE
    # Route every ingest through the stub instead of a real provider.
    original = notes.ingest_paths

    def stubbed(conn, paths, **kw):
        kw.setdefault("extract_fn", fake_extract)
        return original(conn, paths, **kw)

    notes.ingest_paths = stubbed

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    BASE = f"http://127.0.0.1:{httpd.server_address[1]}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"server on {BASE}\ntemp root {TMP}\n")

    try:
        print("CSRF guard (a credentials page is exposed here):")
        status, body = request("POST", "/api/notes", {"body": "x"}, headers={"X-Ruger": ""})
        check("write without the guard header -> 403", status, 403)
        status, _ = request("PUT", "/api/config", {"provider": "anthropic"}, headers={"X-Ruger": ""})
        check("settings write without it -> 403", status, 403)
        status, _ = request("POST", "/api/notes", {"body": NOTE_BODY},
                            headers={"Origin": "https://evil.example"})
        check("cross-origin Origin refused -> 403", status, 403)
        status, _ = request("GET", "/api/tasks")
        check("reads still work without it", status, 200)

        print("\nPOST /api/notes — store then extract:")
        status, body = request("POST", "/api/notes", {
            "title": "Weekly with Maya", "date": "2026-08-03",
            "participants": ["Alex", "Maya"], "body": NOTE_BODY})
        check("status", status, 200)
        check("actionables created", body["created"], 2)
        check("hallucinated quote dropped", body["dropped"], 1)
        check("returns the new ids", len(body["commitment_ids"]), 2)
        check("returns the full board", len(body["tasks"]), 2)
        saved = body["saved"][0]
        check("filename is slugified and dated", saved, "2026-08-03-weekly-with-maya.md")

        stored = Path(os.environ["PKM_INBOX"]) / saved
        check("file really is on disk", stored.exists(), True)
        text = stored.read_text()
        check("frontmatter written", text.startswith('---\ntitle: "Weekly with Maya"'), True)
        check("body preserved verbatim (the quote check depends on it)",
              NOTE_BODY.strip() in text, True)

        drops = body["episodes"][0]["drops"]
        check("drop reason surfaced to the UI", drops[0]["reason"], "quote not_found")

        print("\nempty and malformed input:")
        status, body = request("POST", "/api/notes", {"body": "   "})
        check("empty note -> 400", status, 400)
        status, body = request("POST", "/api/notes", {"body": "x", "date": "not-a-date"})
        check("bad date -> 400", status, 400)
        status, body = request("POST", "/api/uploads", {"files": []})
        check("no files -> 400", status, 400)

        print("\nPOST /api/uploads:")
        status, body = request("POST", "/api/uploads", {"files": [
            {"filename": "../../etc/passwd.md", "content": NOTE_BODY.replace("Friday", "Monday")},
        ]})
        check("status", status, 200)
        # Only the final path component survives, then it is slugified.
        check("path traversal neutralised", body["saved"][0], "passwd.md")
        check("nothing escaped the inbox",
              (Path(os.environ["PKM_INBOX"]) / "passwd.md").exists(), True)
        check("second copy merged, not duplicated", body["merged"] >= 1, True)

        print("\nGET /api/notes:")
        status, body = request("GET", "/api/notes")
        check("status", status, 200)
        check("two notes stored", len(body["notes"]), 2)
        first = body["notes"][0]
        for field in ("id", "title", "started_at", "commitments", "drops", "extracted_at"):
            if field not in first:
                check(f"field {field}", False, True)
        print(f"        {[(n['title'], n['commitments']) for n in body['notes']]}")

        print("\nDELETE /api/notes/{id}:")
        target = [n for n in body["notes"] if n["title"] == "Weekly with Maya"][0]
        before = len(request("GET", "/api/tasks")[1]["tasks"])
        status, body = request("DELETE", f"/api/notes/{target['id']}")
        check("status", status, 200)
        check("file moved to trash, not deleted", len(body["moved_to_trash"]), 1)
        check("trash file exists", Path(body["moved_to_trash"][0]).exists(), True)
        check("inbox copy gone", stored.exists(), False)
        after = request("GET", "/api/tasks")[1]["tasks"]
        check("its commitments went too", len(after) < before, True)
        check("remaining notes", len(request("GET", "/api/notes")[1]["notes"]), 1)
        status, _ = request("DELETE", "/api/notes/999999")
        check("unknown note -> 400", status, 400)

        print("\nGET /api/config:")
        status, cfg = request("GET", "/api/config")
        check("status", status, 200)
        check("defaults to anthropic", cfg["provider"], "anthropic")
        check("secret never leaves the process", "api_key" in cfg, False)
        check("presets include gemini", "gemini" in cfg["presets"], True)
        check("presets include openai-compatible", "openai" in cfg["presets"], True)

        print("\nPUT /api/config:")
        status, body = request("PUT", "/api/config", {
            "provider": "gemini", "api_key": "AIzaTESTKEY1234567890", "model": "gemini-2.5-flash",
            "me": "Alex, S", "base_url": "", "context_length": ""})
        check("status", status, 200)
        check("saved", body["ok"], True)
        check("provider applied", body["settings"]["provider"], "gemini")
        check("gemini base url filled from preset",
              body["settings"]["base_url"], config.PROVIDER_PRESETS["gemini"]["base_url"])
        check("key stored but masked", body["settings"]["api_key_set"], True)
        check("mask hides the key", "AIzaTESTKEY1234567890" not in json.dumps(body), True)
        check("me applied", config.ME_ALIASES, ["Alex", "S"])

        env_text = (TMP / ".env").read_text()
        check("written to the env file", "PKM_PROVIDER=gemini" in env_text, True)
        check("permissions are owner-only", oct(os.stat(TMP / ".env").st_mode & 0o777), "0o600")
        check("paths are NOT writable over http", "PKM_DB" in env_text, False)

        print("\n  a blank key keeps the stored one:")
        request("PUT", "/api/config", {"provider": "gemini", "model": "gemini-2.5-pro"})
        check("key survived", config.API_KEY, "AIzaTESTKEY1234567890")
        check("model changed", config.MODEL, "gemini-2.5-pro")

        print("\n  validation:")
        status, body = request("PUT", "/api/config", {"provider": "nope"})
        check("unknown provider rejected", status, 400)
        status, body = request("PUT", "/api/config", {"provider": "openai", "model": ""})
        check("openai without a model rejected", status, 400)
        status, body = request("PUT", "/api/config", {"provider": "gemini", "base_url": "ftp://x"})
        check("bad base url rejected", status, 400)
        check("still on the last good settings", config.PROVIDER, "gemini")

        print("\nboard page still wired to the api:")
        status, html = request("GET", "/")
        check("serves", status, 200)
        for needle in ('fetch(path', '"/api/tasks"', '"/api/notes"', '"/api/config"',
                       'X-Ruger', 'data-view="settings"'):
            check(f"page references {needle}", needle in html, True)
        check("no hardcoded task fixture", "const TASKS = [{" in html, False)

        print("\nthe design rules CLAUDE.md says are easy to break by accident:")
        import re as _re

        # 1. Every colour token defined for dark needs a light counterpart. A
        #    missing one silently inherits the dark value and goes unreadable.
        dark = _re.search(r":root\s*\{(.*?)\}", html, _re.S).group(1)
        light = _re.search(r':root\[data-theme="light"\]\s*\{(.*?)\}', html, _re.S).group(1)
        colourish = _re.compile(r"(--[\w-]+):\s*(#|rgba?\()")
        dark_tokens = {m.group(1) for m in colourish.finditer(dark)}
        light_tokens = {m.group(1) for m in colourish.finditer(light)}
        missing = sorted(t for t in dark_tokens - light_tokens
                         if not t.startswith("--shadow"))
        check("every colour token has a light counterpart", missing, [])

        # 2. No colour emoji in the chrome. They ignore `color`, so they cannot
        #    dim with their row or invert for light mode, and they read as clip
        #    art next to 13px type.
        chrome = html[html.index('<aside class="sidebar"'):html.index("</aside>")]
        emoji = _re.findall(r"[\U0001F300-\U0001FAFF☀-➿️]", chrome)
        check("the sidebar carries no emoji", emoji, [])
        check("it uses inline svg instead", 'data-icon="activity"' in html, True)

        # 3. Layout spacing comes from the scale. `padding` is exempt on purpose:
        #    a control's internal padding is tuned to the 37px height invariant,
        #    which is a deliberate optical value rather than a stray one.
        oneoffs = _re.findall(r"(?:margin|gap):\s*(?:[\w()-]+\s+)*?(\d\d+)px", html)
        check("no stray layout spacing outside the scale",
              sorted({int(v) for v in oneoffs} - {12, 16, 24, 32, 48, 96, 120}), [])
    finally:
        notes.ingest_paths = original
        httpd.shutdown()
        httpd.server_close()

    print("\nOK — ingest stores files, sources list and delete work, settings persist safely.")


if __name__ == "__main__":
    main()
