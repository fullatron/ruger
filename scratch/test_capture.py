"""Capture: a dictated line becomes tasks in Notion (§10).

    .venv/bin/python scratch/test_capture.py

Canned model output through the real writer, the real connector, the real
validator and the real dedup, with a stand-in for Notion's push. No model call,
no network, no notification on your screen.

What is being protected:

  - the note is written verbatim, because the quote check runs on those bytes;
  - `kind: capture` survives ingest, which is what selects the capture prompt and
    the lower quote floor (D14/D15);
  - that floor lets a two-word quote through *without* loosening the
    contiguous-span rule that catches invention;
  - a meeting is completely unaffected by any of it;
  - a capture only pushes its own tasks, not the whole board.
"""

from __future__ import annotations

import os
import sys
import tempfile
from contextlib import closing
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp())
os.environ.update(PKM_DB=str(TMP / "t.db"), PKM_INBOX=str(TMP / "inbox"),
                  PKM_TRASH=str(TMP / "trash"), PKM_ENV_FILE=str(TMP / ".env"),
                  PKM_ME="Alex")
for _k in ("PKM_PROVIDER", "PKM_MODEL", "PKM_API_KEY", "PKM_BASE_URL"):
    os.environ.pop(_k, None)

sys.path.insert(0, str(ROOT))

from pkm import capture, config, db, episodes, extract  # noqa: E402
from pkm.connectors import inbox as inbox_mod  # noqa: E402

config.ENV_FILE = TMP / ".env"
config.reload()

PASSES = {"n": 0}
WHEN = datetime(2026, 8, 5, 14, 32, 10)

TEXT = "send maya the revised deck tomorrow and book the trade show banner"

GOOD = [
    {"task": "Send Maya the revised deck", "direction": "mine", "owner": "me",
     "due_date": "2026-08-06", "quote": "send maya the revised deck tomorrow",
     "speaker": None},
    {"task": "Book the trade show banner", "direction": "mine", "owner": "me",
     "due_date": None, "quote": "and book the trade show banner", "speaker": None},
]


def check(label, actual, expected):
    ok = actual == expected
    PASSES["n"] += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {actual!r}"
          + ("" if ok else f" != {expected!r}"))
    if not ok:
        raise SystemExit(1)


def raises(label, fn, message_contains=""):
    PASSES["n"] += 1
    try:
        fn()
    except capture.CaptureError as exc:
        ok = message_contains in str(exc)
        print(f"  {'PASS' if ok else 'FAIL'}  {label}: {str(exc)[:60]!r}")
        if not ok:
            raise SystemExit(1)
        return
    print(f"  FAIL  {label}: did not raise")
    raise SystemExit(1)


def canned(items):
    def fake(episode):
        kept, dropped = extract.validate({"commitments": items}, episode)
        return {"kept": kept, "dropped": dropped,
                "usage": {"model": "fake", "input_tokens": 1, "output_tokens": 1}}
    return fake


