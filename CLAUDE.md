# Ruger — working notes for Claude Code

Read `v0PRD.md` first. It is the spec and it wins any argument with this file.

## What this is

Meeting notes in, verified commitments out. One processing layer, two outputs: a
local board for **correcting** what was extracted, and a Notion database for
**living with** the list (D9). Email and Slack come later and must not require a
rewrite.

Notes reach the inbox three ways: a Granola export dropped there by hand, a note
pasted or uploaded in the board, and — the path that actually runs now — the
Wispr Flow importer on a 5-minute timer. See "Wispr Flow import" and "The timer".

The local board is not redundant. Correcting an extraction needs the evidence
sitting next to the task, which a generic task tool cannot show you — that is
D5, and it is why the board stayed even after Notion became the daily surface.

## Run it

Config lives in `.env` at the repo root (gitignored) or the process
environment; real env vars win. `PKM_ME` must be your name as it appears in your
notes, or every promise you made is filed as one made to you.

```bash
.venv/bin/python -m pkm wispr             # import Wispr Flow meetings into the inbox
.venv/bin/python -m pkm doctor            # provider, model, key, inbox, db
.venv/bin/python -m pkm                   # sync the inbox, then serve the board
.venv/bin/python -m pkm sync --table      # extract and print a table, no UI
.venv/bin/python -m pkm drops             # what the quote check threw away
.venv/bin/python -m pkm models gemma      # what the endpoint serves (openai path)
```

Board: http://127.0.0.1:8765

## Providers

`pkm/providers/` is the only place that knows which model answers. Everything
downstream treats a response as untrusted either way.

- `anthropic` (default) — native structured outputs, so the JSON shape is
  **enforced server-side**.
- `gemini` — Gemini's OpenAI-compatible endpoint. Honours `json_schema`, so it
  starts there.
- `openai` — any other `/v1/chat/completions` endpoint. Selected automatically
  when `PKM_BASE_URL` is set. Starts at `json_object`.

**Never assume a requested schema was applied.** Featherless serving
`google/gemma-4-31B-it` (measured 2026-08-04) returns HTTP 200 for a
`json_schema` request and silently ignores it — it invented its own key names.
Accepted-but-unenforced is worse than unsupported, because it looks like it
worked. So `OpenAICompatibleProvider` judges a response by **the shape that came
back**, not the status code, and walks down
`json_schema → json_object → plain prompting`, remembering what worked.

`parse_json_object` repairs *packaging* only — fences, leading prose, bare
arrays. It must never rename or invent fields: a wrong-shaped response has to
stay wrong so the validator can reject it.

## Tests

No framework — scripts that print PASS/FAIL and exit non-zero. 530 assertions.

```bash
.venv/bin/python scratch/test_ingest.py             # step 1: idempotent ingest      (22)
.venv/bin/python scratch/test_extraction.py         # step 2: quote check + dedup     (54)
.venv/bin/python scratch/test_providers.py          # JSON salvage + provider select  (30)
.venv/bin/python scratch/test_ui.py                 # note ingest, sources, CSRF      (59)
.venv/bin/python scratch/test_tasks.py              # manual tasks, merge-refresh     (59)
.venv/bin/python scratch/test_notion.py             # push/pull vs a fake Notion     (119)
.venv/bin/python scratch/test_wispr.py              # Wispr import, end to end        (84)
.venv/bin/python scratch/test_capture_handoff.py    # capture layer -> inbox          (26)
.venv/bin/python scratch/test_status.py             # counts + timer liveness         (55)
cd scratch && ../.venv/bin/python test_server.py    # step 3: the endpoints           (22)
.venv/bin/python scratch/test_live.py               # real provider call — COSTS TOKENS
```

Everything except `test_live.py` feeds **canned model output** — or, for Notion, a
stdlib HTTP server standing in for `api.notion.com` — through the real validator,
dedup and store path, so it costs nothing and needs no key. All of them use temp
dirs and in-memory or temp SQLite; none touch `~/.pkm`.

