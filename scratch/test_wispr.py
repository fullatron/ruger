"""The Wispr Flow importer: a meeting becomes a file, the file becomes rows.

    .venv/bin/python scratch/test_wispr.py

Builds a fake Wispr Flow data directory — a flow.sqlite with the columns the
importer reads, plus refined/live ndjson — and runs the real export, the real
inbox connector and the real quote check over it. No Wispr install needed, no
model call, nothing outside a temp dir.

What is actually being protected here:

  - segments join with NO separator, because Wispr splits words across them and
    a spurious space breaks the verbatim-quote check;
  - the microphone speaker becomes `Me`, so promises you made are filed as
    yours without configuring anything;
  - `<@speaker:N>` is resolved, or the model has nobody to name as owner;
  - the generated summary is not copied twice into the note;
  - a retitle does not leave two files claiming one id.
"""

from __future__ import annotations

import json
import os
import sqlite3
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
for _k in ("PKM_PROVIDER", "PKM_MODEL", "PKM_API_KEY", "PKM_BASE_URL", "PKM_WISPR_HOME"):
    os.environ.pop(_k, None)

sys.path.insert(0, str(ROOT))

from pkm import config, db, episodes, extract  # noqa: E402
from pkm.connectors import wispr  # noqa: E402

config.ENV_FILE = TMP / ".env"
config.reload()

MEETING_ID = "4b7c2f10-8a3d-4c19-9e52-0f1a6d8b3c47"

# Shaped exactly like the live app's: `people` keyed by uuid, `assignments`
# recording which signal identified each numbered speaker.
SPEAKER_MAP = json.dumps({
    "people": {
        "ca43a33d": {"name": "Kavi", "origin": "llm"},
        "d92a1d6b": {"name": "Alex", "origin": "self"},
    },
    "assignments": {
        "1": {"consensus": "ca43a33d", "user": None, "mic": None, "dom": None,
              "llm": "ca43a33d"},
        "2": {"consensus": "d92a1d6b", "user": None, "mic": "d92a1d6b", "dom": None,
              "llm": None},
    },
})

SUMMARY = """Handoff call on cold email outreach: Speaker 2 (last day) briefed \
<@speaker:1> on inbox access.

<@speaker:2>'s last day; <@speaker:1>'s email is not provisioned yet.

### Next Steps
- (<@speaker:1>) Share attendee list with Nila once ready
- (<@speaker:2>) Send KT doc to Nila for <@speaker:1>"""

NOTES_ONLY_SUMMARY = f"---\n\n:::toggle\n## Flow Summary\n\n{SUMMARY}\n\n:::\n"
NOTES_WITH_THOUGHTS = (
    f"---\n\n:::toggle\n## Flow Summary\n\n{SUMMARY}\n\n:::\n\n"
    "chase Nila on Monday if nothing lands\n"
)

# Wispr splits mid-word across segments, and only puts a leading space where a
# real word boundary falls. "Send" + "line" must rejoin as "Sendline".
REFINED = [
    {"id": "a", "timestamp": "00:00", "text": "Hello.",
     "speaker": {"id": 1, "source": "refined", "name": None}},
    {"id": "b", "timestamp": "00:01", "text": " Hello. Hey, Kavi, how are you?",
     "speaker": {"id": 2, "source": "refined", "name": None}},
    {"id": "c", "timestamp": "00:04",
     "text": " There's this platform called Send",
     "speaker": {"id": 2, "source": "refined", "name": None}},
    {"id": "d", "timestamp": "00:06", "text": "line that we use.",
     "speaker": {"id": 2, "source": "refined", "name": None}},
    {"id": "e", "timestamp": "00:09",
     "text": " I'll share the attendee list with Nila once I have it.",
     "speaker": {"id": 1, "source": "refined", "name": None}},
    # The same promise the raw stream carries, worded as the refine pass has it.
    {"id": "f", "timestamp": "00:14",
     "text": " I'll send the KT doc across on WhatsApp.",
     "speaker": {"id": 2, "source": "refined", "name": None}},
]