def main() -> None:
    print("the note is named after what you said:")
    check("first words", capture.title_for("send maya the deck", WHEN),
          "Capture · send maya the deck")
    check("long ones are cut", capture.title_for(TEXT, WHEN),
          "Capture · send maya the revised deck tomorrow and book…")
    check("punctuation trimmed", capture.title_for("book the banner.", WHEN),
          "Capture · book the banner")
    check("wordless falls back to the clock",
          capture.title_for("...", WHEN), "Capture 14:32")

    print("\nit refuses what is not a capture:")
    raises("empty", lambda: capture.write_note("   "), "nothing was captured")
    raises("a whole transcript", lambda: capture.write_note("x " * 3000),
           "captures are for a sentence")

    print("\nthe file is written verbatim, which §5 depends on:")
    path = capture.write_note(TEXT, when=WHEN, inbox=TMP / "inbox")
    text = path.read_text(encoding="utf-8")
    check("named by date and time", path.name, "2026-08-05-capture-20260805-143210.md")
    check("marked as a capture", "kind: capture" in text, True)
    check("carries a stable id", "id: capture-20260805-143210" in text, True)
    check("body is exactly what was said", text.endswith(TEXT + "\n"), True)

    print("\n  and `kind` survives the trip through ingest:")
    record = inbox_mod.read_file(path, TMP / "inbox")
    check("the connector reads it", record["kind"], "capture")
    with closing(db.connect()) as conn:
        episodes.ingest_inbox(conn, TMP / "inbox")
        row = conn.execute("SELECT * FROM episodes").fetchone()
        check("the episode records it", row["kind"], "capture")
        check("source stays legal for the CHECK constraint", row["source"], "meeting")

        print("\n  so the capture prompt and the lower floor are selected:")
        check("prompt", extract.prompt_for(row).name, "extract_capture.md")
        check("floor", extract.quote_floor(row), (2, 6))

    print("\nthe lowered floor is a threshold, not an exemption:")
    short = "book the trade"        # 3 words, 14 chars: under the meeting floor
    check("a meeting would reject it",
          extract.verify_quote(short, TEXT), (False, "too_short"))
    check("a capture accepts it",
          extract.verify_quote(short, TEXT, min_words=2, min_chars=6),
          (True, "exact"))
    check("but two words is still the floor",
          extract.verify_quote("book", TEXT, min_words=2, min_chars=6),
          (False, "too_short"))
    check("and an invented line still fails",
          extract.verify_quote("call the printer about it", TEXT,
                               min_words=2, min_chars=6),
          (False, "not_found"))
    check("as does a reworded one",
          extract.verify_quote("book the tradeshow banner", TEXT,
                               min_words=2, min_chars=6),
          (False, "not_found"))

    print("\n  a meeting is untouched by any of it:")
    meeting = {"transcript": TEXT, "started_at": "2026-08-05", "kind": "meeting"}
    check("prompt", extract.prompt_for(meeting).name, "extract_commitments.md")
    check("floor", extract.quote_floor(meeting), (4, 15))
    check("and an episode with no kind at all defaults to a meeting",
          extract.quote_floor({"transcript": "x", "started_at": "2026-08-05"}),
          (4, 15))

    print("\ncapture end to end, without Notion:")
    fresh = Path(TMP / "inbox2")
    with closing(db.connect(TMP / "run.db")) as conn:
        result = capture.run(TEXT, conn=conn, push=False, when=WHEN, inbox=fresh,
                             extract_fn=canned(GOOD))
        check("no error", result["error"], None)
        check("two tasks", [t["task"] for t in result["tasks"]],
              ["Send Maya the revised deck", "Book the trade show banner"])
        check("the relative date resolved",
              result["tasks"][0]["due_date"], "2026-08-06")
        check("both are mine", {t["direction"] for t in result["tasks"]}, {"mine"})
        check("evidence is on the row",
              result["tasks"][0]["quote"], "send maya the revised deck tomorrow")
        check("nothing pushed", result["pushed"], 0)
        check("and nothing is linked to Notion yet",
              conn.execute("SELECT COUNT(*) AS n FROM commitments "
                           "WHERE external_id IS NOT NULL").fetchone()["n"], 0)
        check("the summary reads as a notification",
              capture.summarise(result), "2 tasks on the board")

        print("\n  saying the same thing again merges rather than duplicating:")
        again = capture.run(TEXT, conn=conn, push=False,
                            when=datetime(2026, 8, 5, 15, 0, 0), inbox=fresh,
                            extract_fn=canned(GOOD))
        check("no new rows", again["tasks"], [])
        check("merged into the open ones", again["merged"], 2)
        check("board still holds two",
              conn.execute("SELECT COUNT(*) AS n FROM commitments").fetchone()["n"], 2)
        check("summary says so", capture.summarise(again), "2 merged on the board")

        print("\n  a hallucinated task is dropped, and reported:")
        bad = capture.run("chase theo about the invoice", conn=conn, push=False,
                          when=datetime(2026, 8, 5, 16, 0, 0), inbox=fresh,
                          extract_fn=canned([
                              {"task": "Book a flight", "direction": "mine",
                               "owner": "me", "due_date": None,
                               "quote": "book a flight to lisbon", "speaker": None}]))
        check("nothing kept", bad["tasks"], [])
        check("one dropped", bad["dropped"], 1)
        check("for the right reason", bad["drops"][0]["reason"], "quote not_found")
        check("and it says so", capture.summarise(bad),
              "Nothing to do in that one (1 dropped)")

    print("\npush sends only the capture's own tasks, not the whole board:")
    sent = {}

    with closing(db.connect(TMP / "push.db")) as conn:
        from pkm.connectors import notion

        def fake_push(c, **kw):
            sent.update(kw)
            return {"created": len(kw.get("only") or []), "updated": 0,
                    "failed": 0, "errors": [], "total": 0, "plan": [], "url": ""}

        original, notion.push = notion.push, fake_push
        try:
            result = capture.run(TEXT, conn=conn, push=True, when=WHEN,
                                 inbox=TMP / "inbox3", extract_fn=canned(GOOD))
        finally:
            notion.push = original

        check("pushed both", result["pushed"], 2)
        check("no push error", result["push_error"], None)
        check("scoped to these ids", sent.get("only"),
              {t["id"] for t in result["tasks"]})
        check("the summary says Notion", capture.summarise(result),
              "2 tasks added to Notion")

    print("\n  a Notion outage costs the notification, not the task:")
    with closing(db.connect(TMP / "outage.db")) as conn:
        from pkm.connectors import notion

        def angry_push(c, **kw):
            raise notion.NotionError("Notion is not answering")

        original, notion.push = notion.push, angry_push
        try:
            result = capture.run(TEXT, conn=conn, push=True, when=WHEN,
                                 inbox=TMP / "inbox4", extract_fn=canned(GOOD))
        finally:
            notion.push = original

        check("the tasks still exist", len(result["tasks"]), 2)
        check("nothing pushed", result["pushed"], 0)
        check("and the failure is reported", result["push_error"],
              "Notion is not answering")
        check("they are still on the board for the next push",
              conn.execute("SELECT COUNT(*) AS n FROM commitments").fetchone()["n"], 2)

    print(f"\nOK — {PASSES['n']} assertions. A dictated line becomes tasks, and the "
          f"quote check still refuses to take the model's word for it.")


if __name__ == "__main__":
    main()
