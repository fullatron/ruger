"""Live end-to-end run against the configured provider. Costs real tokens.

    .venv/bin/python scratch/test_live.py

Uses a temp inbox and temp database — never your real board.
"""
from __future__ import annotations
import sys, tempfile
from contextlib import closing
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pkm import config, db, sync
from pkm.providers import get_provider
from test_extraction import WEEK_1, WEEK_2

def main() -> int:
    p = get_provider()
    print(f"provider: {p.describe()}\n")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp); inbox = root / "inbox"; inbox.mkdir()
        (inbox / "week1.md").write_text(WEEK_1, encoding="utf-8")
        (inbox / "week2.md").write_text(WEEK_2, encoding="utf-8")
        config.DB_PATH = root / "live.db"

        with closing(db.connect()) as conn:
            stats = sync.run_sync(conn, inbox_path=inbox, verbose=True)
            print()
            for k in ("files", "extracted", "created", "merged", "dropped", "tasks"):
                print(f"  {k:10} {stats[k]}")
            for e in stats["errors"]:
                print(f"  ! {e}")
            if stats["usage"]:
                tin = sum(u["input_tokens"] for u in stats["usage"])
                tout = sum(u["output_tokens"] for u in stats["usage"])
                print(f"  tokens     {tin} in / {tout} out  (mode: {stats['usage'][0]['structured']})")
            print()
            from pkm.__main__ import print_table, print_drops
            print_table(conn); print(); print_drops(conn)
        return 1 if stats["errors"] else 0

if __name__ == "__main__":
    sys.exit(main())