**A test double must model what the API *returns*, not what you send it.** The
fake Notion first returned properties in creation shape (`{"title": {}}`) rather
than retrieval shape (which carries `id`, `name` and `type`). `title_property`
keys off `type`, so the double was hiding the renamed-title-column case instead
of testing it — and that case is exactly what the first real database hit.

## Layout

```
schema.sql              events -> episodes -> commitments (D1). Do not simplify.
pkm/config.py           env-var config, no required values
pkm/db.py               all SQL. Board state lives here, not the browser (D6)
pkm/connectors/inbox.py markdown + frontmatter reader (D2)
pkm/connectors/notion.py push content out, pull status back (D9). Off the ingest path
pkm/episodes.py         events -> episodes grouping; 1:1 for meetings, not for Slack
pkm/extract.py          one model call per episode + the verbatim-quote check
pkm/providers/          the only code that knows which model answers
pkm/prompts/            the extraction prompt, editable without touching code
pkm/notes.py            UI ingest, source detail, manual task creation
pkm/settings.py         reads/writes the provider settings in .env
pkm/dedup.py            Jaccard over stopworded tokens (D4)
pkm/sync.py             ingest -> extract -> dedup -> rows
pkm/server.py           stdlib http.server; board, notes and settings endpoints
pkm/board.html          the whole UI. No React, no bundler, no build step
pkm/status.py           board counts + timer liveness. Three renderings, one snapshot
scripts/ruger.5m.sh     SwiftBar plugin. A wrapper, so the logic stays testable
```

## Constraints that are easy to break by accident

- **D1 — keep the full schema.** `events` → `episodes` → `commitments`, even
  though only `source='meeting'` is populated. This is the entire reason adding
  Slack later is a connector and not a migration. Do not collapse it into a
  meetings table, and do not drop `episode_events` because it is 1:1 today.
- **D2 — never make Granola's undocumented local API a build dependency.**
  Ingest is files in `~/.pkm/inbox`. Automating the pull is step 4, and only if
  step 2 proved worth it.
- **D5 — evidence stays on the card face.** The verbatim quote, speaker,
  meeting and date are what make a wrong extraction obvious in half a second.
  Do not move them into the detail panel to tidy up the card.
- **D6 — no `localStorage`.** Checking a box PATCHes the server. The page holds
  no state the server does not have.
- **§5 — never trust the model's quote.** Every commitment whose quote is not in
  the transcript is dropped and logged. This one check catches most hallucinated
  tasks; it is the highest-value code in the repo.
- **Precision beats recall.** Ten right tasks make a tool someone opens daily.
  Thirty tasks where twelve are noise make a tab they close. When tuning the
  prompt, prefer dropping a borderline task over surfacing it.
- **§8 — not in v0:** auto-closing loops, email, Slack, embeddings, the MCP
  server, notifications, digests. The MCP server is a Phase 2 wrapper over these
  same tables — do not scaffold it.
  *Three items on that list were built anyway, on the owner's explicit
  instruction: due-date editing, adding a task by hand, and the menu bar item
  (`pkm status`, which is a status readout rather than the notifications §8
  rules out). Everything else on it still stands.*
- **A pasted note becomes a file before it becomes a row.** `notes.save_note`
  writes into `~/.pkm/inbox` and then ingests through the ordinary connector, so
  there is still exactly one ingest path (D2) and the database stays derived.
  Do not add a route that inserts straight into `events`.
- **Writes require the `X-Ruger: 1` header.** The process holds API keys and any
  page in the browser can reach localhost; a custom header forces a CORS
  preflight this server never approves. Do not drop the check, and do not add
  permissive CORS. `PKM_DB` / `PKM_INBOX` are deliberately not writable over
  HTTP — a browser must not be able to repoint the database.
- **Refresh must never undo human work.** `sync.reextract_episode` MERGES; it
  does not call `apply_extraction`, which clears the episode first. Manual rows
  (`origin='manual'`) are untouched, rows a human edited (`edited=1`) keep their
  wording and only get fresh evidence, and rows the model stops returning are
  kept and reported rather than deleted.
