"""`pkm status` and the menu bar item.

    .venv/bin/python scratch/test_status.py

The load-bearing case is the timer's liveness. The heartbeat is the mtime of the
tick log, so this writes a log file and moves its mtime around rather than
waiting 15 minutes for one to go stale.

No model call, no menu bar, no SwiftBar install: the menu is a string, so it can
be asserted like any other.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp())
LOG = TMP / "wispr.log"

# Real env vars, not just .env: config.reload() would otherwise repoint the
# database at ~/.pkm and this test would report on the real board.
os.environ.update(PKM_DB=str(TMP / "t.db"), PKM_INBOX=str(TMP / "inbox"),
                  PKM_TRASH=str(TMP / "trash"), PKM_ENV_FILE=str(TMP / ".env"),
                  PKM_ME="Alex", PKM_TICK_LOG=str(LOG))
for _k in ("PKM_PROVIDER", "PKM_MODEL", "PKM_API_KEY", "PKM_BASE_URL"):
    os.environ.pop(_k, None)

sys.path.insert(0, str(ROOT))

from pkm import config, db, dedup, status  # noqa: E402

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


def add(conn, task, direction, owner, *, due=None, state="todo", pushed=False):
    cid = db.insert_commitment(conn, {
        "episode_id": 1, "event_id": 1, "task": task,
        "task_norm": dedup.normalise_text(task), "direction": direction,
        "owner": owner, "owner_norm": dedup.normalise_owner(owner, direction),
        "due_date": due, "quote": f"I'll {task.lower()}.", "speaker": owner,
        "occurred_at": "2026-08-03",
    })
    if state != "todo":
        db.set_status(conn, cid, state)
    if pushed:
        db.mark_pushed(conn, cid, f"page-{cid}", f"https://notion.so/{cid}")
    return cid


def touch(age_seconds: int) -> None:
    """Put the tick log's mtime `age_seconds` in the past."""
    LOG.write_text("=== a tick ===\n", encoding="utf-8")
    when = time.time() - age_seconds
    os.utime(LOG, (when, when))


