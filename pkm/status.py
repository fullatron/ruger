"""What the board looks like right now, and whether the timer is still alive.

Three renderings of one snapshot: a human line for the terminal, JSON for
anything else, and SwiftBar's menu format for the macOS menu bar item.

**The timer's heartbeat is the mtime of its log.** `scripts/wispr-tick.sh` prints
a dated header on every wake, including the idle ones that skip Notion, so a log
that has stopped moving means the agent has stopped running. Nothing here writes
a heartbeat file, deliberately: a heartbeat that a process has to remember to
update is one that keeps saying "alive" after the interesting part has died.

The formatting lives here rather than in the plugin so that the plugin stays a
three-line wrapper with no `jq` dependency, and so this is testable without a
menu bar.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import config, db

# The tick wakes every 300s. Three missed wakes is the point where this stops
# being a slow machine and starts being a stopped agent. A laptop that slept is
# the common false positive, and it resolves itself on the next wake.
STALE_AFTER = 900

LABEL = "ai.ruger.wispr"

# The board's own palette, so the menu reads as part of the same product.
GREEN = "#4dab9a"
RED = "#eb5757"
GREY = "#8b8b8b"


def log_path() -> Path:
    return Path(config.env("PKM_TICK_LOG", "~/.pkm/logs/wispr.log")).expanduser()


def url_for() -> str:
    """Read at call time, so a non-default PKM_PORT reaches the menu too."""
    return f"http://{config.SERVER_HOST}:{config.SERVER_PORT}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def tick(now: datetime | None = None) -> dict:
    """When the timer last woke, from its log's mtime."""
    path = log_path()
    now = now or _now()
    if not path.exists():
        return {"log": str(path), "at": None, "age": None, "stale": True, "ran": False}

    at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    age = max(0, int((now - at).total_seconds()))
    return {
        "log": str(path),
        "at": at.isoformat(timespec="seconds"),
        "age": age,
        "stale": age > STALE_AFTER,
        "ran": True,
    }


def snapshot(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    board = db.board_summary(conn)
    return {
        "board": board,
        "tick": tick(now),
        "url": url_for(),
        "db": str(config.DB_PATH),
        "inbox": str(config.INBOX),
    }


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def ago(seconds: int | None) -> str:
    """Coarse on purpose: 'about an hour ago' beats a spurious 61 minutes."""
    if seconds is None:
        return "never"
    if seconds < 90:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''} ago"


def _headline(snap: dict) -> str:
    t = snap["tick"]
    if not t["ran"]:
        return "timer has never run"
    if t["stale"]:
        return f"timer looks stopped, last tick {ago(t['age'])}"
    return f"last tick {ago(t['age'])}"


def human(snap: dict) -> str:
    b, lines = snap["board"], []
    lines.append(f"board      {plural(b['total'], 'commitment')} "
                 f"({b['todo']} to do, {b['doing']} doing, {b['done']} done)")
    lines.append(f"           {b['mine']} mine, {b['theirs']} theirs"
                 + (f", {b['overdue']} overdue" if b["overdue"] else ""))
    lines.append(f"notes      {b['notes']}"
                 + (f", last extracted {b['last_extracted']}" if b["last_extracted"] else ""))
    lines.append(f"notion     {b['pushed']} of {b['total']} pushed")
    lines.append(f"timer      {_headline(snap)}")
    lines.append(f"           {snap['tick']['log']}")
    lines.append(f"board at   {snap['url']}")
    return "\n".join(lines)


def swiftbar(snap: dict) -> str:
    """SwiftBar's plugin format: title, `---`, then menu items.

    A filled dot means the timer ticked recently, a hollow one that it has not.
    The count rides in the title so the board's size is visible without opening
    the menu, which is the entire reason to have a menu bar item.
    """
    b, t = snap["board"], snap["tick"]
    fresh = t["ran"] and not t["stale"]
    dot = "◉" if fresh else "◌"
    out = [f"{dot} {b['total']} | color={GREEN if fresh else RED}", "---"]

    headline = f"Ruger · {_headline(snap)}"
    out.append(headline if fresh else f"{headline} | color={RED}")
    if not fresh:
        out.append(f"Restart it: launchctl kickstart -p gui/{os.getuid()}/{LABEL} "
                   f"| color={GREY}")
    out.append("--")

    out.append(f"{plural(b['total'], 'commitment')} · {b['todo']} to do "
               f"· {b['doing']} doing · {b['done']} done | color={GREY}")
    if b["overdue"]:
        out.append(f"{b['overdue']} overdue | color={RED}")
    out.append(f"{b['mine']} mine · {b['theirs']} theirs | color={GREY}")
    out.append(f"{plural(b['notes'], 'note')} · {b['pushed']} pushed to Notion "
               f"| color={GREY}")

    out.append("---")
    out.append(f"Open board | href={snap['url']}")
    # `terminal=false` keeps a stray Terminal window from opening on every click.
    out.append(f"Run a tick now | bash=/bin/launchctl param1=kickstart param2=-p "
               f"param3=gui/{os.getuid()}/{LABEL} terminal=false refresh=true")
    out.append(f"Open log | href=file://{t['log']}")
    out.append("Refresh | refresh=true")
    return "\n".join(out)


def render(conn: sqlite3.Connection, mode: str = "human") -> str:
    snap = snapshot(conn)
    if mode == "json":
        return json.dumps(snap, indent=2)
    if mode == "swiftbar":
        return swiftbar(snap)
    return human(snap)