- **Within one meeting, a commitment's identity is its QUOTE, not its task
  text.** The quote is a verbatim transcript line, so it survives rewording.
  Matching on task text meant renaming a task pushed it below the dedup
  threshold and refresh created a duplicate.
- **`update_commitment` only writes fields the caller sent.** An earlier version
  used `updates.get("due_date")` in its normaliser and silently cleared the due
  date on every unrelated patch.
- **New columns need an explicit ALTER** in `db._ADDED_COLUMNS`.
  `CREATE TABLE IF NOT EXISTS` does nothing to a table that already exists, so
  without it a database with real data breaks on the next query.
- **`[hidden]` needs `!important`** here: any author `display:` rule beats the UA
  stylesheet, so a `.field` we toggle with `hidden` stays visible without it.
- **`config.reload()` recomputes every setting** after `.env` changes. Anything
  that reads config at call time picks it up; anything that captured a value at
  import time will not. Tests that touch the settings API must set `PKM_DB` and
  `PKM_INBOX` as *real* env vars, or the reload will repoint them at `~/.pkm`.

## Design

`pkm/board.html` follows **Notion's dark UI**, deliberately and specifically.
Everything is driven by custom properties at the top of the file — change those,
not the component rules. The values are Notion's own, and several are
load-bearing for the look:

- `#191919` content ground, `#202020` sidebar, `#252525` cards.
- Text is `rgba(255,255,255,.81)` — **never pure white**. This is most of why
  Notion's dark mode reads soft rather than harsh. Do not "fix" it to `#fff`.
- Radii are 3–4px. Not `rounded-lg`. Hover states are translucent white
  overlays, not solid fills.
- Type is Notion's own system stack. Do not add a webfont: the personality comes
  from size/weight/colour, and a display face would stop it reading as Notion.
- D3's mine/theirs survives as Notion chip colours — purple `#9a6dd7` for mine,
  green `#4dab9a` for theirs. Keep them distinct; that distinction is the point.
- Light mode is opt-in via the sidebar toggle, not `prefers-color-scheme`,
  because the brief was dark. **Every colour token needs a light counterpart** —
  a missing one silently inherits the dark value and becomes unreadable.

### Elevation — the one that bit us

**On a dark ground, depth comes from a lighter edge, not a darker shadow.**
Notion's own card shadow is `rgba(15,15,15,.1) 0 0 0 1px` — dark-on-white, which
is correct in *their* light mode. Copied into a `#191919` dark theme it is
invisible, so cards rendered as flat rectangles with no perceptible border and
the whole UI read as unfinished placeholder boxes. That single mistake was the
root of it.

So every surface gets a visible `1px solid var(--edge)`, and only the shadow
varies with height:

| Token | Use |
|---|---|
| `--edge` / `--edge-hover` | every card, input, button outline, panel, table |
| `--shadow-sm` | resting cards, inputs, the primary button |
| `--shadow-md` | hover |
| `--shadow-lg` | side peek, toast, dragging card |
| `--r-sm` 4px / `--r-md` 6px / `--r-lg` 10px | chips / controls+cards / panels |

Both sets are redefined for light mode. **Style through the tokens** — no raw
hex and no inline `style=` in the file; both are audited at zero.

Also set: `color-scheme` per theme, so native controls (the date picker, its
popup, scrollbars, autofill) render dark instead of light-on-dark. Easy to miss
and an instant tell.

### Spacing and layout

There is one spacing scale (`--s1` 4px … `--s7` 48px) plus `--nav-pad`,
`--gutter` and `--measure`. **Use it. Do not introduce one-off pixel values** —
that is exactly what made the first cut look unfinished.

Alignments that are load-bearing, and how to check them:

| Invariant | Value |
|---|---|
| Sidebar tile, nav icons, footer icons share a left edge | all at `left: 16` |
| Sidebar labels share a left edge | all at `left: 44` |
| Column status chip is flush with its cards | both at `left: 336` (240 sidebar + 96 gutter) |
| Card properties hang under the title, not the checkbox | `margin-left: 24px` = checkbox 16 + gap 8 |
| Every input is the same height | `37px` |