def main() -> None:
    with closing(db.connect()) as conn:
        with db.transaction(conn):
            conn.execute(
                """INSERT INTO events (id, source, external_id, occurred_at, body,
                                       content_hash, ingested_at)
                   VALUES (1, 'meeting', 'e1', '2026-08-03', 'body', 'h', 'now')""")
            conn.execute(
                """INSERT INTO episodes (id, source, external_id, kind, title,
                                        started_at, transcript, content_hash,
                                        extracted_at)
                   VALUES (1, 'meeting', 'ep1', 'meeting', 'Weekly with Maya',
                           '2026-08-03', 'transcript', 'h', '2026-08-03T10:00:00+00:00')""")
            conn.execute("INSERT INTO episode_events VALUES (1, 1)")

        print("an empty board reads as zeros, not None:")
        empty = db.board_summary(conn)
        check("total", empty["total"], 0)
        check("todo", empty["todo"], 0)
        check("overdue", empty["overdue"], 0)
        check("notes counted even with no commitments", empty["notes"], 1)

        print("\ncounts come back split by status, direction and due date:")
        with db.transaction(conn):
            add(conn, "Audit the profiles", "mine", "me", due="2026-01-01")
            add(conn, "Send the login", "theirs", "Maya", state="doing", pushed=True)
            add(conn, "Book the banner", "theirs", "Maya", state="done",
                due="2026-01-01")
            add(conn, "Draft the brief", "mine", "me", due="2099-01-01")

        b = db.board_summary(conn, today="2026-08-05")
        check("total", b["total"], 4)
        check("todo", b["todo"], 2)
        check("doing", b["doing"], 1)
        check("done", b["done"], 1)
        check("mine", b["mine"], 2)
        check("theirs", b["theirs"], 2)
        check("pushed", b["pushed"], 1)
        # The done one is also past its date, and must not be counted: a finished
        # task is not overdue, it is finished.
        check("overdue excludes done and future dates", b["overdue"], 1)

        print("\nthe timer's liveness comes from the log's mtime:")
        check("no log at all means it has never run", status.tick()["ran"], False)
        check("and that counts as stale", status.tick()["stale"], True)

        touch(120)
        fresh = status.tick()
        check("a recent tick is not stale", fresh["stale"], False)
        check("age is in seconds", 110 <= fresh["age"] <= 130, True)

        touch(status.STALE_AFTER + 60)
        check("three missed wakes is stale", status.tick()["stale"], True)
        touch(status.STALE_AFTER - 60)
        check("just inside the window is not", status.tick()["stale"], False)

        print("\n  and it is phrased for a human:")
        check("under 90s", status.ago(30), "just now")
        check("minutes", status.ago(600), "10 min ago")
        check("one hour", status.ago(3600), "1 hour ago")
        check("hours", status.ago(7200), "2 hours ago")
        check("days", status.ago(90000), "1 day ago")
        check("never", status.ago(None), "never")

        print("\nthe board's port is probed, not assumed:")
        # The bug this covers: the menu offered "Open board" whether or not
        # anything was serving, so the link went to a dead port and the click
        # just failed in the browser.
        import socket as _socket

        free = _socket.socket()
        free.bind(("127.0.0.1", 0))
        spare_port = free.getsockname()[1]
        free.close()
        check("a port with no listener reads as down",
              status.board_up("127.0.0.1", spare_port), False)

        listener = _socket.socket()
        listener.bind(("127.0.0.1", 0))
        # Deep backlog on purpose: the probe connects and closes without anyone
        # accepting, so a backlog of 1 fills up and the *second* probe is refused
        # — which reads as "board down" and has nothing to do with the code.
        listener.listen(64)
        live_port = listener.getsockname()[1]
        try:
            check("one with a listener reads as up",
                  status.board_up("127.0.0.1", live_port), True)

            os.environ["PKM_PORT"] = str(live_port)
            config.reload()
            up_menu = status.swiftbar(status.snapshot(conn))
            check("so the menu offers the link",
                  f"Open board | href=http://127.0.0.1:{live_port}" in up_menu, True)
            check("and does not offer to start it", "Start board" in up_menu, False)
        finally:
            listener.close()

        os.environ["PKM_PORT"] = str(spare_port)
        config.reload()
        down_menu = status.swiftbar(status.snapshot(conn))
        check("with nothing serving, no link is offered",
              "Open board | href=" in down_menu, False)
        check("it says so instead", "Board is not running" in down_menu, True)
        check("and offers to start it",
              f"param3=gui/{os.getuid()}/{status.BOARD_LABEL}" in down_menu, True)
        check("the human rendering says so too",
              "NOT RUNNING" in status.render(conn, "human"), True)

        os.environ.pop("PKM_PORT", None)
        config.reload()

        print("\nthe SwiftBar menu renders from the same snapshot:")
        touch(120)
        menu = status.swiftbar(status.snapshot(conn))
        lines = menu.splitlines()
        check("the title carries the count", lines[0].startswith("◉ 4"), True)
        check("fresh is green", status.GREEN in lines[0], True)
        check("title is separated from the menu", lines[1], "---")
        check("says when it last ticked", "last tick 2 min ago" in menu, True)
        check("board is one click away", f"Open board | href={status.url_for()}" in menu,
              True)
        check("a tick can be forced from the menu",
              f"param3=gui/{os.getuid()}/{status.LABEL}" in menu, True)
        check("no terminal window on click", "terminal=false" in menu, True)
        check("overdue is called out", "1 overdue" in menu, True)

        print("\n  a stopped timer looks different at a glance:")
        touch(status.STALE_AFTER + 3600)
        stale = status.swiftbar(status.snapshot(conn))
        check("hollow dot", stale.splitlines()[0].startswith("◌ 4"), True)
        check("and red", status.RED in stale.splitlines()[0], True)
        check("says so in words", "timer looks stopped" in stale, True)
        check("and how to restart it", "launchctl kickstart" in stale, True)

        print("\n  every menu line is one line, or SwiftBar renders junk:")
        for line in stale.splitlines():
            check(f"no stray newline in {line[:28]!r}", "\n" in line, False)

        print("\nthe human and json renderings agree with it:")
        touch(120)
        text = status.render(conn, "human")
        check("counts present", "4 commitments" in text, True)
        check("timer line present", "last tick" in text, True)

        import json as _json
        payload = _json.loads(status.render(conn, "json"))
        check("json carries the board", payload["board"]["total"], 4)
        check("json carries the tick", payload["tick"]["stale"], False)
        check("json is serialisable end to end", isinstance(payload["url"], str), True)

    print(f"\nOK — {PASSES['n']} assertions. Counts are right, and a stopped timer "
          f"is visible without reading a log.")


if __name__ == "__main__":
    main()
