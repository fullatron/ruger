"""Step 2 proof, without spending API calls.

Feeds canned model output through the real validator, the real dedup and the
real store path, then asserts the §7 acceptance criteria:

  - a hallucinated quote never reaches the board
  - a commitment repeated across two meetings is one card reading 2x
  - re-running the sync produces zero duplicate cards

    .venv/bin/python scratch/test_extraction.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pkm import config, db, dedup, extract, sync  # noqa: E402

config.ME_ALIASES = ["Alex", "me", "I"]

WEEK_1 = """---
title: Weekly with Maya
date: 2026-08-03
participants: [Alex, Maya]
id: w1
---

Alex: I'll audit all the team LinkedIn profiles and align messaging by vertical. Should take 3-4 days.
Maya: Good. I'll send you the Beacon login today so you can dig in.
Maya: We should probably think about YouTube at some point.
"""

WEEK_2 = """---
title: Weekly with Maya
date: 2026-08-10
participants: [Alex, Maya]
id: w2
---

Maya: Where are we on the profiles?
Alex: Still open — I'll audit the team LinkedIn profiles and align the messaging by vertical this week.
Maya: Fine. Let me get you the trade show banner by Thursday.
"""

# What the model returns per meeting. Two of the week-1 items are deliberately
# bad in ways the code must catch.
CANNED = {
    "Weekly with Maya::2026-08-03": {
        "commitments": [
            {   # good
                "task": "Audit the team LinkedIn profiles and align messaging by vertical",
                "direction": "mine", "owner": "me", "due_date": "2026-08-07",
                "quote": "I'll audit all the team LinkedIn profiles and align messaging by vertical.",
                "speaker": "Alex",
            },
            {   # good
                "task": "Send the Beacon login",
                "direction": "theirs", "owner": "Maya", "due_date": "2026-08-03",
                "quote": "I'll send you the Beacon login today so you can dig in.",
                "speaker": "Maya",
            },
            {   # HALLUCINATION: nobody said this
                "task": "Book the Lisbon office desks",
                "direction": "theirs", "owner": "Maya", "due_date": None,
                "quote": "I'll book the desks for the Lisbon office on Monday.",
                "speaker": "Maya",
            },
            {   # a topic discussed, not a commitment — and the quote is too thin
                "task": "Think about YouTube",
                "direction": "theirs", "owner": "Maya", "due_date": None,
                "quote": "YouTube", "speaker": "Maya",
            },
            {   # no owner
                "task": "Circle back on pricing",
                "direction": "theirs", "owner": "", "due_date": None,
                "quote": "Maya: We should probably think about YouTube at some point.",
                "speaker": None,
            },
        ]
    },
    "Weekly with Maya::2026-08-10": {
        "commitments": [
            {   # the SAME promise, restated — must merge, not duplicate
                "task": "Audit the team LinkedIn profiles and align the messaging by vertical",
                "direction": "mine", "owner": "Alex", "due_date": None,
                "quote": "I'll audit the team LinkedIn profiles and align the messaging by vertical this week.",
                "speaker": "Alex",
            },
            {   # new
                "task": "Get the trade show banner",
                "direction": "theirs", "owner": "Maya", "due_date": "2026-08-13",
                "quote": "Let me get you the trade show banner by Thursday.",
                "speaker": "Maya",
            },
        ]
    },
}


def fake_extract(episode):
    key = f"{episode['title']}::{episode['started_at']}"
    raw = CANNED[key]
    kept, dropped = extract.validate(raw, episode)   # the real validator
    return {"kept": kept, "dropped": dropped, "usage": {"model": "fake"}}


def check(label, actual, expected):
    ok = actual == expected
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {actual!r}" + ("" if ok else f" != {expected!r}"))
    if not ok:
        raise SystemExit(1)


def check_numbers_are_not_noise() -> None:
    """Two tasks that differ only by an identifier are two tasks.

    Found by the stress harness: twelve "Send report N to Maya" collapsed into
    one row. Stopwording leaves "send report 1041 maya" against "send report 1042
    maya", which scores 0.75 — well over the 0.6 threshold — so dedup merged them
    and an invoice silently vanished.
    """
    print("\ndifferent numbers are different tasks:")
    def item(text):
        return {"task": text, "task_norm": dedup.normalise_text(text)}

    a = item("Send Maya the signed invoice 1041")
    b = item("Send Maya the signed invoice 1042")
    check("they score above the threshold",
          round(dedup.jaccard(a["task_norm"], b["task_norm"]), 2) >= 0.6, True)
    check("and are still not merged", dedup.find_match(a, [b])[0], None)

    check("the same identifier still merges",
          dedup.find_match(item("Send Maya the signed invoice 1041 today"),
                           [a])[0] is not None, True)

    # Only when both sides carry digits: a time on one side must not split a pair.
    timed = {"task": "Send the report by 5pm",
             "task_norm": dedup.normalise_text("Send the report by 5pm")}
    plain = {"task": "Send the report", "task_norm": dedup.normalise_text("Send the report")}
    check("a number on one side only does not split them",
          dedup.find_match(timed, [plain])[0] is not None, True)


def main() -> None:
    check_numbers_are_not_noise()
    print("verbatim-quote check:")
    t = "Alex: I'll audit all the team LinkedIn profiles.\nMaya: Sounds good."
    check("exact match", extract.verify_quote("I'll audit all the team LinkedIn profiles.", t), (True, "exact"))
    check("whitespace/curly tolerated",
          extract.verify_quote("I’ll  audit all the  team LinkedIn profiles.", t), (True, "whitespace"))
    check("invented sentence rejected",
          extract.verify_quote("I'll book the desks on Monday.", t), (False, "not_found"))
    check("fragment rejected", extract.verify_quote("audit", t), (False, "too_short"))
    check("reworded rejected",
          extract.verify_quote("I will audit every team LinkedIn profile.", t), (False, "not_found"))

    # A model reports *rendered* text, so markup delimiters never survive into
    # its quote. Comparing raw bytes therefore tests the source's formatting
    # conventions rather than whether the words were said. These cases span
    # several markup dialects on purpose: the check must not be tuned to any one
    # source, because Slack and email arrive as connectors later (D1).
    print("\n  markup is formatting, not content:")
    md = (
        "### Next Steps\n\n"
        "- **Raise 50% of outstanding invoice** (Alex)\n"
        "  - Remaining 50% to be raised at official exit.\n"
    )
    check("markdown bold, quoted without the asterisks",
          extract.verify_quote("Raise 50% of outstanding invoice (Alex)", md), (True, "markup"))
    check("markdown bold, quoted with the asterisks",
          extract.verify_quote("**Raise 50% of outstanding invoice** (Alex)", md), (True, "exact"))
    # Text inside a bullet is already an exact substring, so no normalising is
    # needed; the earlier tier correctly wins.
    check("text inside a bullet needs no normalising",
          extract.verify_quote("Remaining 50% to be raised at official exit.", md), (True, "exact"))
    check("bullet character differs between quote and source",
          extract.verify_quote("- Cover tools and automations",
                               "• Cover tools and automations"), (True, "markup"))
    # Delimiters mid-span are the case that needs normalising; delimiters that
    # merely wrap the whole quote leave an exact substring behind.
    check("slack single-asterisk bold inside the quoted span",
          extract.verify_quote("I will send the pricing sheet by Wednesday",
                               "Nina: I will *send the pricing sheet* by Wednesday"),
          (True, "markup"))
    check("strikethrough delimiters go, the struck words stay",
          extract.verify_quote("Cover tools and automations",
                               "- Cover ~tools~ and automations"), (True, "markup"))
    check("skipping a word in the source is still rejected",
          extract.verify_quote("I will send the pricing sheet by Wednesday",
                               "Nina: *I will send the pricing sheet* today by Wednesday"),
          (False, "not_found"))
    check("inline code and underscore italics",
          extract.verify_quote("Cover tools, automations, email infra",
                               "- Cover `tools`, _automations_, email infra."), (True, "markup"))
    check("html email tags",
          extract.verify_quote("I will circulate the deck on Monday",
                               "<p>I will <b>circulate the deck</b> on Monday</p>"), (True, "markup"))
    check("markdown link, quoted by its label",
          extract.verify_quote("I will review the transition doc tonight",
                               "- I will review the [transition doc](https://x.co/a?b=1) tonight"),
          (True, "markup"))

    print("\n  stripping markup is not a licence to invent:")
    check("changed number rejected",
          extract.verify_quote("Raise 100% of outstanding invoice (Alex)", md), (False, "not_found"))
    check("reordered words rejected",
          extract.verify_quote("Outstanding invoice raise 50% (Alex)", md), (False, "not_found"))
    check("extra clause rejected",
          extract.verify_quote("Raise 50% of outstanding invoice by Friday (Alex)", md),
          (False, "not_found"))
    check("item from another meeting rejected",
          extract.verify_quote("Book the offsite venue next week (Maya)", md), (False, "not_found"))
    check("two lines welded together rejected",
          extract.verify_quote("Raise 50% of outstanding invoice Cover tools", md), (False, "not_found"))

    print("\ndedup (jaccard over stopworded tokens):")
    a = dedup.normalise_text("Audit the team LinkedIn profiles and align messaging by vertical")
    b = dedup.normalise_text("Audit the team LinkedIn profiles and align the messaging by vertical")
    c = dedup.normalise_text("Get the trade show banner from Maya")
    print(f"        a = {a}")
    check("restatement scores >= 0.6", round(dedup.jaccard(a, b), 3) >= 0.6, True)
    check("different task scores < 0.6", dedup.jaccard(a, c) < 0.6, True)
    check("owner alias folds to me", dedup.normalise_owner("Alex", "theirs"), "me")
    check("direction mine forces me", dedup.normalise_owner("Maya", "mine"), "me")
    check("other person kept", dedup.normalise_owner("Maya", "theirs"), "maya")

    print("\na pronoun is not a person, so it is not an owner:")
    # A card owned by "you" is one nobody ever picks up, and it poisons dedup,
    # which groups still-open work by owner. The prompt says so; this is the
    # backstop for when the model says it anyway.
    episode = {"transcript": "I want you to write the comparison blogs by Friday.",
               "kind": "meeting"}
    def owned(owner, direction="theirs"):
        return extract.validate({"commitments": [{
            "task": "Write the comparison blogs", "direction": direction,
            "owner": owner, "due_date": None, "speaker": None,
            "quote": "I want you to write the comparison blogs by Friday."}]}, episode)

    print("    second person resolves to the user, because these notes are theirs:")
    for pronoun in ("you", "You", "yourself"):
        kept, _ = owned(pronoun)
        # Dropping would throw away a real task over a pronoun: every note Ruger
        # reads is the user's own recording, so "you" means them. MiniMax M2.7
        # answers `you`/`theirs` on this sentence every single time.
        check(f"{pronoun!r} is the user", (len(kept), kept[0]["owner"], kept[0]["direction"]),
              (1, "me", "mine"))

    print("    and one that names nobody at all is refused:")
    for pronoun in ("we", "they", "someone", "the team", "TBD"):
        kept, drops = owned(pronoun)
        check(f"{pronoun!r} refused",
              (len(kept), "pronoun, not a person" in drops[0]["_reason"]), (0, True))
    kept, _ = owned("Maya")
    check("a real name is kept", kept[0]["owner"], "Maya")
    # `mine` is folded to `me` regardless, so it can never be ambiguous — and a
    # note that addresses the user as "you" resolves there, not into a drop.
    kept, _ = owned("you", "mine")
    check("but mine is never ambiguous", kept[0]["owner"], "me")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "week1.md").write_text(WEEK_1, encoding="utf-8")
        conn = db.connect(":memory:")

        print("\nweek 1 sync:")
        s1 = sync.run_sync(conn, extract_fn=fake_extract, inbox_path=root)
        check("extracted", s1["extracted"], 1)
        check("created", s1["created"], 2)
        check("dropped (hallucination, fragment, no owner)", s1["dropped"], 3)
        check("board size", s1["tasks"], 2)

        drops = conn.execute("SELECT reason FROM extraction_drops ORDER BY reason").fetchall()
        check("drop reasons", [d["reason"] for d in drops],
              ["no owner named", "quote not_found", "quote too_short"])

        print("\nweek 2 sync (same promise restated):")
        (root / "week2.md").write_text(WEEK_2, encoding="utf-8")
        s2 = sync.run_sync(conn, extract_fn=fake_extract, inbox_path=root)
        check("extracted", s2["extracted"], 1)
        check("created", s2["created"], 1)
        check("merged", s2["merged"], 1)
        check("board size (not 4)", s2["tasks"], 3)

        repeated = conn.execute(
            "SELECT task, mention_count, mentions, due_date FROM commitments WHERE mention_count > 1"
        ).fetchall()
        check("one repeated card", len(repeated), 1)
        check("reads 2x", repeated[0]["mention_count"], 2)
        check("mentions oldest first", repeated[0]["mentions"], '["2026-08-03", "2026-08-10"]')
        check("first due date survives the merge", repeated[0]["due_date"], "2026-08-07")

        print("\nre-sync (both files unchanged):")
        s3 = sync.run_sync(conn, extract_fn=fake_extract, inbox_path=root)
        check("nothing re-extracted", s3["extracted"], 0)
        check("board size unchanged", s3["tasks"], 3)
        counts = conn.execute(
            "SELECT mention_count FROM commitments WHERE mention_count > 1"
        ).fetchone()
        check("pip count not inflated", counts["mention_count"], 2)

        print("\nre-extract after editing week 2:")
        # An episode that has already been extracted goes down the MERGE path, not
        # the clearing one, so an unattended re-import cannot delete rows a push
        # has already linked to a Notion page. It is counted as a refresh.
        (root / "week2.md").write_text(WEEK_2 + "\nMaya: noted.\n", encoding="utf-8")
        s4 = sync.run_sync(conn, extract_fn=fake_extract, inbox_path=root)
        check("refreshed rather than rebuilt", s4["refreshed"], 1)
        check("not counted as a first extraction", s4["extracted"], 0)
        check("board size still 3", s4["tasks"], 3)
        again = conn.execute(
            "SELECT mention_count FROM commitments WHERE mention_count > 1"
        ).fetchone()
        check("still 2x, not 3x", again["mention_count"], 2)

        # A flaky endpoint must not cost you a meeting. Seen live: Featherless
        # returned a Cloudflare 522 on one of two episodes.
        print("\ntransient provider failure:")
        (root / "week3.md").write_text(
            WEEK_2.replace("id: w2", "id: w3").replace("2026-08-10", "2026-08-17"),
            encoding="utf-8",
        )
        CANNED["Weekly with Maya::2026-08-17"] = CANNED["Weekly with Maya::2026-08-10"]

        calls = {"n": 0}

        def flaky(episode):
            if episode["started_at"] == "2026-08-17" and calls["n"] == 0:
                calls["n"] += 1
                raise RuntimeError("Error code: 522 - origin connection timed out")
            return fake_extract(episode)

        before = s4["tasks"]
        s5 = sync.run_sync(conn, extract_fn=flaky, inbox_path=root)
        check("failure reported, not swallowed", len(s5["errors"]), 1)
        check("nothing extracted from the failed episode", s5["extracted"], 0)
        check("board untouched by the failure", s5["tasks"], before)
        check("failed episode stays queued",
              [e["external_id"] for e in db.episodes_needing_extraction(conn)], ["w3"])

        print("\nretry on the next sync:")
        s6 = sync.run_sync(conn, extract_fn=flaky, inbox_path=root)
        check("no errors second time", s6["errors"], [])
        check("now extracted", s6["extracted"], 1)
        check("nothing left queued", len(db.episodes_needing_extraction(conn)), 0)
        check("the repeat merged again, still one card",
              conn.execute("SELECT COUNT(*) AS n FROM commitments").fetchone()["n"], before)
        check("now reads 3x",
              conn.execute("SELECT MAX(mention_count) AS m FROM commitments").fetchone()["m"], 3)

        print("\nboard:")
        from pkm.__main__ import print_table
        print_table(conn)

        print("\nOK — hallucination dropped, restatement merged to one 2x card, re-runs clean.")


if __name__ == "__main__":
    main()