Prefer flex/grid `gap` over per-element margins, and keep `align-content: start`
on `.field` / `align-items: start` on `.two`: without them a field with no hint
line stretches its input to match a neighbour that has one.

Verify by measuring, not squinting. Headless Chrome plus `--dump-dom` and a
`getBoundingClientRect` script gives exact numbers; screenshots hide 2px errors.

### Copy

**No em dashes in UI copy.** Rewrite the sentence instead of swapping in a
hyphen. Hints belong on their own `.desc` line or in a placeholder, never glued
to a label with a dash. Use curly apostrophes (`’`) in prose. Note that some
user-visible strings live in `config.py` (`PROVIDER_PRESETS` notes and key
hints), not in `board.html`.

## Where the product is won or lost

`pkm/prompts/extract_commitments.md`. Iterate there, run
`python -m pkm sync --table`, eyeball the output against the transcripts, repeat.
Code changes are rarely the answer to a bad board.

## Environment

- Python 3.14, stdlib only except `anthropic` and `openai` (in `.venv`). No Node.
- `uv venv .venv && uv pip install --python .venv/bin/python anthropic openai`
- Model: `claude-haiku-4-5` per §5 on the Anthropic path. Override with `PKM_MODEL`.

## Wispr Flow import

`pkm/connectors/wispr.py`, run with `python -m pkm wispr`. Wispr Flow records the
call, transcribes both sides and writes a summary once you press the button, all
locally. This reads that and **writes markdown into `~/.pkm/inbox`**, then stops:
ingest is still the ordinary inbox connector, so there is one path into `events`
(D2) and the database stays derived from files. Nothing here touches `events` or
`commitments`.

    ~/Library/Application Support/Wispr Flow/
        flow.sqlite                     Meetings.title/summary/notes/speakerMap
        meetings/<uuid>/refined.ndjson  cleaned, speaker-numbered transcript
        meetings/<uuid>/live.ndjson     raw stream, carries mic vs system
        meetings/<uuid>/upload.ogg      audio

Override the location with `PKM_WISPR_HOME`. The database is opened **read-only**
and never written; a URI connection is enough while Wispr is running, and the
copy-to-temp fallback exists for a `-wal` that cannot be replayed read-only.

- **`refined.ndjson` wins over `live.ndjson`.** The refine pass rewrites the
  stream into whole sentences and fixes names — "Hey guys, Cavey" became
  "Hey, Kavi", and "Nina" became "Nila". Both matter: the quote check needs
  contiguous text to match, and `owner` needs a name spelled the way a human
  spells it.
- **Segments join with NO separator.** Wispr splits words across segments
  (`"Send"` + `"line"`) and puts a leading space only at real word boundaries.
  Joining with `" "` writes "Send line" into the body, the model quotes
  "Sendline", the check fails, and a real commitment disappears silently.
- **Attribution comes from `speakerMap`, not the transcript.** `refined.ndjson`
  numbers turns 1/2 and drops the mic flag, so identity comes from
  `assignments`, where the entry carrying `mic` is you by definition. `live`'s
  own `speaker.id` is `1` for every segment and is useless; its `source` field is
  the attribution.
- **Your turns resolve to `Me`, theirs to their real name.** `Me` because
  `dedup.normalise_owner` folds `me`/`i`/`myself`/`self` unconditionally,
  independent of `PKM_ME`, so your promises are yours even if that setting is
  wrong. Their real name because the prompt refuses to invent an owner and a
  generic "Them" is a weak one that makes a poor Notion card.
- **`<@speaker:N>` and `Speaker N` must be resolved.** They appear in the
  summary, and unresolved they leave the model with nobody to name as owner. The
  possessive form is handled separately so `<@speaker:2>'s last day` reads "My
  last day" rather than "Me's last day".
- **"My thoughts" is the `notes` column,** which Wispr also uses to hold the
  generated summary inside a `:::toggle` block. Those blocks are stripped and
  what remains is what the human typed. Keeping them would put the summary in
  the file twice and give the model a second, non-verbatim shot at the same
  commitment.