# The raw stream: deliberately out of order, numbered 1 throughout, carrying the
# mic/system flag that is the real attribution.
LIVE = [
    {"meta": {"v": 3, "clock": "recording_active_ms"}},
    {"id": "z", "marker": "paused", "timestamp": "4:41 PM"},
    {"id": "l2", "timestamp": "0:03", "text": " I'll send the KT doc over WhatsApp.",
     "speaker": {"id": 1, "source": "mic", "name": None}, "startRecordingMs": 3000},
    {"id": "l1", "timestamp": "0:00", "text": "Hello there, how are you?",
     "speaker": {"id": 1, "source": "system", "name": None}, "startRecordingMs": 100},
]

PASSES = {"n": 0}


def check(label, actual, expected):
    ok = actual == expected
    PASSES["n"] += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {actual!r}"
          + ("" if ok else f" != {expected!r}"))
    if not ok:
        raise SystemExit(1)


def build_home(*, notes=NOTES_ONLY_SUMMARY, refined=True, live=True,
               title="Handoff and Access", finalized=1,
               ended_ms=1785928278670, home=None) -> Path:
    """A fake Wispr Flow data directory."""
    base = Path(home) if home else Path(tempfile.mkdtemp(dir=TMP))
    conn = sqlite3.connect(base / "flow.sqlite")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS Meetings (
             id TEXT PRIMARY KEY, title TEXT, content TEXT, createdAt TEXT,
             modifiedAt TEXT, finalized INT, isDeleted INT DEFAULT 0,
             isTourDemo INT DEFAULT 0, endedAt INT, notes TEXT, summary TEXT,
             speakerMap TEXT)"""
    )
    conn.execute("DELETE FROM Meetings")
    conn.execute(
        "INSERT INTO Meetings (id, title, createdAt, modifiedAt, finalized, "
        "endedAt, notes, summary, speakerMap) VALUES (?,?,?,?,?,?,?,?,?)",
        (MEETING_ID, title, "2026-08-05 11:06:36.518 +00:00",
         "2026-08-05 11:24:39.000 +00:00", finalized, ended_ms, notes, SUMMARY,
         SPEAKER_MAP),
    )
    conn.commit()
    conn.close()

    md = base / "meetings" / MEETING_ID
    md.mkdir(parents=True, exist_ok=True)
    if refined:
        (md / "refined.ndjson").write_text(
            "\n".join(json.dumps(r) for r in REFINED), encoding="utf-8")
    if live:
        (md / "live.ndjson").write_text(
            "\n".join(json.dumps(r) for r in LIVE), encoding="utf-8")
    return base


def main() -> None:
    inbox = Path(os.environ["PKM_INBOX"])

    print("speakerMap says which speaker held the microphone:")
    names, me = wispr.speakers(SPEAKER_MAP)
    check("names resolved", names, {1: "Kavi", 2: "Alex"})
    check("mic assignment identifies me", me, 2)
    check("my turns are labelled Me", wispr.label(2, names, me), "Me")
    check("theirs get a real name, not 'Them'", wispr.label(1, names, me), "Kavi")
    check("an unknown speaker still degrades safely",
          wispr.label(9, names, me), "Them")

    print("\n  a missing/garbled map does not explode:")
    check("no map", wispr.speakers(None), ({}, None))
    check("bad json", wispr.speakers("{nope"), ({}, None))

    print("\nspeaker tokens are resolved, so the model has an owner to name:")
    resolved = wispr.resolve_speakers(SUMMARY, names, me)
    check("token for me becomes Me", "(Me) Send KT doc to Nila" in resolved, True)
    check("token for them becomes their name",
          "(Kavi) Share attendee list" in resolved, True)
    check("the literal form is caught too", "Speaker 2" in resolved, False)
    check("nothing unresolved is left", "<@speaker:" in resolved, False)
    check("my possessive reads as English, not 'Me's'",
          "My last day" in resolved, True)
    check("and theirs keeps the apostrophe", "Kavi’s email" in resolved, True)

    print("\nthe generated summary is not counted as the human's own notes:")
    check("only a summary means no thoughts", wispr.my_thoughts(NOTES_ONLY_SUMMARY), "")
    check("what the human typed survives",
          wispr.my_thoughts(NOTES_WITH_THOUGHTS),
          "chase Nila on Monday if nothing lands")
    check("empty stays empty", wispr.my_thoughts(None), "")

    print("\nthe transcript rejoins words Wispr split across segments:")
    body = wispr.transcript(build_home() / "meetings" / MEETING_ID, names, me)
    check("no spurious space inside a split word", "Sendline" in body, True)
    check("and the broken form is absent", "Send line" in body, False)
    check("consecutive turns from one speaker merge into one line",
          "Me: Hello. Hey, Kavi, how are you? There's this platform called "
          "Sendline that we use." in body, True)
    check("their line carries their name",
          "Kavi: I'll share the attendee list with Nila once I have it." in body,
          True)
    # Six segments, four turns: the three consecutive Me segments become one line.
    check("one line per turn, not per segment", len(body.splitlines()), 4)

    print("\nrefined is preferred, and live is the fallback:")
    only_live = build_home(refined=False)
    live_body = wispr.transcript(only_live / "meetings" / MEETING_ID, names, me)
    check("mic becomes Me", "Me: I'll send the KT doc over WhatsApp." in live_body, True)
    check("system becomes them", "Kavi: Hello there, how are you?" in live_body, True)
    check("ordered by the recording clock, not file order",
          live_body.splitlines()[0].startswith("Kavi:"), True)
    check("meta and marker lines are not speech", len(live_body.splitlines()), 2)

    print("\nno transcript on disk means no file, rather than an empty one:")
    check("nothing to file", wispr.to_note(
        {"id": MEETING_ID, "title": "x", "createdAt": "", "endedAt": None,
         "notes": "", "summary": "", "speakerMap": SPEAKER_MAP},
        build_home(refined=False, live=False) / "meetings" / MEETING_ID), None)

    print("\nthe date is local, so relative due dates resolve correctly:")
    # 2026-08-05T11:06 UTC is the same day everywhere east of UTC-11.
    check("from endEdAt epoch ms",
          wispr.meeting_date({"endedAt": 1785928278670, "createdAt": ""}),
          "2026-08-05")
    check("falling back to the createdAt string",
          wispr.meeting_date({"endedAt": None,
                              "createdAt": "2026-08-05 11:06:36.518 +00:00"}),
          "2026-08-05")

    print("\nexport writes one file per meeting:")
    base = build_home()
    stats = wispr.export(wispr_home=base, inbox=inbox)
    check("one meeting seen", stats["meetings"], 1)
    check("one file written", stats["written"], 1)
    check("named by date and title", stats["files"],
          ["2026-08-05-handoff-and-access.md"])
    written = inbox / stats["files"][0]
    check("it exists", written.exists(), True)

    text = written.read_text(encoding="utf-8")
    check("frontmatter carries a stable id", "id: wispr-" + MEETING_ID in text, True)
    check("and the participants", "participants: [Kavi, Alex]" in text, True)
    check("summary is included once",
          text.count("Share attendee list with Nila"), 1)
    check("no My notes section when there are none", "## My notes" in text, False)
    check("transcript section present", "## Transcript" in text, True)

    print("\n  re-running changes nothing:")
    again = wispr.export(wispr_home=base, inbox=inbox)
    check("nothing rewritten", again["written"], 0)
    check("recognised as current", again["unchanged"], 1)

    print("\n  a retitle moves the file instead of duplicating the id:")
    build_home(title="Handoff v2", home=base)
    moved = wispr.export(wispr_home=base, inbox=inbox)
    check("a new file", moved["written"], 1)
    check("the old one is gone",
          moved["removed"], ["2026-08-05-handoff-and-access.md"])
    check("only one file is left", len(list(inbox.glob("*.md"))), 1)

    print("\n  the human's own notes land in their own section:")
    build_home(title="Handoff and Access", notes=NOTES_WITH_THOUGHTS,
               home=base)
    wispr.export(wispr_home=base, inbox=inbox)
    text = (inbox / "2026-08-05-handoff-and-access.md").read_text()
    check("section added", "## My notes" in text, True)
    check("with the typed text", "chase Nila on Monday" in text, True)
    check("and the summary still once",
          text.count("Share attendee list with Nila"), 1)

    print("\n  an unfinished call is not exported:")
    pending = build_home(finalized=0)
    check("skipped entirely",
          wispr.export(wispr_home=pending, inbox=TMP / "inbox2")["meetings"], 0)

    print("\n  a call that just stopped is left to settle:")
    # `finalized` and the last transcript flush land together, so reading
    # instantly risks a truncated transcript.
    fresh = build_home(ended_ms=int(datetime.now().timestamp() * 1000))
    conn = wispr.connect(fresh)
    try:
        check("held back", len(wispr.meetings(conn)), 0)
        check("but visible with the delay waived", len(wispr.meetings(conn, settle=0)), 1)
    finally:
        conn.close()

    print("\n  the note records which transcript it came from:")
    md = build_home() / "meetings" / MEETING_ID
    check("refined when present", wispr.source_of(md), "refined")
    check("live when it is not",
          wispr.source_of(build_home(refined=False) / "meetings" / MEETING_ID), "live")
    check("None when neither",
          wispr.source_of(build_home(refined=False, live=False) / "meetings" / MEETING_ID),
          None)
    # NOT `source:` — the inbox connector maps that onto events.source, which is
    # constrained to meeting/email/slack, so anything else fails the insert.
    text = (inbox / "2026-08-05-handoff-and-access.md").read_text()
    check("recorded under its own key", "wispr_transcript: refined" in text, True)
    check("and not under `source`", "\nsource:" in text, False)

    print("\nRuger ingests the file through the ordinary connector:")
    with closing(db.connect()) as conn:
        ingested = episodes.ingest_inbox(conn, inbox)
        check("one event", ingested["events_new"], 1)
        check("one episode", ingested["episodes_new"], 1)
        check("no complaints", ingested["problems"], [])

        row = conn.execute("SELECT * FROM episodes").fetchone()
        check("title carried through", row["title"], "Handoff and Access")
        check("date carried through", row["started_at"], "2026-08-05")
        transcript = row["transcript"]

        print("\n  and the quote check can match what was said:")
        for quote in (
            "Kavi: I'll share the attendee list with Nila once I have it.",
            "There's this platform called Sendline that we use.",
        ):
            ok, how = extract.verify_quote(quote, transcript)
            check(f"{quote[:38]}...", (ok, how), (True, "exact"))

        ok, _ = extract.verify_quote(
            "I will definitely courier the laptop back on Tuesday.", transcript)
        check("an invented promise is still rejected", ok, False)

        print("\n  a mic line resolves to the canonical owner:")
        kept, dropped = extract.validate({"commitments": [{
            "task": "Send the KT doc to Nila", "direction": "mine", "owner": "me",
            "due_date": None,
            "quote": "I'll send the KT doc over WhatsApp.",
            "speaker": "Me",
        }]}, {"transcript": transcript, "started_at": "2026-08-05"})
        # That line only exists in live.ndjson, which refined supersedes, so it
        # is correctly absent from the body a human would quote.
        check("not in the refined body, so dropped", len(kept), 0)
        check("for the right reason", dropped[0]["_reason"], "quote not_found")

        kept, _ = extract.validate({"commitments": [{
            "task": "Share the attendee list with Nila", "direction": "theirs",
            "owner": "Kavi", "due_date": None,
            "quote": "I'll share the attendee list with Nila once I have it.",
            "speaker": "Kavi",
        }]}, {"transcript": transcript, "started_at": "2026-08-05"})
        check("a named owner survives", len(kept), 1)
        check("and is not folded into me", kept[0]["owner_norm"], "kavi")

    unattended()

    print(f"\nOK — {PASSES['n']} assertions. Wispr writes a file, Ruger ingests it, "
          f"and who promised what survives the trip.")


def extract_of(quote: str, task: str):
    """A canned extraction, run through the real validator against the real body."""
    def run(episode):
        kept, dropped = extract.validate({"commitments": [{
            "task": task, "direction": "mine", "owner": "me", "due_date": None,
            "quote": quote, "speaker": "Me",
        }]}, episode)
        return {"kept": kept, "dropped": dropped,
                "usage": {"model": "test", "input_tokens": 0, "output_tokens": 0}}
    return run


def unattended() -> None:
    """The sequence a timer produces, which interactive use never reaches.

    A call ends before the summarize button is pressed, so the note is built from
    `live.ndjson` and pushed to Notion. Later the button is pressed, the refine
    pass rewrites the transcript, and the note is re-imported.

    `run_sync` used to send every changed episode through `apply_extraction`,
    which DELETES the episode's rows and re-inserts them. That drops the stored
    Notion page id off each one, so the next push creates a duplicate page for
    every commitment and orphans the originals. It also discards any status set
    in the meantime. This asserts the merge path is taken instead.
    """
    print("\nthe unattended sequence: imported before the summary, refreshed after")

    from pkm import sync

    inbox = TMP / "unattended-inbox"
    base = build_home(refined=False, notes="")          # no summary, raw stream only
    wispr.export(wispr_home=base, inbox=inbox)
    note = next(inbox.glob("*.md"))
    check("first import is from the raw stream",
          "wispr_transcript: live" in note.read_text(), True)

    with closing(db.connect(TMP / "unattended.db")) as conn:
        episodes.ingest_inbox(conn, inbox)
        stats = sync.run_sync(
            conn, inbox_path=inbox,
            extract_fn=extract_of("I'll send the KT doc over WhatsApp.",
                                  "Send the KT doc to Nila"))
        check("extracted first time round", stats["extracted"], 1)
        check("nothing refreshed yet", stats["refreshed"], 0)
        check("one commitment", stats["created"], 1)

        row = conn.execute("SELECT * FROM commitments").fetchone()
        first_id = row["id"]

        # What a push and a bit of board work leave behind.
        with db.transaction(conn):
            db.mark_pushed(conn, first_id, "notion-page-1", "https://notion.so/page-1")
            db.set_status(conn, first_id, "doing")

        print("\n  you press summarize; the refined transcript replaces the raw one:")
        build_home(home=base, refined=True, notes=NOTES_WITH_THOUGHTS)
        again = wispr.export(wispr_home=base, inbox=inbox)
        check("the note is rewritten", again["written"], 1)
        check("now from the refined transcript",
              "wispr_transcript: refined" in next(inbox.glob("*.md")).read_text(), True)

        ingested = episodes.ingest_inbox(conn, inbox)
        check("the episode changed, it is not a new one", ingested["episodes_new"], 0)

        # The refine pass rewords the line, so the stored quote no longer matches
        # and identity falls to task-text similarity — the realistic case.
        stats = sync.run_sync(
            conn, inbox_path=inbox,
            extract_fn=extract_of("I'll send the KT doc across on WhatsApp.",
                                  "Send the KT doc to Nila"))
        check("routed to the merge path, not a fresh extraction", stats["extracted"], 0)
        check("counted as a refresh", stats["refreshed"], 1)
        check("nothing new was created", stats["created"], 0)
        check("the existing row was updated in place", stats["updated"], 1)

        rows = conn.execute("SELECT * FROM commitments").fetchall()
        check("still exactly one commitment", len(rows), 1)
        check("the same row", rows[0]["id"], first_id)
        check("its Notion page id survived", rows[0]["external_id"], "notion-page-1")
        check("so did the url", rows[0]["external_url"], "https://notion.so/page-1")
        check("and the status you had set", rows[0]["status"], "doing")
        check("with evidence refreshed to the new wording",
              rows[0]["quote"], "I'll send the KT doc across on WhatsApp.")

        print("\n  and a commitment the second pass no longer returns is kept:")
        stats = sync.run_sync(
            conn, inbox_path=inbox,
            extract_fn=lambda ep: {"kept": [], "dropped": [],
                                   "usage": {"model": "test", "input_tokens": 0,
                                             "output_tokens": 0}})
        # The transcript did not change, so there is nothing to re-extract.
        check("an unchanged transcript is not re-extracted", stats["refreshed"], 0)
        check("the commitment is still there",
              conn.execute("SELECT COUNT(*) AS n FROM commitments").fetchone()["n"], 1)


if __name__ == "__main__":
    main()
