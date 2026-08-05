"""Fuzzy dedup and moving cards by instruction (§11).

    .venv/bin/python scratch/test_instruct.py

Canned judge and canned instruction output through the real validators, the real
merge and the real store path. No model call, no network.

What is being protected:

  - the LLM tie-break is only asked when it could change something, and it can
    never block ingest — a failing judge falls back to the lexical answer;
  - it merges only on a confident, unambiguous, *offered* id;
  - a merge keeps every mention, so evidence that two people asked survives;
  - an instruction cannot touch a row it was not shown, cannot set a value outside
    the four fields, and cannot delete anything;
  - Status reaches Notion for a card an instruction moved, and for nothing else.
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

from pkm import config, db, dedup, instruct, similar  # noqa: E402

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


def seed(conn):
    """Two meetings, because that is what "two people asked me" looks like.

    `resync_mentions` counts one mention per *episode*, so a merge of duplicates
    raised in the same meeting correctly reads 1x — the interesting case is two.
    """
    with db.transaction(conn):
        for n, (day, title) in enumerate(
                [("2026-08-03", "Weekly with Maya"), ("2026-08-04", "Handoff with Nila")],
                start=1):
            conn.execute(
                """INSERT INTO events (id, source, external_id, occurred_at, body,
                                       content_hash, ingested_at)
                   VALUES (?, 'meeting', ?, ?, 'body', ?, 'now')""",
                (n, f"e{n}", day, f"h{n}"))
            conn.execute(
                """INSERT INTO episodes (id, source, external_id, kind, title, started_at,
                                        transcript, content_hash, extracted_at)
                   VALUES (?, 'meeting', ?, 'meeting', ?, ?, 't', ?, ?)""",
                (n, f"ep{n}", title, day, f"h{n}", f"{day}T10:00:00+00:00"))
            conn.execute("INSERT INTO episode_events VALUES (?, ?)", (n, n))


def add(conn, task, *, owner="me", direction="mine", due=None, status="todo",
        pushed=False, edited=0, episode=1):
    with db.transaction(conn):
        cid = db.insert_commitment(conn, {
            "episode_id": episode, "event_id": episode, "task": task,
            "task_norm": dedup.normalise_text(task), "direction": direction,
            "owner": owner, "owner_norm": dedup.normalise_owner(owner, direction),
            "due_date": due, "quote": f"I'll {task.lower()}.", "speaker": owner,
            "occurred_at": "2026-08-03" if episode == 1 else "2026-08-04",
        })
        if status != "todo":
            db.set_status(conn, cid, status)
        if pushed:
            db.mark_pushed(conn, cid, f"page-{cid}", f"https://notion.so/{cid}")
        if edited:
            conn.execute("UPDATE commitments SET edited = 1 WHERE id = ?", (cid,))
    return cid


def main() -> None:
    print("the judge is only asked when it could change the answer:")
    asked = {"n": 0}

    def spy(values):
        asked["n"] += 1
        return {"same_as": None, "confidence": "high", "why": "different"}

    same = {"task": "Send Maya the revised deck",
            "task_norm": dedup.normalise_text("Send Maya the revised deck"),
            "owner": "me"}
    existing = [{"id": 1, "task": "Send Maya the revised deck",
                 "task_norm": dedup.normalise_text("Send Maya the revised deck")}]
    match, score, how = similar.resolve(same, existing, ask=spy)
    check("a lexical match short-circuits", how, "lexical")
    check("no call was made", asked["n"], 0)

    unrelated = {"task": "Buy blades for shaving",
                 "task_norm": dedup.normalise_text("Buy blades for shaving"),
                 "owner": "me"}
    match, score, how = similar.resolve(unrelated, existing, ask=spy)
    check("nothing in common is not worth asking about", how, "none")
    check("still no call", asked["n"], 0)
    check("because overlap is under the floor", score < similar.AMBIGUOUS_FLOOR, True)

    paraphrase = {"task": "Share the handover sheet with Nila",
                  "task_norm": dedup.normalise_text("Share the handover sheet with Nila"),
                  "owner": "me"}
    candidates = [{"id": 7, "task": "Send Nila the KT doc and handover sheet",
                   "task_norm": dedup.normalise_text("Send Nila the KT doc and handover sheet"),
                   "status": "todo", "due_date": None}]
    match, score, how = similar.resolve(paraphrase, candidates, ask=spy)
    check("a plausible pair is asked about", asked["n"], 1)
    check("and a 'different' answer is respected", how, "none")

    print("\n  it merges only on a confident, offered id:")

    def yes(values):
        return {"same_as": 7, "confidence": "high", "why": "one delivery"}

    match, _score, how = similar.resolve(paraphrase, candidates, ask=yes)
    check("a confident yes merges", how, "llm")
    check("onto the row it named", int(match["id"]), 7)

    def unsure(values):
        return {"same_as": 7, "confidence": "low", "why": "might be"}

    check("low confidence is ignored",
          similar.resolve(paraphrase, candidates, ask=unsure)[2], "none")

    def invented(values):
        return {"same_as": 999, "confidence": "high", "why": "sure"}

    check("an id that was never offered is refused",
          similar.resolve(paraphrase, candidates, ask=invented)[2], "none")

    def rubbish(values):
        return ["not", "an", "object"]

    check("a wrong-shaped answer is refused",
          similar.resolve(paraphrase, candidates, ask=rubbish)[2], "none")

    print("\n  and it can never block ingest:")

    def outage(values):
        raise RuntimeError("provider is down")

    check("an outage falls back to lexical",
          similar.resolve(paraphrase, candidates, ask=outage)[2], "none")
    check("a lexical match still works during an outage",
          similar.resolve(same, existing, ask=outage)[2], "lexical")

    os.environ["PKM_FUZZY_DEDUP"] = "0"
    config.reload()
    check("and it can be switched off entirely", similar.enabled(), False)
    check("which skips the call", similar.resolve(paraphrase, candidates, ask=yes)[2],
          "none")
    os.environ.pop("PKM_FUZZY_DEDUP", None)
    config.reload()

    print("\nmerging keeps the evidence that two people asked:")
    with closing(db.connect(TMP / "merge.db")) as conn:
        seed(conn)
        first = add(conn, "Send Nila the KT doc", pushed=True)
        # A second meeting: a different person asking for the same thing.
        second = add(conn, "Share the handover sheet with Nila", due="2026-08-09",
                     episode=2)

        with db.transaction(conn):
            outcome = db.merge_commitments(conn, first, second)
        check("the duplicate is gone", db.get_commitment(conn, second), None)
        check("the survivor stays", db.get_commitment(conn, first) is not None, True)
        row = db.get_commitment(conn, first)
        check("its count covers both", row["mention_count"], 2)
        check("and it took the earlier due date", row["due_date"], "2026-08-09")
        check("the dropped page id comes back for the caller to archive",
              outcome["external_id"], None)

        mentions = conn.execute(
            "SELECT COUNT(*) AS n FROM commitment_mentions WHERE commitment_id = ?",
            (first,)).fetchone()["n"]
        check("both quotes survive as mentions", mentions, 2)
        check("nothing is orphaned",
              conn.execute("SELECT COUNT(*) AS n FROM commitment_mentions "
                           "WHERE commitment_id = ?", (second,)).fetchone()["n"], 0)

        print("\n  the survivor is the one already in Notion:")
        third = add(conn, "Book the trade show banner")
        fourth = add(conn, "Book the banner for the trade show", pushed=True, episode=2)
        pairs = similar.pairs_for_review(conn)
        pair = next(p for p in pairs if {int(p[0]["id"]), int(p[1]["id"])} == {third, fourth})
        check("keeps the pushed row", int(pair[0]["id"]), fourth)

    print("\nan instruction only touches rows it was shown:")
    with closing(db.connect(TMP / "instruct.db")) as conn:
        seed(conn)
        deck = add(conn, "Send Maya the revised deck", due="2026-08-06")
        invoice = add(conn, "Chase Theo about the invoice")
        done_already = add(conn, "Buy blades for shaving", status="done")
        rows = instruct.open_tasks(conn)

        check("done rows are not offered", done_already in [int(r["id"]) for r in rows],
              False)
        check("open ones are", sorted(int(r["id"]) for r in rows), sorted([deck, invoice]))

        kept, refused = instruct.validate([
            {"id": deck, "status": "done"},
            {"id": 4242, "status": "done"},               # never offered
            {"id": done_already, "status": "todo"},       # not open
            {"id": invoice, "status": "sideways"},        # not a status
            {"id": invoice, "due_date": "next friday"},   # not a date
            {"id": invoice, "task": "   "},               # empty rename
        ], rows)
        check("one edit survived", [c["id"] for c in kept], [deck])
        check("and five were refused", len(refused), 5)
        check("the invented id is named", any("4242" in r for r in refused), True)

        print("\n  the four fields, and nothing else:")
        kept, refused = instruct.validate([
            {"id": deck, "status": "doing", "due_date": "2026-08-10",
             "owner": "Maya", "task": "Send Maya the final deck"},
        ], rows)
        check("all four are allowed", sorted(kept[0]["fields"]),
              ["direction", "due_date", "owner", "status", "task"])
        check("owner change flips direction too", kept[0]["fields"]["direction"], "theirs")

        kept, _ = instruct.validate([{"id": deck, "due_date": None}], rows)
        check("a date can be cleared", kept[0]["fields"]["due_date"], None)

        kept, _ = instruct.validate([{"id": deck, "status": "todo"}], rows)
        check("a no-op is not an edit", kept, [])

        print("\n  applying it writes what was asked and nothing more:")

        def canned(text, rows, ask=None, today=None):
            return {"changes": [{"id": deck, "status": "done"},
                                {"id": invoice, "due_date": "2026-08-07"}],
                    "unclear": ["'the other one' matched nothing"], "error": None}

        result = instruct.run(conn, "mark the deck done and push the invoice to friday",
                             plan_fn=canned, push=False)
        check("two changes", len(result["changes"]), 2)
        check("status moved", db.get_commitment(conn, deck)["status"], "done")
        check("date moved", db.get_commitment(conn, invoice)["due_date"], "2026-08-07")
        check("the ambiguity is reported, not guessed", result["unclear"],
              ["'the other one' matched nothing"])
        check("a status move does not freeze the wording",
              db.get_commitment(conn, deck)["edited"], 0)
        check("but a content edit does",
              db.get_commitment(conn, invoice)["edited"], 1)
        check("it reads as one line",
              instruct.summarise(result).startswith("2 tasks updated"), True)

        print("\n  nothing matching means nothing changes:")
        empty = instruct.run(conn, "sort out the thing",
                             plan_fn=lambda *a, **k: {"changes": [], "unclear": [],
                                                      "error": None}, push=False)
        check("no changes", empty["changes"], [])
        check("and it says so", instruct.summarise(empty),
              "Nothing matched that instruction")

    print("\nthe router fails toward 'create', so no work is ever swallowed:")
    check("a command is routed",
          instruct.route("mark the deck one done",
                         ask=lambda v: {"kind": "command"}), "command")
    check("new work is routed",
          instruct.route("buy blades", ask=lambda v: {"kind": "create"}), "create")
    check("an outage becomes create",
          instruct.route("mark it done", ask=lambda v: (_ for _ in ()).throw(
              RuntimeError("down"))), "create")
    check("so does a wrong shape",
          instruct.route("mark it done", ask=lambda v: {"kind": "banana"}), "create")

    print("\nStatus reaches Notion only for a card an instruction moved:")
    sent = []
    with closing(db.connect(TMP / "push.db")) as conn:
        seed(conn)
        moved = add(conn, "Send Maya the deck", pushed=True)
        renamed = add(conn, "Chase Theo about the invoice", pushed=True)
        from pkm.connectors import notion

        def fake_push(c, **kw):
            sent.append(kw)
            return {"created": 0, "updated": len(kw.get("only") or []), "failed": 0,
                    "errors": [], "total": 0, "plan": [], "url": ""}

        original, notion.push = notion.push, fake_push
        try:
            instruct.run(conn, "x", push=True, plan_fn=lambda *a, **k: {
                "changes": [{"id": moved, "status": "done"},
                            {"id": renamed, "task": "Chase Theo about payment"}],
                "unclear": [], "error": None})
        finally:
            notion.push = original

        check("both rows are pushed", sent[0]["only"], {moved, renamed})
        check("only the moved one forces Status", sent[0]["force_status"], {moved})
        # The regression this exists for: §12 made push create-only, so without
        # `resend` an instruction changed the due date locally, said so in the
        # notification, and Notion never heard about it.
        check("and content is re-sent, or a due date never leaves the machine",
              sent[0].get("resend"), True)

    print("\nan instruction is logged even when it changes nothing:")
    with closing(db.connect(TMP / "log.db")) as conn:
        seed(conn)
        task = add(conn, "Send Maya the deck")

        instruct.run(conn, "mark the deck one done", push=False,
                     plan_fn=lambda *a, **k: {"changes": [{"id": task, "status": "done"}],
                                              "unclear": [], "error": None})
        instruct.run(conn, "sort out the thing", push=False,
                     plan_fn=lambda *a, **k: {"changes": [], "unclear": [],
                                              "error": None})
        events = db.recent_events(conn)
        check("both were recorded", [e["action"] for e in events],
              ["instructed", "instructed"])
        check("what you said is kept", events[0]["task"], "sort out the thing")
        check("and so is the fact that it did nothing",
              events[0]["detail"], "nothing matched")
        check("the one that worked says what changed",
              "todo → done" in events[1]["detail"], True)

    print(f"\nOK — {PASSES['n']} assertions. Duplicates merge without losing "
          f"evidence, and an instruction cannot touch what it was not shown.")


if __name__ == "__main__":
    main()
