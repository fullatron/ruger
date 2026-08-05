# Ruger

Meeting notes in, verified commitments out. Ruger reads what was said in a call,
pulls out the things people committed to doing — yours and other people's —
throws away anything it cannot back up with a verbatim line from the notes, and
sends what survives to a Notion database. Notion owns the tasks from there; the
local page is the log of what was sent and what came back, with the evidence
still attached.

Spec: [`v0PRD.md`](v0PRD.md). Working notes: [`CLAUDE.md`](CLAUDE.md).

```
Wispr Flow recordings ─┐
capture from the menu ─┼──▶  ~/.pkm/inbox/*.md          files are the durable artifact
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
             Notion database              board.html
            (own the tasks here)      (the log of what was sent)
```

The database is derived. Delete `~/.pkm/ruger.db`, run the sync again, and the
board comes back from the files in the inbox.

## Setup

```bash
uv venv .venv
uv pip install --python .venv/bin/python anthropic openai
```

Python 3.14, stdlib only except those two SDKs. No Node, no bundler, and no build
step for anything that matters — the menu bar app is a single optional `swiftc`
call. Config comes from the process environment, falling back to a gitignored
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
| `python -m pkm push --resend` | overwrite content on pages that already exist |
| `python -m pkm status [--json]` | board counts, and whether the timer is still alive |
| `python -m pkm capture "…"` | one line → tasks → Notion. Reads stdin if no text is given |
| `python -m pkm dedupe [--apply]` | find open commitments that are the same job worded differently |
| `python -m pkm drops` | what the verbatim-quote check rejected, and why |
| `python -m pkm revalidate [--apply]` | re-check stored drops against the current checker. No model calls |
| `python -m pkm wispr [--dry-run --limit N]` | Wispr Flow → inbox |
| `python -m pkm doctor` | inbox, database, credentials, your name |
| `python -m pkm models [substring]` | what an OpenAI-compatible endpoint serves |
| `python -m pkm notion` | who the Notion token is, and what it can see |
| `python -m pkm notion setup --parent URL` | create the Notion database |
| `python -m pkm notion setup --database URL` | or adopt one you already have |
| `python -m pkm push [--dry-run --limit N]` | create the Notion pages that are missing |
| `python -m pkm pull [--dry-run --prune]` | bring status changes back |
| `python -m pkm unlink` | forget every Notion page id; the next push rebuilds |

## How notes get in

Three ways: the Wispr Flow importer on a timer, a capture from the menu bar, and
pasting or dropping a note in the board. All three land in `~/.pkm/inbox` as
markdown before anything becomes a row, so there is exactly one ingest path —
which is why adding a source later is a connector and not a migration, and why
nothing in the pipeline inserts straight into `events`.

**Capture — a task the moment you think of it.** One click on the menu bar icon
and a focused box is already open. Type, or hit your dictation shortcut and speak;
`⌘↩` captures. Three seconds later a notification says "2 tasks added to Notion".
It takes a paragraph or several — up to 20,000 characters, which is about 3,000
words.

```
send maya the revised deck tomorrow and also book the trade show banner,
and I need to chase theo about the invoice by friday
        │
        ▼
· Send Maya the revised deck        due 2026-08-06
· Chase Theo about the invoice      due 2026-08-07
· Book the trade show banner        no date
```

**Ruger records no audio.** The dialog is a text field, and dictating into it is
Wispr Flow's job or macOS's. That is not a workaround: the extraction model here
is text-only, so audio would mean a second model and a slower round trip to
reproduce something already running on the machine. Nothing is recorded, nothing
is uploaded, and the capture works the same whether you typed or spoke.

A capture is written to the inbox like everything else and read by the same
pipeline, with two differences, both because a dictated sentence is not a
transcript:

- **Its own prompt**, `prompts/extract_capture.md`. The meeting prompt is tuned to
  be ruthless about chatter — *not ideas raised, nothing with no named owner* —
  and a capture is the opposite. Under meeting rules, "book the trade show banner"
  is an ownerless idea and gets dropped.
- **A lower quote floor.** §5 wants four words and fifteen characters, which is
  right for a transcript and wrong for a note that is one sentence long. For a
  capture it is two words and six characters. The contiguous-span rule does not
  move, so an invented or reworded task still fails.

Owner defaults to you unless somebody else is clearly acting: "send Maya the deck"
is yours, "Maya is sending me the deck" is Maya's.

Tasks go to Notion immediately rather than waiting for the timer, and only the
capture's own tasks are sent, so it costs one API call instead of a re-send of the
whole board. A Notion outage costs you the notification, not the task: it stays on
the local board and the next push carries it out.

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

Three launchd agents, installed from templates so the paths match your checkout
rather than someone else's home directory:

```bash
sh scripts/install-agents.sh            # install or reinstall both
sh scripts/install-agents.sh --remove   # stop and remove both
```

