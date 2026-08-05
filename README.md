# Ruger

Meeting notes in, verified commitments out. Ruger reads what was said in a call,
pulls out the things people committed to doing — yours and other people's —
throws away anything it cannot back up with a verbatim line from the notes, and
puts what survives in two places: a local board for **correcting** the
extraction, and a Notion database for **living with** the list.

Spec: [`v0PRD.md`](v0PRD.md). Working notes: [`CLAUDE.md`](CLAUDE.md).

```
Wispr Flow recordings ─┐
                       ├──▶  ~/.pkm/inbox/*.md          files are the durable artifact
paste / drop in the UI ┘            │  inbox connector
                                    ▼
                                 events                 raw items, verbatim
                                    │  grouping         (later: one email, one Slack message)
                                    ▼
                                 episodes                the unit extraction runs over
                                    │  one model call each
                                    ▼
                              commitments                + verbatim-quote check, + Jaccard dedup
                                    │
                        ┌───────────┴───────────┐
                        ▼                       ▼
                 board.html                 Notion database
              (correct it here)         (work the list here)
```

The database is derived. Delete `~/.pkm/ruger.db`, run the sync again, and the
board comes back from the files in the inbox.

## Setup

```bash
uv venv .venv
uv pip install --python .venv/bin/python anthropic openai
```

Python 3.14, stdlib only except those two SDKs. No Node, no bundler, no build
step. Config comes from the process environment, falling back to a gitignored
`.env` at the repo root; real env vars always win.

The easiest route for the model settings is the **Settings** tab — pick a
provider, paste a key, press **Test connection**. It writes the same `.env` shown
below, with owner-only permissions.