- **Only `finalized = 1` meetings export.** An in-progress call has no summary
  and a transcript still being appended to.
- **A retitle deletes the old file.** The filename is `<date>-<slug>`, so a new
  title means a new name, and two files carrying one `id:` is the duplicate the
  inbox connector refuses to guess about. The stable `id: wispr-<uuid>` is what
  makes re-import update the episode instead of adding a second.

Per-meeting folders look like they are cleaned up after upload, so this runs on a
timer rather than when you remember. Once a note is in the inbox it is safe: the
inbox is the durable artifact, and a later run whose transcript has vanished
reports the meeting as skipped rather than truncating the note.

## The timer

`scripts/wispr-tick.sh`, driven by `~/Library/LaunchAgents/ai.ruger.wispr.plist`
every 300s with `RunAtLoad`. Log: `~/.pkm/logs/wispr.log`.

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.ruger.wispr.plist
launchctl bootout   gui/$(id -u)/ai.ruger.wispr        # stop it
launchctl kickstart -p gui/$(id -u)/ai.ruger.wispr     # run one tick now
launchctl print     gui/$(id -u)/ai.ruger.wispr | grep -E "runs|last exit"
```

- **The repo must not live under `~/Desktop`.** macOS TCC denies a launchd agent
  every path under Desktop, Documents and Downloads, and it fails with
  `Operation not permitted` on the script itself, before any of this code runs.
  That is why the repo sits at `~/ruger` and not where it started. `~/.pkm` and
  `~/Library/Application Support/Wispr Flow` are both outside the protected set,
  so no Full Disk Access grant is needed anywhere. Verified: the agent reads
  Wispr's database with no permission prompt.
- **The tick skips Notion when nothing changed.** `push` is O(board), not O(new
  work) — it re-sends every row so local edits reach Notion — so an idle wake
  every 5 minutes would otherwise be pure API traffic. The script reads the
  import and sync output and exits early.
- **No `set -e`.** Each stage is independent: a provider outage must not stop the
  push of what is already extracted, and a Notion outage must not undo an
  extraction.
- **`pull` never runs with `--prune`.** An automated sync must not archive
  anything.
- **`sync` routes an already-extracted episode to the MERGE path.** This is what
  makes the timer safe, and it is why `run_sync` checks `extracted_at`: Wispr
  rewrites a transcript when you press summarize, and rebuilding those rows would
  drop the Notion page id off every one and duplicate every card on the next push.
- launchd will not start a second copy of the label while one is running, so a
  slow tick cannot overlap the next. A tick missed while the machine slept runs
  once on wake, not as a backlog.
- **The heartbeat is the tick log's mtime, and nothing writes one on purpose.**
  The tick prints a dated header on every wake, including the idle ones that
  skip Notion, so `pkm status` reads liveness off that file's mtime. A heartbeat
  a process has to remember to update is one that keeps reporting "alive" after
  the interesting part has died. `STALE_AFTER` is 900s — three missed wakes,
  which is where a slow machine stops being the explanation. A slept laptop is
  the common false positive and it clears itself on the next wake.
- **`pkm status --swiftbar` owns the menu's formatting, not the plugin.**
  `scripts/ruger.5m.sh` resolves the repo through its own symlink and shells
  into this. That keeps the plugin free of a `jq` dependency, and it is why the
  menu can be asserted in `test_status.py` instead of verified by squinting at
  the menu bar. Every menu line must stay one line — a stray newline makes
  SwiftBar render the rest as separate items.

## The Notion board

Ruger's job ends at a commitment that survived the quote check. Where you *work*
the list is a separate question, and `pkm/connectors/notion.py` answers it by
pushing to a Notion database. The local SQLite database stays the source of
truth for content; the Notion database is derived from it and rebuildable.

```bash
.venv/bin/python -m pkm notion                    # who the token is, what it can see
.venv/bin/python -m pkm notion setup --parent URL  # create the database
.venv/bin/python -m pkm notion setup --database URL  # or adopt one you have
.venv/bin/python -m pkm push --dry-run            # what would go out
.venv/bin/python -m pkm push                      # send it
.venv/bin/python -m pkm pull                      # bring status changes back
.venv/bin/python -m pkm unlink                    # forget page ids, rebuild next push
```

Setup is two steps on Notion's side that no API call can do for you: create an
internal integration at notion.so/my-integrations for the token, then open the
parent page and use ••• → Connections to give that integration access. Without
the second step every call returns 404, which is why `_request` spells that out
in the 404 message rather than passing Notion's wording through.

### Each direction owns different fields

| | push | pull |
|---|---|---|
| task, owner, direction, due, evidence, meeting | **writes** | ignores |
| status | writes **once**, at page creation | **reads back** |

- **`push` must never re-send Status.** It is set when the page is created and
  never again. A push that included Status would drag a card back out of Done
  the moment you moved it — the same class of bug `sync.reextract_episode`
  exists to prevent. `content_properties` deliberately has no Status key, and
  the test asserts that no PATCH body ever contains one.
- **The consequence, and it is deliberate:** changing a status on the local
  board does not reach Notion. Once a commitment is pushed, Notion owns which
  column it sits in. If that ever needs an override it wants to be an explicit
  `push --force-status`, not a quiet change to the normal path.
- **`pull` reads status generously.** Renaming a Notion column is a normal thing
  to do, so `STATUS_ALIASES` accepts "Not started", "WIP", "Completed" and
  friends. Anything unrecognised is left alone and counted in `unreadable` —
  never guessed at, never reset to todo.
- **Nothing is deleted on either side by default.** A page whose commitment is
  gone here is *reported* as orphaned and only archived with `--prune`. A row
  whose page vanished from Notion is reported in `missing` and kept. Same rule
  as refresh: an automated sync must not lose a real commitment.
- **A page added in Notion by hand is ignored, not adopted.** It has no
  `Ruger ID`, and the board is derived from meetings — inventing an episode for
  it would break D2.

### Things that bite

- **`external_id` on `commitments` is what makes push idempotent.** Without the
  stored page id every push would create a duplicate page for every row. It
  needed an entry in `db._ADDED_COLUMNS`, like every other added column.
- **`config.notion_id` drops the query string before searching.** A database URL
  carries the database id in the path and the *view* id in `?v=`. Taking the
  view id gives a permanent 404 that looks like a permissions problem. Both hex
  patterns are also anchored, so a 33-character hex run is rejected rather than
  silently truncated to 32.
- **`VERSION` is part of the contract, not a stray header.** Later Notion API
  versions split a database into "data sources" and change the shape of a page's
  `parent`. Bumping it means revisiting create/query together.
- **Never assume the target database has Ruger's shape.** `board_profile` reads
  it once before any write, and every page is fitted to what came back. The live
  database this was first pointed at (a stock Notion "Tasks" template) differed
  in both ways that matter:
  - its title column is called **`Name`**, not `Task`, so a hardcoded title key
    creates seven untitled cards;
  - its Status is Notion's real **`status`** type offering *Not started / In
    progress / Done*. That takes `{"status": {...}}`, not `{"select": {...}}`,
    **and its options cannot be created over the API** — so writing Ruger's own
    `"To do"` is a 400 on every single page.

  `status_payload` therefore maps a local status onto an option that already
  exists, reading the column's vocabulary back through `STATUS_ALIASES` so
  "Not started" is understood as todo with no configuration. If nothing matches
  it returns None and push omits Status: a card in the default column is
  recoverable, a card that 400'd and never arrived is not.
- **Ruger's own `create_database` uses a `select` for Status**, because the API
  cannot create a `status` property at all. It groups into a board identically.
  `read_status` reads both types, which is what makes adoption work.
- **Notion allows about three requests a second.** `GAP` paces the loop so the
  429 path stays theoretical; 429 and 5xx are retried with backoff, and
  everything else is raised immediately rather than hammered.
- **The Notion token is a separate secret from `PKM_API_KEY`.** One reads
  meetings, the other writes your task list. Both live in the gitignored `.env`,
  both are in `SECRET_KEYS`, and neither is returned by `GET /api/config`.