| | |
|---|---|
| `ai.ruger.wispr` | `scripts/wispr-tick.sh` every 300s with `RunAtLoad`. One tick is import → sync → push → pull. Push is skipped when nothing new arrived; **pull always runs**, so the log's status never goes stale. Log: `~/.pkm/logs/wispr.log` |
| `ai.ruger.board` | `pkm serve`, `RunAtLoad` and `KeepAlive`, so the board answers on 8765 for as long as you are logged in. Log: `~/.pkm/logs/board.log` |
| `ai.ruger.menubar` | `build/RugerBar`, the menu bar app. Installed only once it has been built. Log: `~/.pkm/logs/menubar.log` |

**The board needs its own agent.** The timer only imports and extracts; it never
serves anything. Without `ai.ruger.board` nothing is listening on 8765, and every
link to the board fails — including the one in the menu bar.

```bash
launchctl kickstart -p gui/$(id -u)/ai.ruger.wispr     # run one tick now
launchctl print     gui/$(id -u)/ai.ruger.wispr | grep -E "runs|last exit"
launchctl bootout   gui/$(id -u)/ai.ruger.board        # stop the board
```

Re-running the installer boots each agent out before bootstrapping it again,
which is also how an edited plist takes effect: launchd caches the old definition
otherwise, and you end up debugging one that is no longer on disk.

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

### The menu bar app

`menubar/RugerBar.swift` — a filled green `◉ 11` when the timer ticked recently, a
hollow red `◌ 11` when it looks stopped. **One click and the capture box is
already there**, focused, in a popover rather than a window, with the counts
underneath it.

```bash
sh scripts/build-menubar.sh     # -> build/RugerBar
sh scripts/install-agents.sh    # picks it up and keeps it running at login
```

This is the repo's only build step and it stays optional: everything works without
it through `pkm status` and `pkm capture`. It needs the Xcode command line tools
for `swiftc` (`xcode-select --install`). No app bundle — a plain executable with an
`.accessory` activation policy *is* a menu bar app, and a bundle would add signing
and `Info.plist` upkeep for something launchd starts rather than Finder.

**The app owns no logic.** Counts come from `pkm status --json` and a capture is
handed to `pkm capture --notify`, so the rules stay in Python where they are
testable and the menu cannot disagree with the board. `test_status.py` asserts the
exact JSON keys the Swift parses, because renaming one would leave the app quietly
rendering zeros.

Open board is hidden unless something is listening on the port: a dead link is
worse than no link, since the click just fails in the browser with nothing to act
on.

<details>
<summary>SwiftBar plugin, the earlier version</summary>