| Provider | What you need |
|---|---|
| **Anthropic** (default) | `ANTHROPIC_API_KEY`, or nothing at all if you've run `ant auth login`. Model defaults to `claude-haiku-4-5`. |
| **Google Gemini** | A key from [aistudio.google.com/apikey](https://aistudio.google.com/apikey). Uses Gemini's OpenAI-compatible endpoint; model defaults to `gemini-2.5-flash`. |
| **OpenAI-compatible** | Any `/v1/chat/completions` endpoint — OpenAI, Featherless, Together, Groq, OpenRouter, vLLM, Ollama, LM Studio. Needs a base URL and a model id. |

```ini
# .env — gitignored, chmod 600
PKM_PROVIDER=gemini
PKM_API_KEY=AIza…
PKM_MODEL=gemini-2.5-flash
PKM_ME=Alex
PKM_CONTEXT_LENGTH=32768        # optional: refuse over-long transcripts up front
PKM_NOTION_TOKEN=ntn_…          # separate secret; not editable from the UI
PKM_NOTION_DB=…                 # written by `pkm notion setup`
```

`python -m pkm doctor` shows what resolved — inbox, database, provider, model,
masked key, your name, dedup threshold, Notion token and how many commitments
have been pushed. `python -m pkm models gemma` (or **Browse models** in
Settings) lists what your endpoint actually serves, which is useful when a
catalogue has 20,000+ entries and a typo just 404s.

**`PKM_ME` matters more than it looks.** It is a comma-separated list of your own
names and aliases, and it is how "who promised this" is decided for any note
without speaker labels. Get it wrong and every promise *you* made is filed as one
made *to* you.

## Run it

```bash
.venv/bin/python -m pkm wispr        # import Wispr Flow meetings into the inbox
.venv/bin/python -m pkm              # sync the inbox, then serve the board
open http://127.0.0.1:8765
```

| | |
|---|---|
| `python -m pkm` | sync the inbox, then serve the board |
| `python -m pkm sync` | ingest + extract only |
| `python -m pkm sync --table` | …and print the table. No UI, for prompt work |
| `python -m pkm sync --push` | …and send the result to Notion |
| `python -m pkm serve [--host --port]` | serve without syncing |
| `python -m pkm table` | print the current board |
| `python -m pkm status [--json]` | board counts, and whether the timer is still alive |
| `python -m pkm drops` | what the verbatim-quote check rejected, and why |
| `python -m pkm revalidate [--apply]` | re-check stored drops against the current checker. No model calls |
| `python -m pkm wispr [--dry-run --limit N]` | Wispr Flow → inbox |
| `python -m pkm doctor` | inbox, database, credentials, your name |
| `python -m pkm models [substring]` | what an OpenAI-compatible endpoint serves |
| `python -m pkm notion` | who the Notion token is, and what it can see |
| `python -m pkm notion setup --parent URL` | create the Notion database |
| `python -m pkm notion setup --database URL` | or adopt one you already have |
| `python -m pkm push [--dry-run --limit N]` | send commitments to Notion |
| `python -m pkm pull [--dry-run --prune]` | bring status changes back |
| `python -m pkm unlink` | forget every Notion page id; the next push rebuilds |

## How notes get in

Two ways, today: the Wispr Flow importer on a timer, and pasting or dropping a
note in the board. Both land in `~/.pkm/inbox` as markdown before anything
becomes a row, so there is exactly one ingest path — which is why adding a source
later is a connector and not a migration, and why nothing in the pipeline inserts
straight into `events`.

**Wispr Flow, on a timer.** This is the path that runs unattended.
`pkm/connectors/wispr.py` reads Wispr Flow's local store — `flow.sqlite` for the
title, summary, your typed notes and the speaker map, plus
`meetings/<uuid>/refined.ndjson` for the transcript — and writes one markdown
file per finished meeting. It opens that database **read-only** and never writes
to it. Only `finalized = 1` meetings export, and each waits 45 seconds after the
recording stops so the last transcript flush has landed.

Two details in there are load-bearing. `refined.ndjson` is preferred over
`live.ndjson` because the refine pass rewrites the stream into whole sentences
and fixes names, and both matter: the quote check needs contiguous text to match,
and an owner needs a name spelled the way a human spells it. And segments are
joined with **no separator**, because Wispr splits words mid-token (`"Send"` +
`"line"`) and puts a leading space only at real word boundaries.

Attribution comes from `speakerMap`, not from the transcript: the assignment
carrying the microphone flag is you by definition. Your turns are written as
`Me`, theirs under their real name.

**Paste or drop in the board.** The **Add notes** tab takes a pasted meeting or
up to 50 `.md` files, and you can drop files into `~/.pkm/inbox` yourself and
press **Sync inbox**. Either way the note is written to the inbox *first*, then
read back through the ordinary connector, so both routes are the same pipeline.

**There is no Granola integration.** Granola is a source of *text* you paste like
any other; Ruger has no connector for it and never touches its undocumented local
API, which was always meant to be step 4 and has not been built. Two
accommodations for its formatting do exist, because they were paid for in real
dropped commitments: the inbox connector reads the bare `Sat, 01 Aug 26` date line
its exports put in the body, and the quote check's `markup` tier survives the bold
"Next Steps" block it renders. Both are deliberately source-agnostic.

Anything that writes a markdown file into the inbox is a valid producer — that is
the whole interface, and no program has to call another.
`scratch/test_capture_handoff.py` pins Ruger's half of that contract against an
external capture layer's output, though nothing but the two paths above feeds the
inbox today.

### Inbox format

Frontmatter is preferred but optional — a file with none at all works, because
the title falls back to the `# heading` and then the filename, and the date falls
back to a bare date line in the body (`Sat, 01 Aug 26`) and then to the file's
mtime. That is what makes a paste out of someone else's notes app usable as-is.

```markdown
---
title: Weekly with Maya
date: 2026-08-03
participants: [Alex, Maya]
id: weekly-maya-892f9134   # optional; defaults to the path. Two files with one id is an error
---

Alex: I'll audit the team LinkedIn profiles by Friday.
Maya: I'll send you the Beacon login today.
```

## The timer

`scripts/wispr-tick.sh`, driven by `~/Library/LaunchAgents/ai.ruger.wispr.plist`
every 300s with `RunAtLoad`. One tick is import → sync → push → pull. Log:
`~/.pkm/logs/wispr.log`.

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.ruger.wispr.plist
launchctl bootout   gui/$(id -u)/ai.ruger.wispr        # stop it
launchctl kickstart -p gui/$(id -u)/ai.ruger.wispr     # run one tick now
launchctl print     gui/$(id -u)/ai.ruger.wispr | grep -E "runs|last exit"
```

- The script has **no `set -e`**: each stage is independent, so a provider outage
  must not stop the push of what is already extracted, and a Notion outage must
  not undo an extraction.
- **Notion is skipped when nothing changed.** `push` is O(board) rather than
  O(new work) — it re-sends every row so local edits reach Notion — so an idle
  wake would otherwise be pure API traffic. An idle tick is two SQLite reads.
- **`pull` never runs with `--prune`.** An automated sync must not archive
  anything.
- **The repo cannot live under `~/Desktop`, `~/Documents` or `~/Downloads`.**
  macOS TCC denies a launchd agent every path under those, and it fails on the
  script itself before any of this code runs. `~/.pkm` and Wispr's own directory
  are outside the protected set, so no Full Disk Access grant is needed.
- launchd will not start a second copy of the label while one is running, so a
  slow tick cannot overlap the next.

### Seeing it run

A timer whose only evidence is a log file is a timer you stop trusting.
`python -m pkm status` answers both questions at once:

```
board      11 commitments (7 to do, 0 doing, 4 done)
           6 mine, 5 theirs
notes      4, last extracted 2026-08-05T12:12:21+00:00
notion     11 of 11 pushed
timer      last tick 3 min ago
```

**The heartbeat is the mtime of the tick log**, not a file anything writes on
purpose. The tick prints a dated header on every wake, including the idle ones
that skip Notion, so a log that stopped moving means the agent stopped running. A
heartbeat a process has to remember to update is one that keeps reporting "alive"
after the interesting part has died.

For a menu bar item, `scripts/ruger.5m.sh` is a [SwiftBar](https://swiftbar.app)
plugin. A filled green `◉ 11` means it ticked recently; a hollow red `◌ 11` means
the timer looks stopped, and the menu then shows the `launchctl kickstart` line
that restarts it. The dropdown carries the counts, plus Open board, Run a tick
now, and Open log.

```bash
brew install --cask swiftbar
mkdir -p ~/.swiftbar-plugins
ln -s "$PWD/scripts/ruger.5m.sh" ~/.swiftbar-plugins/ruger.5m.sh
defaults write com.ameba.SwiftBar PluginDirectory -string "$HOME/.swiftbar-plugins"
open -a SwiftBar
```

The `5m` in the filename is SwiftBar's refresh interval and matches the timer's
own 300s, so the menu is never more than one tick out of date. All of the
formatting is `pkm status --swiftbar`, which keeps the plugin a wrapper with no
`jq` dependency and makes the menu assertable in a test rather than something you
verify by squinting at the menu bar.

## The board

`http://127.0.0.1:8765`. One page, `pkm/board.html`, served by the stdlib
`http.server`.

| | |
|---|---|
| **Board** | the commitment board |
| **Add notes** | paste a meeting, or drop `.md` files — stores and extracts in one step |
| **Sources** | every note stored; open one for its transcript, its tasks and its drops |
| **Settings** | provider, model, API key, your name, context limit |
| **Sync inbox** | ingest and extract whatever is new in `~/.pkm/inbox` |
| **Appearance** | dark (the default) or light |

Columns are status: **To do / Doing / Done**. Filter to All / Mine / Theirs, and
group into swimlanes by meeting or by person — the same renderer draws all three
views.

- **Purple** cards are yours, **green** are someone else's promise to you. Two
  different kinds of anxiety; merging them makes the board useless.
- Every card carries its evidence on the face: the verbatim line that produced
  it, who said it, which meeting. A wrong extraction is obvious in half a second
  instead of quietly rotting on the board.
- Due dates go red when overdue.
- A commitment raised in more than one meeting is **one card** reading `2× raised`,
  not two rows. Open it for the full history of when it came up.
- Drag a card, tick its checkbox, or focus it and press `1`/`2`/`3`.
- Click any card to open a panel where the task, owner, direction, due date and
  status are all editable, and can be deleted. Every control saves immediately.
- A task you added by hand says it has no quote rather than showing empty
  evidence, because nobody said it.

Board state lives in SQLite, not the browser. There is no `localStorage`: every
change PATCHes the server, and the page holds nothing the server does not have.

### One source

Open a row in **Sources** to get that meeting on its own page: the transcript
exactly as stored, every task read out of it, and everything the quote check
dropped with the reason. Three actions live there.

| | |
|---|---|
| **Refresh tasks** | run the model over the note again |
| **Add a task** | write one by hand, attached to this meeting |
| **Remove note** | forget it (the file moves to `~/.pkm/trash`, so it is recoverable and will not re-ingest) |

**Refresh merges, it does not replace.** Tasks you edited keep their wording and
only get fresh evidence; tasks you added by hand are untouched; statuses are
never changed; anything new is added; and a task the model stops returning is
kept and reported rather than deleted, because a worse second reading must not
lose a real commitment. The same merge path runs when a transcript changes
underneath you — which Wispr does when you press summarize — so the unattended
timer cannot undo work either.

Within one meeting a task's identity is its **quote**, not its wording, so
renaming a task never makes refresh create a duplicate of it.

### Endpoints

| | |
|---|---|
| `GET /api/tasks` | all commitments with meeting + evidence joined |
| `PATCH /api/tasks/{id}` | status (drag or checkbox) **and** content edits |
| `POST /api/tasks` · `DELETE /api/tasks/{id}` | add a task by hand, remove one |
| `POST /api/sync` | inbox ingest → extraction |
| `GET /api/notes` · `GET /api/notes/{id}` | sources, and one source with its transcript, tasks and drops |
| `POST /api/notes` · `POST /api/uploads` | paste or upload a note (writes to the inbox first) |
| `POST /api/notes/{id}/reextract` | re-run extraction for one meeting and **merge** |
| `DELETE /api/notes/{id}` | remove rows; the file moves to `~/.pkm/trash` |
| `GET`/`PUT /api/config` · `POST /api/config/test` · `POST /api/config/reveal` · `GET /api/config/models` | credentials, visible and editable |
| `GET /api/version` | so a stale server process announces itself |

**Writes require an `X-Ruger: 1` header.** The server binds 127.0.0.1, but
localhost is not a trust boundary: any page in your browser can POST to it, and
this process holds API keys. A custom header forces a CORS preflight this server
approves none of, so those requests never arrive. There is an Origin check too,
and `PKM_DB` / `PKM_INBOX` are deliberately not writable over HTTP — a browser
must not be able to repoint the database.

`GET /api/version` exists because `board.html` is read from disk per request, so
a long-running server happily serves the *new* page while routing on *old* code.
The page compares versions and shows a red banner telling you to restart.

## Extraction

One model call per episode, with the prompt in
`pkm/prompts/extract_commitments.md` — editable without touching code, and the
place the product is won or lost. Iterate there, run `pkm sync --table`, eyeball
the output against the transcripts, repeat. Code changes are rarely the answer to
a bad board.

The model returns JSON only:

```json
{ "commitments": [ {
    "task": "imperative phrasing, one line, no hedging",
    "direction": "mine" | "theirs",
    "owner": "name as spoken, or 'me'",
    "due_date": "YYYY-MM-DD or null",
    "quote": "verbatim from the transcript",
    "speaker": "who said it"
} ] }
```

Only things someone committed to **doing** count — not topics discussed, not
decisions made, not ideas raised. If nothing was promised the answer is `[]`: an
empty board is correct output, not a failure. Relative dates ("by Friday") are
resolved against the meeting date, which is passed in.

**Precision beats recall.** Ten right tasks make a tool someone opens daily.
Thirty tasks where twelve are noise make a tab they close. When tuning the
prompt, prefer dropping a borderline task over surfacing it.

### The quote check

Every commitment must carry the line that produced it, copied character for
character, and code verifies that line appears in the transcript. Anything that
fails is dropped and logged. A model that invented a commitment usually cannot
produce the line that proves it, so this one check catches most hallucinations —
it is the highest-value code in the repo.

The check is tiered and source-agnostic. A match is recorded as `exact`,
`whitespace` or `markup`; anything else is dropped as `not_found` or `too_short`.
The `markup` tier strips markdown, Slack and HTML formatting before comparing,
because a model reads the *rendered* text: Granola renders its "Next Steps"
section in bold, and a line stored as `- **Raise 50% of outstanding invoice**`
once caused every commitment in that block to be silently rejected. No tier ever
adds, removes or reorders a word, and a match must still be one contiguous span,
so a reworded or invented sentence still fails.

Dropped commitments are kept in `extraction_drops` with the model's original
JSON. `pkm drops` shows them; `pkm revalidate` re-runs the *current* checker over
what is already on disk and recovers the ones that were wrongly rejected, with no
model call. That paid for itself the first time the `markup` tier landed.

### Deduplication

A commitment that matches an existing open one — same owner, normalised-text
Jaccard ≥ 0.6 over stopworded tokens — does not create a second row. It appends a
mention and bumps the count, so a task promised three weeks running is the most
interesting thing on the board and the card says so. Owner matching folds your
`PKM_ME` aliases and the first person to a single key.

### Structured output is requested, never trusted

On the Anthropic path the JSON schema is **enforced server-side**, so the shape
is guaranteed. Elsewhere it is a hope. Measured against Featherless serving
`google/gemma-4-31B-it`:

| request | result |
|---|---|
| `response_format: json_schema` | HTTP 200, **schema silently ignored** — invented its own keys |
| `response_format: json_object` | clean, correctly-shaped JSON |
| no `response_format` | correct JSON, wrapped in ``` fences |

Accepted-but-unenforced is the worst case, because it looks like it worked. So
the provider judges a response by **the shape that came back**, not the status
code, and walks down `json_schema → json_object → plain prompting` until
something usable arrives, remembering what worked. Gemini starts at
`json_schema`; an arbitrary endpoint starts at `json_object`.

Salvage repairs *packaging* only — fences, leading prose, a bare array. It never
renames or invents a field, so a wrong-shaped response stays wrong and the
validator rejects it. Expect a higher drop rate from smaller models and read the
dropped list after each ingest: a small model dropping more is the system
working, not failing.

An episode whose extraction failed is left unextracted, so the next sync retries
it. Losing a meeting to one overloaded endpoint is not acceptable.

## The Notion board

Ruger's job ends at a commitment that survived the quote check. Where you *work*
the list is a separate question, and `pkm/connectors/notion.py` answers it by
pushing to a Notion database. The local SQLite database stays the source of truth
for content; the Notion database is derived from it and rebuildable.

```bash
python -m pkm notion                          # who the token is, what it can see
python -m pkm notion setup --parent URL       # create the database
python -m pkm notion setup --database URL     # or adopt one you already have
python -m pkm push --dry-run                  # what would go out
python -m pkm push                            # send it
python -m pkm pull                            # bring status changes back
```

Two setup steps happen on Notion's side and no API call can do them for you:
create an internal integration at notion.so/my-integrations for the token, then
open the parent page and use ••• → Connections to give that integration access.
Skip the second and every call returns 404. The Notion token is a separate secret
from `PKM_API_KEY` — one reads meetings, the other writes your task list — and it
has no Settings-page field; it lives in `.env`, where `pkm notion setup` also
writes the database id.

### Each direction owns different fields

| | push | pull |
|---|---|---|
| task, owner, direction, due, evidence, meeting, raised count | **writes** | ignores |
| status | writes **once**, at page creation | **reads back** |

- **Push never re-sends Status.** It is set when the page is created and never
  again, so a card you dragged to Done stays in Done. The deliberate cost: once a
  commitment is pushed, changing its status on the *local* board does not reach
  Notion. Notion owns which column a card sits in.
- **Pull reads status generously.** Renaming a Notion column is a normal thing to
  do, so "Not started", "WIP" and "Completed" are all understood. Anything
  unrecognised is left alone and counted, never guessed at.
- **Nothing is deleted on either side by default.** A page whose commitment is
  gone here is reported as orphaned and only archived with `--prune`. A row whose
  page vanished from Notion is reported and kept.
- **A page added in Notion by hand is ignored, not adopted.** It carries no
  `Ruger ID`, and the board is derived from meetings.
- Push is idempotent: the Notion page id is stored on the row, so a second push
  with no local changes creates nothing. `pkm unlink` forgets those ids when the
  Notion database has been deleted or replaced.
- **The target database is never assumed to have Ruger's shape.** It is read once
  before any write and every page is fitted to what came back. The first live
  database this was pointed at — a stock Notion "Tasks" template — called its
  title column `Name`, and used Notion's real `status` type whose options
  *cannot be created over the API*. So local status is mapped onto an option that
  already exists, and omitted when nothing matches: a card in the default column
  is recoverable, a card that 400'd and never arrived is not.

## Storage

`events` → `episodes` → `commitments`, plus `episode_events`,
`commitment_mentions` and `extraction_drops`. See [`schema.sql`](schema.sql).

Only `source='meeting'` is populated, and the schema still carries `email` and
`slack` from day one. That is the entire reason adding Slack later is a connector
and a grouping rule rather than a migration: an event is one raw item, an episode
is the unit extraction runs over, and for meetings those are 1:1 while for Slack
they will not be.

On `commitments`: `mention_count` and `mentions` carry the repeat count,
`origin`/`edited` are what make refresh unable to undo human work, and
`external_id`/`external_url`/`pushed_at` record where the row lives in Notion.
Any column added after the first release needs an explicit `ALTER` in
`db._ADDED_COLUMNS` — `CREATE TABLE IF NOT EXISTS` does nothing to a table that
already exists.

## Layout

```
schema.sql               events -> episodes -> commitments. Do not simplify
pkm/config.py            env-var config, no required values
pkm/db.py                all SQL. Board state lives here, not the browser
pkm/connectors/inbox.py  markdown + frontmatter reader
pkm/connectors/wispr.py  Wispr Flow -> inbox. Read-only, writes files only
pkm/connectors/notion.py push content out, pull status back. Off the ingest path
pkm/episodes.py          events -> episodes grouping
pkm/extract.py           one model call per episode + the verbatim-quote check
pkm/providers/           the only code that knows which model answers
pkm/prompts/             the extraction prompt, editable without touching code
pkm/notes.py             UI ingest, source detail, manual task creation
pkm/settings.py          reads/writes the provider settings in .env
pkm/dedup.py             Jaccard over stopworded tokens
pkm/sync.py              ingest -> extract -> dedup -> rows
pkm/server.py            stdlib http.server; board, notes and settings endpoints
pkm/board.html           the whole UI. No React, no bundler, no build step
pkm/status.py            board counts + timer liveness; human, JSON and SwiftBar
scripts/wispr-tick.sh    one tick of the unattended pipeline
scripts/ruger.5m.sh      SwiftBar plugin. A wrapper over `pkm status --swiftbar`
```

`pkm/board.html` follows Notion's dark UI deliberately, driven by custom
properties at the top of the file: one spacing scale, one radius scale, and
elevation built from a **lighter edge** plus a shadow that varies with height.
The token tables and the reasoning behind them are in
[`CLAUDE.md`](CLAUDE.md#design).

## Configuration

All optional. Real environment variables beat `.env`.

| | |
|---|---|
| `PKM_INBOX` `PKM_TRASH` `PKM_DB` | paths. Default under `~/.pkm`. Not writable over HTTP |
| `PKM_ME` | your names and aliases, comma-separated |
| `PKM_PROVIDER` `PKM_BASE_URL` `PKM_API_KEY` `PKM_MODEL` | which model answers. A base URL alone implies the OpenAI-compatible path |
| `PKM_CONTEXT_LENGTH` | refuse over-long transcripts before spending a call |
| `PKM_DEDUP_THRESHOLD` | Jaccard cutoff, default 0.6 |
| `PKM_HOST` `PKM_PORT` | default 127.0.0.1:8765 |
| `PKM_NOTION_TOKEN` `PKM_NOTION_DB` `PKM_NOTION_PARENT` | the Notion board. Ids accept a pasted URL |
| `PKM_WISPR_HOME` | where Wispr Flow keeps its data |
| `PKM_ENV_FILE` | where to read the `.env` from |

Provider-native names still work: `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`,
`GEMINI_API_KEY`, `GOOGLE_API_KEY`, `OPENAI_API_KEY`, `OPENAI_BASE_URL`,
`NOTION_TOKEN`.

## Tests

No framework — scripts that print PASS/FAIL and exit non-zero. **530
assertions** across ten suites.

```bash
.venv/bin/python scratch/test_ingest.py             # idempotent ingest             (22)
.venv/bin/python scratch/test_extraction.py         # quote check + dedup           (54)
.venv/bin/python scratch/test_providers.py          # JSON salvage + selection      (30)
.venv/bin/python scratch/test_ui.py                 # note ingest, sources, CSRF    (59)
.venv/bin/python scratch/test_tasks.py              # manual tasks, merge-refresh   (59)
.venv/bin/python scratch/test_notion.py             # push/pull vs a fake Notion   (119)
.venv/bin/python scratch/test_wispr.py              # Wispr import, end to end      (84)
.venv/bin/python scratch/test_capture_handoff.py    # capture layer -> inbox        (26)
.venv/bin/python scratch/test_status.py             # counts + timer liveness       (55)
cd scratch && ../.venv/bin/python test_server.py    # the endpoints                 (22)
.venv/bin/python scratch/test_live.py               # real provider call — COSTS TOKENS
```

Everything except `test_live.py` feeds **canned model output** — or, for Notion, a
stdlib HTTP server standing in for `api.notion.com` — through the real validator,
dedup and store path, so it costs nothing and needs no key. All of them use temp
directories and in-memory or temp SQLite; none touch `~/.pkm`.
