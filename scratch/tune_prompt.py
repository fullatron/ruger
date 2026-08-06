"""Re-extract real notes with the current prompt and print old vs new.

    .venv/bin/python scratch/tune_prompt.py                 # the last 4 notes
    .venv/bin/python scratch/tune_prompt.py --kind capture  # only captures
    .venv/bin/python scratch/tune_prompt.py --limit 8 --episode 14

**COSTS TOKENS** — one model call per note, against whatever provider is
configured. That is the point: `pkm/prompts/` is where the product is won or
lost, and the only way to know whether an edit helped is to run it over notes
you have actually recorded and read the output.

**Writes nothing.** It reads the real database for transcripts and for what is
currently on the board, and prints. No row is inserted, updated or deleted, no
Notion call is made. Safe to run against a live board, which is the whole reason
it exists rather than a temp-database fixture.
"""

from __future__ import annotations

import argparse
import sys
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pkm import db, extract  # noqa: E402

WIDTH = 78


def wrap(text: str, indent: str = "      ") -> str:
    out, line = [], ""
    for word in str(text or "").split():
        if len(line) + len(word) + 1 > WIDTH:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    out.append(line)
    return f"\n{indent}".join(out)


def try_text(text: str, *, kind: str, title: str) -> int:
    """One note, straight from the command line. Touches no database at all."""
    from datetime import date

    episode = {"id": 0, "kind": kind, "title": title, "transcript": text,
               "started_at": date.today().isoformat(), "participants": "[]"}
    print(f"[{kind}] {title}\n  {wrap(text, '  ')}\n")
    result = extract.extract(episode)
    for c in result["kept"]:
        print(f"    · {wrap(c['task'], '      ')}")
        print(f"        {c['owner']} · {c['direction']} · {c['due_date'] or 'no date'}")
        print(f"        quote: {wrap(c['quote'], '               ')}")
    for d in result["dropped"]:
        print(f"    × dropped ({d.get('_reason')}): {d.get('task')}")
    if not result["kept"] and not result["dropped"]:
        print("    (nothing — which is a correct answer for a note with no action in it)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=4, help="how many notes (newest first)")
    ap.add_argument("--kind", help="only 'meeting' or 'capture'")
    ap.add_argument("--episode", type=int, action="append",
                    help="a specific episode id; repeatable")
    ap.add_argument("--text", help="try a note that is not in the database at all")
    ap.add_argument("--title", default="Scratch note",
                    help="the meeting title --text is read under")
    args = ap.parse_args()

    if args.text:
        # Nothing is stored, so `kind` decides which prompt answers and the
        # title stands in for the account context a real note would carry.
        return try_text(args.text, kind=args.kind or "capture", title=args.title)

    with closing(db.connect()) as conn:
        sql = "SELECT * FROM episodes WHERE transcript <> ''"
        params: list = []
        if args.kind:
            sql += " AND kind = ?"
            params.append(args.kind)
        if args.episode:
            sql += f" AND id IN ({','.join('?' * len(args.episode))})"
            params.extend(args.episode)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(args.limit)
        episodes = conn.execute(sql, params).fetchall()

        if not episodes:
            print("no notes matched.")
            return 1

        cost = {"in": 0, "out": 0}
        for episode in episodes:
            row = dict(episode)
            print("=" * (WIDTH + 6))
            print(f"[{row['kind']}] #{row['id']}  {row['title']}")
            print("=" * (WIDTH + 6))

            current = conn.execute(
                "SELECT task, owner, direction, due_date FROM commitments "
                "WHERE episode_id = ? ORDER BY id", (row["id"],)).fetchall()
            print("\n  ON THE BOARD NOW")
            for c in current or []:
                print(f"    · {wrap(c['task'], '      ')}")
                print(f"        {c['owner']} · {c['direction']} · {c['due_date'] or 'no date'}")
            if not current:
                print("    (nothing)")

            try:
                result = extract.extract(row)
            except Exception as exc:                       # noqa: BLE001
                print(f"\n  ! extraction failed: {exc}\n")
                continue

            usage = result.get("usage") or {}
            cost["in"] += int(usage.get("input_tokens") or 0)
            cost["out"] += int(usage.get("output_tokens") or 0)

            print("\n  WITH THE PROMPT AS IT STANDS")
            for c in result["kept"]:
                print(f"    · {wrap(c['task'], '      ')}")
                print(f"        {c['owner']} · {c['direction']} · {c['due_date'] or 'no date'}")
                # The failure this script was written to catch: the task line
                # handed back as a copy of the evidence instead of written.
                same = c["task"].strip().strip(".").casefold() == \
                    c["quote"].strip().strip(".").casefold()
                print(f"        quote: {wrap(c['quote'], '               ')}")
                if same:
                    print("        ^^ ECHO — the task is the quote, not a task")
            if not result["kept"]:
                print("    (nothing)")
            for d in result["dropped"]:
                print(f"    × dropped ({d.get('reason')}): "
                      f"{wrap(str(d.get('task') or d.get('payload')), '      ')}")
            print()

        print(f"{len(episodes)} note(s), {cost['in']} in / {cost['out']} out tokens. "
              f"Nothing was written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
