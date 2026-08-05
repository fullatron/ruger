"""The capture layer's handoff into Ruger's inbox.

    .venv/bin/python scratch/test_capture_handoff.py
    CAPTURE_NOTE=/tmp/capture-inbox/x.md .venv/bin/python scratch/test_capture_handoff.py

A capture layer's `to_ruger_note` writes a markdown file into `~/.pkm/inbox` and
stops. Ruger picks it up through the ordinary connector, so there is still
exactly one ingest path (D2) and neither program calls the other: a file on disk
is the whole interface.

This asserts Ruger's half of that contract. With `CAPTURE_NOTE` set it runs
against a file the Rust side actually produced; without it, against the fixture
below, which is byte-identical to that output. So it stays runnable with no Rust
toolchain and still catches drift when one is available.

No model call: what matters here is that ingest stores the body verbatim and
that the verbatim-quote check (§5) can match the transcript lines. If those hold,
extraction has what it needs.
"""

from __future__ import annotations

import os
import sys
import tempfile
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp())
os.environ.update(PKM_DB=str(TMP / "t.db"), PKM_INBOX=str(TMP / "inbox"),
                  PKM_TRASH=str(TMP / "trash"), PKM_ENV_FILE=str(TMP / ".env"),
                  PKM_ME="Alex")
for _k in ("PKM_PROVIDER", "PKM_MODEL", "PKM_API_KEY", "PKM_BASE_URL"):
    os.environ.pop(_k, None)

sys.path.insert(0, str(ROOT))

from pkm import config, db, dedup, episodes, extract  # noqa: E402
from pkm.connectors import inbox  # noqa: E402

config.ENV_FILE = TMP / ".env"
config.reload()

# Byte-identical to what the capture layer's handoff example writes.
FIXTURE = '''---
title: "Weekly with Maya"
date: 2026-08-04
id: capture-01ABCD23EF45GH67JK89MN0PQR
---

Launch prep. Alex owns the profile audit; Maya owns access and the banner.

## My notes

invoice — 50% now, rest on exit

## Transcript

Them: Okay so where are we on the launch.
Me: I'll audit all the team LinkedIn profiles and align the messaging by Friday.
Them: Good. I'll send you the Beacon login today. And I'll book the trade show banner next week.
Me: Perfect. I'll raise 50% of the outstanding invoice now.
Them: We should probably revisit pricing at some point.
'''

PASSES = {"n": 0}


def check(label, actual, expected):
    ok = actual == expected
    PASSES["n"] += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {actual!r}"
          + ("" if ok else f" != {expected!r}"))
    if not ok:
        raise SystemExit(1)


def main() -> None:
    live = os.environ.get("CAPTURE_NOTE")
    if live:
        note_text = Path(live).read_text(encoding="utf-8")
        print(f"against the real capture output: {live}\n")
    else:
        note_text = FIXTURE
        print("against the embedded fixture (set CAPTURE_NOTE to use a real file)\n")

    inbox_dir = Path(os.environ["PKM_INBOX"])
    inbox_dir.mkdir(parents=True, exist_ok=True)
    path = inbox_dir / "2026-08-04-weekly-with-maya.md"
    path.write_text(note_text, encoding="utf-8")

    print("the connector reads what the capture layer wrote:")
    parsed = inbox.read_note(path) if hasattr(inbox, "read_note") else None
    fields, body = inbox.parse_frontmatter(note_text)
    check("title from frontmatter", fields.get("title"), "Weekly with Maya")
    check("date from frontmatter", str(fields.get("date")), "2026-08-04")
    check("stable id survives", str(fields.get("id")).startswith("capture-"), True)

    print("\ningest stores it:")
    with closing(db.connect()) as conn:
        stats = episodes.ingest_inbox(conn, inbox_dir)
        check("one event", stats["events_new"], 1)
        check("one episode", stats["episodes_new"], 1)
        check("no complaints", stats["problems"], [])

        row = conn.execute("SELECT * FROM episodes").fetchone()
        check("title carried through", row["title"], "Weekly with Maya")
        check("date carried through", row["started_at"], "2026-08-04")
        transcript = row["transcript"]

        print("\n  the body is stored verbatim, which §5 depends on:")
        check("mic line present byte-for-byte",
              "Me: I'll audit all the team LinkedIn profiles and align the messaging by Friday."
              in transcript, True)
        # Consecutive segments from one channel are merged into a single line,
        # so their turn opens with "Good." and the promise sits mid-line. That is
        # the point of the merge: a sentence to quote instead of STT fragments.
        check("their turn is one merged line",
              "Them: Good. I'll send you the Beacon login today. "
              "And I'll book the trade show banner next week." in transcript, True)
        check("the summary came along", "Launch prep." in transcript, True)
        check("and the user's own notes", "invoice" in transcript, True)

        print("\n  re-exporting the same session does not create a second episode:")
        # The stable `id` in frontmatter is what makes this hold even though the
        # filename could change with a retitle.
        again = episodes.ingest_inbox(conn, inbox_dir)
        check("zero new events", again["events_new"], 0)
        check("zero new episodes", again["episodes_new"], 0)

        print("\n  a retitle re-exports under a new filename, still one episode:")
        retitled = note_text.replace('title: "Weekly with Maya"',
                                     'title: "Weekly with Maya (revised)"')
        (inbox_dir / "2026-08-04-weekly-with-maya-revised.md").write_text(
            retitled, encoding="utf-8")
        third = episodes.ingest_inbox(conn, inbox_dir)
        check("still no new episode", third["episodes_new"], 0)
        check("total episodes", conn.execute(
            "SELECT COUNT(*) AS n FROM episodes").fetchone()["n"], 1)

    print("\nthe verbatim-quote check can match the transcript (§5):")
    # Exactly what the model is asked to return: the spoken sentence, without
    # the speaker prefix. If this does not match, every commitment gets dropped.
    for quote in [
        "I'll audit all the team LinkedIn profiles and align the messaging by Friday.",
        "I'll send you the Beacon login today.",
        "And I'll book the trade show banner next week.",
        "I'll raise 50% of the outstanding invoice now.",
    ]:
        ok, how = extract.verify_quote(quote, transcript)
        check(f"{quote[:44]!r}", (ok, how), (True, "exact"))

    print("\n  and a quote nobody said is still rejected:")
    ok, how = extract.verify_quote("I'll ship the pricing page on Tuesday.", transcript)
    check("hallucination dropped", (ok, how), (False, "not_found"))

    print("\nspeaker attribution — the thing Granola could not give us:")
    # The capture layer knows the channel for certain, so it labels the mic
    # channel "Me". Ruger folds that to its canonical owner with no config:
    # `normalise_owner` adds me/i/myself/self regardless of PKM_ME.
    check("MIC_LABEL 'Me' folds to the canonical owner",
          dedup.normalise_owner("Me", None), "me")
    check("lowercase too", dedup.normalise_owner("me", None), "me")
    check("'Them' does not", dedup.normalise_owner("Them", None), "them")
    check("a named participant does not",
          dedup.normalise_owner("Maya", None), "maya")
    check("and an explicit direction still wins",
          dedup.normalise_owner("Maya", "mine"), "me")

    print(f"\nOK — {PASSES['n']} assertions. Capture writes a file, Ruger ingests it, "
          f"and the quote check has something to match.")


if __name__ == "__main__":
    main()