`scripts/ruger.5m.sh` renders the same data as a [SwiftBar](https://swiftbar.app)
menu, via `pkm status --swiftbar`. It is kept because it needs no compiler, but it
cannot host a text field — SwiftBar plugins are text output plus click actions — so
capture is a second click and a separate dialog there.

```bash
brew install --cask swiftbar
mkdir -p ~/.swiftbar-plugins
ln -s "$PWD/scripts/ruger.5m.sh" ~/.swiftbar-plugins/ruger.5m.sh
defaults write com.ameba.SwiftBar PluginDirectory -string "$HOME/.swiftbar-plugins"
open -a SwiftBar
```

The `5m` is SwiftBar's refresh interval, matching the timer's 300s.
</details>

## The activity log

`http://127.0.0.1:8765`. One page, `pkm/board.html`, served by the stdlib
`http.server`.

**This is not a task board.** Notion owns a card once it exists, so the local page
is the record of what Ruger sent there and what came back — with the evidence
still attached, which is the one thing Notion cannot show you.

| | |
|---|---|
| **Activity** | the log: sent to Notion, status changed, removed |
| **Add notes** | paste a meeting, or drop `.md` files |
| **Sources** | every note stored; open one for its transcript, tasks and drops |
| **Settings** | provider, model, API key, your name, context limit |
| **Sync inbox** | ingest and extract whatever is new in `~/.pkm/inbox` |
| **Appearance** | dark (the default) or light |

Entries are grouped by day, newest first, and each one carries the verbatim line
that produced the task, who said it, which meeting, the last known status, and a
link straight to the Notion card. Filter to everything, just what was sent, or
just status changes.

There is no status control, no drag, no checkbox and no editing: `PATCH
/api/tasks/{id}` is gone and answers **405 with an explanation**, because a stale
page still holds the old handlers and a 404 would read as a routing bug. Delete
stays — a bad extraction still needs removing, and the log records that too.

Status is still stored, as **last known from Notion**. `pkm pull` refreshes it on
every tick, the menu bar counts it, and dedup needs it to know which rows are
still open.

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

**Token overlap cannot see a paraphrase.** "Send Nila the KT doc" and "Share the
handover sheet with Nila" are one delivery and share almost nothing after
stopwording. So when overlap is plausible but under the threshold, a model is asked
whether it is the same job — the tie-break D4 deferred until the dumb version was
proven to misclassify. Three rules make it safe to have in the ingest path:

- **It never blocks ingest.** A failing judge falls back to the lexical answer.
- **One question, not one per pair** — every candidate goes into a single call.
- **It merges only on a confident, unambiguous, offered id.** A wrong merge hides a
  real commitment; a wrong split leaves two cards you can see. An id it was not
  shown is refused.

Turn it off with `PKM_FUZZY_DEDUP=0` for a run that must cost nothing.

Better matching does nothing about duplicates already on the board, so
`pkm dedupe` sweeps the open ones and prints its case before touching anything:

```
$ pkm dedupe
1 pair(s) look like the same job:

  keep  #4    Send Nila the KT doc
  merge #9    Share the handover sheet with Nila
        llm · overlap 0.09 · KT and handover are the same document here

nothing changed. Re-run with --apply to merge them.
```

`--apply` merges, keeping **every mention**, so the survivor's count covers both
askers and the panel still shows who said what: a merge must not erase the evidence
that two people wanted this. The survivor is the row already in Notion, so the card
someone may have opened is the one that stays. `--archive` also archives the
duplicate's Notion page.

The floor was calibrated against a real board, not guessed. At 0.2 the judge was
never consulted, because the pairs worth asking about scored 0.08 and 0.09 — two
descriptions of one errand often share only the person's name.

### Moving cards by saying so

The capture box takes instructions as well as new work: *"mark the deck one done"*,
*"push the invoice chase to friday"*, *"that banner task is actually Maya's"*. A
small router call decides create-vs-command first, because running an instruction
through the capture prompt would produce a new task called "Mark the deck one done".
Every router failure resolves to `create`: a spurious task is visible and
deletable, while treating new work as a command would silently drop it.

**This is the only path where the model changes records that already exist**, so it
cannot lean on the quote check — an instruction quotes nothing. The safety is
structural:

- only ids it was shown, and only rows still open;
- only four fields, all reversible: status, due date, owner, rename. **No delete**;
- every value validated here rather than trusted — a bad status, an unparseable
  date and an empty rename are all refused and reported;
- **ambiguity is refused, not guessed.** Two candidate matches means no change and
  a note saying which two.

A card an instruction moved **does** reach Notion, via the explicit
`force_status` override. Every other push still leaves Status alone, so a routine
re-push can never drag a card out of a column you moved it to.

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
pkm/similar.py           the LLM tie-break for paraphrased duplicates
pkm/instruct.py          "mark the deck one done" -> edits to existing rows
pkm/sync.py              ingest -> extract -> dedup -> rows
pkm/server.py            stdlib http.server; board, notes and settings endpoints
pkm/board.html           the whole UI. No React, no bundler, no build step
pkm/status.py            board counts + timer liveness; human, JSON and SwiftBar
pkm/capture.py           a dictated line -> inbox -> tasks -> Notion
menubar/RugerBar.swift   the menu bar app. Shells into pkm, owns no rules
scripts/wispr-tick.sh    one tick of the unattended pipeline
scripts/ruger.5m.sh      SwiftBar plugin. A wrapper over `pkm status --swiftbar`
scripts/ruger-capture.sh the capture dialog, for the SwiftBar route
scripts/capture-dialog.js a real multi-line box, built through the ObjC bridge
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

No framework — scripts that print PASS/FAIL and exit non-zero. **681
assertions** across twelve suites.

```bash
.venv/bin/python scratch/test_ingest.py             # idempotent ingest             (22)
.venv/bin/python scratch/test_extraction.py         # quote check + dedup           (54)
.venv/bin/python scratch/test_providers.py          # JSON salvage + shape check    (38)
.venv/bin/python scratch/test_ui.py                 # note ingest, sources, CSRF    (59)
.venv/bin/python scratch/test_tasks.py              # read-only board, merge-refresh(66)
.venv/bin/python scratch/test_notion.py             # push/pull vs a fake Notion   (130)
.venv/bin/python scratch/test_wispr.py              # Wispr import, end to end      (84)
.venv/bin/python scratch/test_capture_handoff.py    # capture layer -> inbox        (26)
.venv/bin/python scratch/test_status.py             # counts, liveness, contract    (79)
.venv/bin/python scratch/test_capture.py            # capture -> tasks -> Notion    (49)
cd scratch && ../.venv/bin/python test_server.py    # the endpoints, and the log    (26)
.venv/bin/python scratch/test_live.py               # real provider call — COSTS TOKENS
```

Everything except `test_live.py` feeds **canned model output** — or, for Notion, a
stdlib HTTP server standing in for `api.notion.com` — through the real validator,
dedup and store path, so it costs nothing and needs no key. All of them use temp
directories and in-memory or temp SQLite; none touch `~/.pkm`.
