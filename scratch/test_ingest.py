"""Step 1 proof: drop 5 files, run twice, second run adds zero rows.

    .venv/bin/python scratch/test_ingest.py

Uses a temp inbox and an in-memory DB — touches neither ~/.pkm nor your real
board.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pkm import db, episodes  # noqa: E402
from pkm.connectors import inbox  # noqa: E402

FIXTURES = {
    # 1. Full frontmatter, the documented shape.
    "weekly-maya.md": """---
title: Weekly with Maya
date: 2026-08-03
participants: [Alex, Maya]
id: fixture-weekly-maya
---

Alex: I'll audit all the team LinkedIn profiles by Friday.
Maya: Great. I'll send you the Beacon login today.
""",
    # 2. Granola paste with no frontmatter: date on its own line, title in the
    #    filename (with a leading "# " that survived the paste).
    "# Northwind GTM strategy with founder.md": """
Sat, 01 Aug 26

### Next Steps

- **Audit all team LinkedIn profiles and align messaging by vertical**Target: 3-4 days.
- **Connect with Iris and Maya for Lisbon office logistics**Intro email already sent.
""",
    # 3. Title from an H1, ISO datetime in frontmatter.
    "standup.md": """---
date: 2026-08-04T09:30:00
---

# Monday standup

Theo: I'm going to take over the Sendline inbox warmup this week.
""",
    # 4. Participants as a comma string, not a list.
    "vendor-call.md": """---
title: Vendor call — Sendline
date: 2026-08-04
attendees: Alex, Nina
---

Nina: Let me get you the enterprise pricing sheet by Wednesday.
""",
    # 5. No date anywhere — must fall back to mtime, not crash.
    "undated-note.md": """# Coffee with Omar

Omar said he'd make the intro to two design agencies next week.
""",
}


def write_fixtures(root: Path) -> None:
    for name, body in FIXTURES.items():
        (root / name).write_text(body, encoding="utf-8")


def counts(conn) -> dict:
    return {
        table: conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        for table in ("events", "episodes", "episode_events", "commitments")
    }


def check(label: str, actual, expected) -> None:
    ok = actual == expected
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {actual!r}" + ("" if ok else f" != {expected!r}"))
    if not ok:
        raise SystemExit(1)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixtures(root)
        conn = db.connect(":memory:")

        print(f"inbox: {root}  ({len(FIXTURES)} files)\n")

        print("parse:")
        records, problems = inbox.scan(root)
        check("files parsed", len(records), 5)
        check("problems", problems, [])
        for r in sorted(records, key=lambda r: r["occurred_at"]):
            print(
                f"        {r['occurred_at']}  ({r['date_source']:>18})  "
                f"{r['title'][:44]:<44}  participants={r['participants']}"
            )

        by_title = {r["title"]: r for r in records}
        check(
            "title from filename, '# ' stripped",
            "Northwind GTM strategy with founder" in by_title,
            True,
        )
        check("date parsed from body line", by_title["Northwind GTM strategy with founder"]["occurred_at"], "2026-08-01")
        check("title from H1", by_title["Monday standup"]["occurred_at"], "2026-08-04")
        check("participants from comma string", by_title["Vendor call — Sendline"]["participants"], ["Alex", "Nina"])
        check("undated falls back to mtime", by_title["Coffee with Omar"]["date_source"], "mtime")
        check("frontmatter id wins over path", by_title["Weekly with Maya"]["external_id"], "fixture-weekly-maya")

        print("\nrun 1 (cold):")
        first = episodes.ingest_inbox(conn, root)
        after_first = counts(conn)
        check("events_new", first["events_new"], 5)
        check("episodes_new", first["episodes_new"], 5)
        check("row counts", after_first, {"events": 5, "episodes": 5, "episode_events": 5, "commitments": 0})
        check("all episodes await extraction", len(db.episodes_needing_extraction(conn)), 5)

        print("\nrun 2 (same files, unchanged):")
        second = episodes.ingest_inbox(conn, root)
        after_second = counts(conn)
        check("events_new", second["events_new"], 0)
        check("events_updated", second["events_updated"], 0)
        check("episodes_new", second["episodes_new"], 0)
        check("episodes_changed", second["episodes_changed"], 0)
        check("row counts unchanged", after_second, after_first)

        print("\nrun 3 (one file edited):")
        (root / "standup.md").write_text(
            FIXTURES["standup.md"] + "\nAlex: I'll write the Reddit seeding plan tomorrow.\n",
            encoding="utf-8",
        )
        third = episodes.ingest_inbox(conn, root)
        check("events_new", third["events_new"], 0)
        check("events_updated", third["events_updated"], 1)
        check("row counts still 5/5", counts(conn) | {"commitments": 0}, after_first)

        # Pretend the edited episode had already been extracted, so we can prove
        # a content change is what re-queues it.
        for ep in conn.execute("SELECT id FROM episodes").fetchall():
            db.mark_extracted(conn, ep["id"], "fixture")
        check("nothing queued once all extracted", len(db.episodes_needing_extraction(conn)), 0)
        (root / "standup.md").write_text(
            FIXTURES["standup.md"] + "\nMaya: I'll review it Thursday.\n", encoding="utf-8"
        )
        episodes.ingest_inbox(conn, root)
        queued = db.episodes_needing_extraction(conn)
        check("edited episode re-queued", [q["title"] for q in queued], ["Monday standup"])

        print("\nOK — 5 files in, 5 events, 5 episodes, zero duplicates on re-run.")


if __name__ == "__main__":
    main()
