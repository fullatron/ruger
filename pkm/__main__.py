"""One command.

    python -m pkm                 sync the inbox, then serve the board
    python -m pkm sync            ingest + extract only
    python -m pkm sync --table    ... and print the table (§7 eyeball check, no UI)
    python -m pkm table           print the board as a table
    python -m pkm serve           serve without syncing
    python -m pkm drops           what the verbatim-quote check threw away
    python -m pkm doctor          check inbox, db, credentials
    python -m pkm status          board counts + whether the timer is alive

    python -m pkm notion          who the token is, and what it can see
    python -m pkm notion setup    create (or adopt) the Notion database
    python -m pkm push --dry-run  what would go to Notion
    python -m pkm push            send it
    python -m pkm pull            bring status changes back
"""

from __future__ import annotations

import argparse
import sys
from contextlib import closing
from pathlib import Path

from . import config, db, server, sync


def _w(text: str, width: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def print_table(conn) -> None:
    tasks = db.all_tasks(conn)
    if not tasks:
        print("no commitments yet — an empty board is correct output, not a failure.")
        return

    header = f"{'DIR':<6} {'OWNER':<12} {'DUE':<10} {'×':>3}  {'STATUS':<6} {'TASK':<52} MEETING"
    print(header)
    print("-" * len(header))
    for t in tasks:
        print(
            f"{t['direction']:<6} {_w(t['owner'], 12):<12} "
            f"{(t['due_date'] or '—'):<10} {t['mention_count']:>3}  "
            f"{t['status']:<6} {_w(t['task'], 52):<52} {_w(t['meeting'], 30)}"
        )
    print(f"\n{len(tasks)} commitments "
          f"({sum(1 for t in tasks if t['direction'] == 'mine')} mine, "
          f"{sum(1 for t in tasks if t['direction'] == 'theirs')} theirs, "
          f"{sum(1 for t in tasks if t['mention_count'] > 1)} repeated)")


def print_drops(conn) -> None:
    rows = conn.execute(
        """SELECT d.reason, d.payload, e.title
             FROM extraction_drops d JOIN episodes e ON e.id = d.episode_id
         ORDER BY d.id DESC LIMIT 40"""
    ).fetchall()
    if not rows:
        print("nothing dropped.")
        return
    import json as _json
    for r in rows:
        payload = _json.loads(r["payload"])
        print(f"[{r['reason']}] {r['title']}")
        print(f"    task:  {payload.get('task', '(none)')}")
        if payload.get("quote"):
            print(f"    quote: {payload['quote'][:150]!r}")
        print()
    print(f"{len(rows)} most recent drops. These never reached the board.")


def report(stats: dict) -> None:
    print(
        f"\n{stats['files']} files &rarr; {stats['events_new']} new events, "
        f"{stats['episodes_new']} new episodes".replace("&rarr;", "->")
    )
    print(
        f"{stats['extracted']} episodes extracted -> "
        f"{stats['created']} new commitments, {stats['merged']} merged into existing, "
        f"{stats['dropped']} dropped by the quote check"
    )
    if stats.get("refreshed"):
        # A changed transcript is merged, not rebuilt, so say what survived.
        print(
            f"{stats['refreshed']} episode(s) refreshed -> "
            f"{stats.get('updated', 0)} updated in place, "
            f"{stats.get('protected', 0)} kept as you edited them, "
            f"{stats.get('unmatched', 0)} kept though no longer returned"
        )
    if stats["usage"]:
        tin = sum(u["input_tokens"] for u in stats["usage"])
        tout = sum(u["output_tokens"] for u in stats["usage"])
        print(f"tokens: {tin:,} in / {tout:,} out  (model: {stats['usage'][0]['model']})")
    for problem in stats["problems"]:
        print(f"  ! inbox: {problem}")
    for err in stats["errors"]:
        print(f"  ! extraction: {err}")
    print(f"board now holds {stats['tasks']} commitments.")


def list_models(pattern: str = "") -> int:
    """Ask an OpenAI-compatible endpoint what it serves. Handy for checking a
    model id before wiring it in — some catalogues have tens of thousands."""
    if config.PROVIDER != "openai":
        print(f"`models` only applies to the openai provider (currently: {config.PROVIDER})")
        return 1
    try:
        from openai import OpenAI

        client = OpenAI(api_key=config.API_KEY, base_url=config.BASE_URL, timeout=60.0)
        ids = sorted(m.id for m in client.models.list().data)
    except Exception as exc:
        print(f"could not list models from {config.BASE_URL}: {exc}")
        return 1

    matches = [i for i in ids if pattern.lower() in i.lower()] if pattern else ids
    print(f"{len(ids):,} models at {config.BASE_URL}"
          + (f"; {len(matches):,} matching {pattern!r}" if pattern else ""))
    for i in matches[:60]:
        print(f"  {'*' if i == config.MODEL else ' '} {i}")
    if len(matches) > 60:
        print(f"  … and {len(matches) - 60:,} more — narrow the search")
    if config.MODEL and config.MODEL not in ids:
        print(f"\n  ! PKM_MODEL={config.MODEL!r} is NOT in this catalogue")
    return 0


def notion_status() -> int:
    """Who the token belongs to and what it can reach. No writes."""
    from .connectors import notion

    print(f"token      {config.redact(config.NOTION_TOKEN)}")
    print(f"database   {config.NOTION_DB or '(not set — run `pkm notion setup`)'}")
    if config.NOTION_PARENT:
        print(f"parent     {config.NOTION_PARENT}")

    try:
        who = notion.whoami()
    except notion.NotionError as exc:
        print(f"\n  ! {exc}")
        return 1
    print(f"\nconnected as {who['name']}"
          + (f" in {who['workspace']}" if who["workspace"] else ""))

    if config.NOTION_DB:
        try:
            title = notion.title_property(config.NOTION_DB)
            pages = notion.all_pages()
            print(f"database reachable, {len(pages)} page(s), title column {title!r}")
        except notion.NotionError as exc:
            print(f"  ! database: {exc}")
            return 1
        return 0

    try:
        pages = notion.shared_pages()
    except notion.NotionError as exc:
        print(f"  ! {exc}")
        return 1
    if not pages:
        print("\nnothing is shared with this integration yet. Open the Notion page")
        print("you want the board created under, then use its ••• menu ->")
        print("Connections -> and pick this integration.")
        return 1
    print("\npages it can see (use one as the parent):")
    for p in pages:
        print(f"  {p['id']}  {_w(p['title'], 50)}")
    return 0


def notion_setup(parent: str = "", database: str = "", title: str = "") -> int:
    """Create the board, or adopt one that already exists. Writes .env."""
    from . import settings
    from .connectors import notion

    if database:
        target = config.notion_id(database)
        if not target:
            print(f"could not find a Notion id in {database!r}")
            return 1
        try:
            added = notion.ensure_schema(target)
        except notion.NotionError as exc:
            print(f"! {exc}")
            return 1
        print(f"adopted database {target}")
        print("added properties: " + (", ".join(added) if added else "none, it already fits"))
    else:
        parent_id = config.notion_id(parent) or config.NOTION_PARENT
        if not parent_id:
            print("need a parent page: `pkm notion setup --parent <page url or id>`")
            print("Run `pkm notion` to list pages the integration can see.")
            return 1
        try:
            created = notion.create_database(
                parent_id, title or "Ruger — commitments")
        except notion.NotionError as exc:
            print(f"! {exc}")
            return 1
        target = config.notion_id(created["id"])
        print(f"created database {target}")
        if created.get("url"):
            print(f"           {created['url']}")

    settings.write_env_file({"PKM_NOTION_DB": target})
    print(f"saved PKM_NOTION_DB to {config.ENV_FILE}")
    print("\nNext: `pkm push --dry-run`, then `pkm push`.")
    return 0


def report_push(stats: dict, dry_run: bool) -> None:
    if not stats["total"]:
        print("no commitments to push yet.")
        return
    if dry_run:
        print(f"{stats['total']} commitment(s) would go to Notion:\n")
        for item in stats["plan"]:
            verb = "CREATE" if item["action"] == "create" else "update"
            print(f"  {verb:<6} {_w(item['task'], 56):<56} "
                  f"{_w(item['owner'], 12):<12} {item['due_date'] or '—'}")
        print(f"\n{stats['created']} new page(s), {stats['updated']} existing "
              f"page(s) refreshed. Nothing was sent.")
        return

    print(f"pushed {stats['total']} commitment(s): {stats['created']} created, "
          f"{stats['updated']} updated"
          + (f", {stats['failed']} failed" if stats["failed"] else ""))
    for err in stats["errors"]:
        print(f"  ! {err}")


def report_pull(stats: dict, dry_run: bool) -> None:
    print(f"read {stats['pages']} page(s) from Notion")
    for move in stats["moves"]:
        print(f"  {move['from']:>5} -> {move['to']:<6} {_w(move['task'], 60)}")
    verb = "would change" if dry_run else "changed"
    print(f"\n{stats['changed']} {verb}, {stats['unchanged']} already in step")
    if stats["unreadable"]:
        print(f"  {stats['unreadable']} page(s) had a Status value Ruger does not "
              f"recognise, left alone")
    if stats["unlinked"]:
        print(f"  {stats['unlinked']} page(s) added in Notion by hand, ignored")
    if stats["orphaned"]:
        print(f"  {stats['orphaned']} page(s) whose commitment no longer exists here"
              + (f" — {stats['archived']} archived" if stats["archived"]
                 else " (pass --prune to archive them)"))
    for gone in stats["missing"]:
        print(f"  ! page deleted in Notion, kept here: {_w(gone['task'], 60)}")


def doctor() -> None:
    import os

    print(f"inbox      {config.INBOX}")
    if config.INBOX.exists():
        files = [p for p in config.INBOX.rglob("*")
                 if p.is_file() and p.suffix.lower() in {".md", ".markdown", ".txt"}
                 and not p.name.startswith(".")]
        print(f"           {len(files)} markdown file(s)")
        for p in files[:10]:
            print(f"             {p.name}")
    else:
        print("           MISSING — create it and drop Granola exports in")

    print(f"database   {config.DB_PATH}"
          f"{'' if config.DB_PATH.exists() else '  (will be created)'}")
    if config.DB_PATH.exists():
        with closing(db.connect()) as conn:
            for table in ("events", "episodes", "commitments"):
                n = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
                print(f"           {table}: {n}")

    print(f"env file   {config.ENV_FILE}"
          f"{'' if config.ENV_FILE.exists() else '  (absent — using process env only)'}")
    print()
    print(f"provider   {config.PROVIDER}")
    print(f"base url   {config.BASE_URL or '(provider default)'}")
    print(f"model      {config.MODEL or '(NOT SET — required for the openai provider)'}")
    print(f"api key    {config.redact(config.API_KEY)}")
    if config.CONTEXT_LENGTH:
        print(f"context    {config.CONTEXT_LENGTH:,} tokens (over-long transcripts refused up front)")

    if config.PROVIDER == "openai":
        print("           note: schema enforcement is NOT available on this path —")
        print("           the verbatim-quote check is what protects the board.")
        if not config.API_KEY:
            print("           ! set PKM_API_KEY")
        if not config.MODEL:
            print("           ! set PKM_MODEL (try: python -m pkm models <substring>)")
    else:
        has_key = bool(
            config.API_KEY
            or os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        )
        if not has_key:
            print("           ! extraction needs one of:")
            print("               export ANTHROPIC_API_KEY=sk-ant-...")
            print("               ant auth login        (a profile the SDK reads)")

    print()
    print(f"me         {', '.join(config.ME_ALIASES)}   (set PKM_ME to your name)")
    print(f"dedup      jaccard >= {config.DEDUP_THRESHOLD}")

    print()
    print(f"notion     {config.redact(config.NOTION_TOKEN)}")
    print(f"board      {config.NOTION_DB or '(not set — run `pkm notion setup`)'}")
    if config.DB_PATH.exists():
        with closing(db.connect()) as conn:
            total = conn.execute("SELECT COUNT(*) AS n FROM commitments").fetchone()["n"]
            sent = conn.execute(
                "SELECT COUNT(*) AS n FROM commitments WHERE external_id IS NOT NULL"
            ).fetchone()["n"]
        print(f"           {sent} of {total} commitment(s) pushed")
    if config.NOTION_TOKEN and not config.NOTION_DB:
        print("           ! no database yet: `pkm notion setup --parent <page url>`")

    # Prove the provider actually constructs, without spending a call.
    try:
        from .providers import get_provider

        print(f"resolved   {get_provider().describe()}")
    except Exception as exc:
        print(f"resolved   FAILED: {exc}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pkm", description="Ruger — meetings to a commitment board")
    sub = parser.add_subparsers(dest="cmd")

    p_sync = sub.add_parser("sync", help="ingest the inbox and extract commitments")
    p_sync.add_argument("--table", action="store_true", help="print the board as a table after")
    p_sync.add_argument("--inbox", help="override the inbox path")
    p_sync.add_argument("--push", action="store_true",
                        help="send the result to Notion when it is done")

    p_notion = sub.add_parser("notion", help="check the Notion connection")
    notion_sub = p_notion.add_subparsers(dest="notion_cmd")
    p_nsetup = notion_sub.add_parser("setup", help="create or adopt the database")
    p_nsetup.add_argument("--parent", default="",
                          help="page to create the database under (url or id)")
    p_nsetup.add_argument("--database", default="",
                          help="adopt an existing database instead (url or id)")
    p_nsetup.add_argument("--title", default="", help="database title")

    p_push = sub.add_parser("push", help="send commitments to Notion")
    p_push.add_argument("--dry-run", action="store_true", help="print, send nothing")
    p_push.add_argument("--limit", type=int, help="only the first N (a trial run)")

    p_pull = sub.add_parser("pull", help="bring status changes back from Notion")
    p_pull.add_argument("--dry-run", action="store_true", help="print, change nothing")
    p_pull.add_argument("--prune", action="store_true",
                        help="archive Notion pages whose commitment is gone here")

    sub.add_parser("unlink", help="forget every Notion page id (next push recreates)")

    p_serve = sub.add_parser("serve", help="serve the board")
    p_serve.add_argument("--port", type=int)
    p_serve.add_argument("--host")

    sub.add_parser("table", help="print the board as a table")
    sub.add_parser("drops", help="show what the quote check rejected")
    sub.add_parser("doctor", help="check configuration")

    p_status = sub.add_parser("status", help="board counts and whether the timer is alive")
    p_status.add_argument("--json", action="store_true", help="machine-readable")
    p_status.add_argument("--swiftbar", action="store_true",
                          help="SwiftBar menu format (see scripts/ruger.5m.sh)")

    p_reval = sub.add_parser(
        "revalidate",
        help="re-check stored drops against the current quote check (no model calls)")
    p_reval.add_argument("--apply", action="store_true",
                         help="actually put the recovered commitments on the board")

    p_wispr = sub.add_parser("wispr", help="import Wispr Flow meetings into the inbox")
    p_wispr.add_argument("--dry-run", action="store_true", help="print, write nothing")
    p_wispr.add_argument("--limit", type=int, help="only the most recent N")
    p_wispr.add_argument("--home", help="override the Wispr Flow data directory")
    p_wispr.add_argument("--inbox", help="override the inbox path")

    p_models = sub.add_parser("models", help="list models your endpoint serves")
    p_models.add_argument("pattern", nargs="?", default="", help="filter substring")

    args = parser.parse_args(argv)
    cmd = args.cmd or "run"

    if cmd == "doctor":
        doctor()
        return 0

    if cmd == "models":
        return list_models(args.pattern)

    if cmd == "revalidate":
        with closing(db.connect()) as conn:
            r = sync.revalidate_drops(conn, apply=args.apply)
        if not r["checked"]:
            print("nothing was ever dropped, so there is nothing to re-check.")
            return 0
        print(f"re-checked {r['checked']} dropped commitment(s) against the "
              f"current quote check.\n")
        for item in r["recovered"]:
            print(f"  RECOVER  {item['task']}")
            print(f"           {item['owner']} · {item['meeting']}")
            print(f"           was {item['was']}, now matches after {item['matched']} "
                  f"normalising")
        if r["still_dropped"]:
            print(f"\n  {r['still_dropped']} still rejected (see `pkm drops`)")
        if args.apply:
            print(f"\napplied: {r['created']} added, {r['merged']} merged into existing.")
        elif r["recovered"]:
            print(f"\n{len(r['recovered'])} recoverable. Re-run with --apply to put "
                  f"them on the board.")
        return 0

    if cmd == "notion":
        if getattr(args, "notion_cmd", None) == "setup":
            return notion_setup(args.parent, args.database, args.title)
        return notion_status()

    if cmd == "push":
        from .connectors import notion

        with closing(db.connect()) as conn:
            try:
                stats = notion.push(conn, dry_run=args.dry_run, limit=args.limit)
            except notion.NotionError as exc:
                print(f"! {exc}")
                return 1
            report_push(stats, args.dry_run)
        return 1 if stats["failed"] else 0

    if cmd == "pull":
        from .connectors import notion

        with closing(db.connect()) as conn:
            try:
                stats = notion.pull(conn, dry_run=args.dry_run, prune=args.prune)
            except notion.NotionError as exc:
                print(f"! {exc}")
                return 1
            report_pull(stats, args.dry_run)
        return 0

    if cmd == "unlink":
        with closing(db.connect()) as conn:
            with db.transaction(conn):
                n = db.clear_push_state(conn)
        print(f"forgot {n} Notion page link(s). The next push recreates them.")
        return 0

    if cmd == "wispr":
        from .connectors import wispr

        try:
            stats = wispr.export(wispr_home=args.home, inbox=Path(args.inbox)
                                 if args.inbox else None,
                                 limit=args.limit, dry_run=args.dry_run)
        except wispr.WisprError as exc:
            print(f"! {exc}")
            return 1

        verb = "would write" if args.dry_run else "wrote"
        print(f"{stats['meetings']} finished meeting(s) in Wispr Flow")
        print(f"  {verb} {stats['written']}, {stats['unchanged']} already current")
        for name in stats["files"]:
            print(f"    {name}")
        for name in stats["removed"]:
            print(f"  removed stale {name} (same meeting, retitled)")
        for problem in stats["skipped"]:
            print(f"  skipped {problem}")
        if stats["written"] and not args.dry_run:
            print("\nnow run: .venv/bin/python -m pkm sync --push")
        return 0

    if cmd == "status":
        from . import status as status_mod

        mode = "json" if args.json else "swiftbar" if args.swiftbar else "human"
        with closing(db.connect()) as conn:
            print(status_mod.render(conn, mode))
        return 0

    if cmd == "table":
        with closing(db.connect()) as conn:
            print_table(conn)
        return 0

    if cmd == "drops":
        with closing(db.connect()) as conn:
            print_drops(conn)
        return 0

    if cmd == "serve":
        server.serve(getattr(args, "host", None), getattr(args, "port", None))
        return 0

    if cmd == "sync":
        with closing(db.connect()) as conn:
            print(f"syncing {config.INBOX} ...")
            stats = sync.run_sync(conn, inbox_path=args.inbox, verbose=True)
            report(stats)
            if args.table:
                print()
                print_table(conn)
            if args.push:
                # A Notion outage must not turn a successful extraction into a
                # failed sync, so this reports and moves on.
                from .connectors import notion

                print()
                try:
                    report_push(notion.push(conn), False)
                except notion.NotionError as exc:
                    print(f"! push skipped: {exc}")
        return 1 if stats["errors"] else 0

    # bare `python -m pkm`: sync, then serve.
    with closing(db.connect()) as conn:
        print(f"syncing {config.INBOX} ...")
        stats = sync.run_sync(conn, verbose=True)
        report(stats)
    print()
    server.serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
