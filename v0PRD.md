# Ruger v0 — Granola meetings → commitments → Notion

**Scope:** meetings only. One source, one processing layer, two outputs.
Email and Slack come later and must not require a rewrite.

Supersedes the full PRD for now. That document stays valid as the destination;
this one is the first leg.

> **Status: v0 is built and running.** Updated 2026-08-04 to describe what the
> code actually does. Sections 1–8 keep their original numbering because
> `CLAUDE.md` cites them. Where the original intent and the built thing differ,
> the difference is called out rather than quietly overwritten — the record of
> what changed is the useful part.
>
> Two things this document originally referenced never existed: a "main PRD"
> holding the schema, and a `board.html` that "already has the layout, palette,
> type and interactions". Both were designed from the constraints fixed here.

---

## 1. What it does

Reads Granola meeting transcripts, extracts the commitments made out loud —
mine and other people's — verifies each one against the transcript, and puts
them somewhere I will actually look.

**Two outputs, one processing layer:**

- a local board on `127.0.0.1:8765` for reviewing and correcting what was
  extracted, which is where you catch a bad card;
- a **Notion database** for living with the list day to day, because Notion
  already has views, filters, mobile, reminders and sharing, and none of that
  should be rebuilt here.

The centre of gravity moved during the build. v0 began as "a local commitment
board" and ended as **an ingestion and processing layer that feeds a board you
already use**. The local board did not go away — correcting extractions needs
the evidence next to the task, which a generic task tool cannot show — but it is
no longer where the work happens. See D9 and §9.

## 2. Why this slice first

Meetings are where commitments actually get spoken. Email and Slack mostly
*reference* decisions made in a call. Extraction quality is also highest here:
a transcript has clear speakers and clear promises, so if the extraction prompt
can't work on meetings it won't work anywhere.

Held up. The one surprise was the opposite of expected: Granola's *exported*
notes are already summarised, so they read as tidy bullet lists rather than
turn-by-turn dialogue. That broke an assumption in §5 about speaker labelling —
noted there.

## 3. Decisions

**D1 — Storage stays the full schema.** `events` → `episodes` → `commitments`,
even though only `source='meeting'` is populated. This is the whole reason
adding Slack later is a connector and not a migration. Do not build a simpler
meetings-only table, and do not drop `episode_events` because it is 1:1 today.

*Held.* `episode_events` is many-to-many as specced.

**D2 — Ingest via `~/.pkm/inbox/`.** Markdown files with frontmatter. Manual
copy-paste out of Granola for v0; automate the local-token pull once the
extraction is proven. Never make the undocumented API a dependency of the build.

*Held, and extended.* Pasting a note into the UI **writes a file to the inbox
first**, then ingests it through the ordinary connector. So there is still
exactly one ingest path and the database stays a derived thing you can delete
and rebuild. Nothing inserts straight into `events`.

**D3 — Both directions.** `direction='mine'` when I made the promise,
`'theirs'` when someone made it to me. Distinct colours on the board, because
these are two different kinds of anxiety and merging them makes the board
useless.

*Held; colours changed.* Indigo/teal became Notion's own purple `#9a6dd7` for
mine and green `#4dab9a` for theirs when the UI was rebuilt to Notion's design
language (D10). The distinction is the point, not the specific hues. It survives
the trip to Notion as select-option colours on a `Direction` property.

**D4 — One task, with a mention count.** When a new meeting produces a
commitment that matches an existing open one (same owner, similar text), do
**not** create a second row. Append to `commitments.mentions` and bump the
count. A task promised three weeks running is the most interesting thing on the
board and the card shows it.

Matching: same `owner`, plus normalised-text similarity above a threshold, among
open rows only. Token-overlap Jaccard over lowercased, stopworded tokens, ≥0.6.

*Held.* The LLM tie-break was never needed. `owner` is matched on a normalised
form that folds `PKM_ME` aliases and first person to `me`.

One refinement the build forced: **within a single meeting, a commitment's
identity is its QUOTE, not its task text.** Re-extraction that matched on task
text created a duplicate whenever a human had renamed a card, because the rename
dropped it below 0.6. The quote is a verbatim transcript line, so it survives
rewording. Cross-meeting merging still keys on task text — see §8 for the known
gap that leaves.

**D5 — Every task keeps its evidence.** The verbatim line that produced it, the
speaker, the meeting, the date. On the card face, not hidden in a detail view.
It's what makes a wrong extraction obvious in half a second instead of quietly
rotting on the board.

*Held, and carried into Notion.* Each pushed page gets the quote as an
`Evidence` property **and** as a quote block in the page body, so the evidence
does not stay behind when the task leaves.

**D6 — Board state lives in SQLite, not the browser.** Checking a box PATCHes
the server. No localStorage.

*Held.*

**D7 — Columns are status; grouping adds swimlanes.** To do / Doing / Done
across the top, optional rows by meeting or by person. Same renderer for all
three views.

*Held.*

**D8 — One place knows which model answers.** `pkm/providers/` is the only code
aware of the provider. Everything downstream treats a response as untrusted
either way.

- `anthropic` (default) — native structured outputs, so the JSON shape is
  enforced server-side.
- `gemini` — Gemini's OpenAI-compatible endpoint; honours `json_schema`.
- `openai` — any other `/v1/chat/completions` endpoint. Selected automatically
  when a base URL is set.

**Never assume a requested schema was applied.** Featherless serving
`google/gemma-4-31B-it` (measured 2026-08-04) returns HTTP 200 for a
`json_schema` request and silently ignores it, inventing its own key names.
Accepted-but-unenforced is worse than unsupported, because it looks like it
worked. So the OpenAI-compatible provider judges a response by **the shape that
came back, not the status code**, and walks down
`json_schema → json_object → plain prompting`, remembering what worked.

*Added during the build.* Not in the original document, which assumed one model.

**D9 — The board you live in is not the board Ruger draws.** Ruger's job ends at
a commitment that survived the quote check. Where you *work* the list is a
separate question, answered by pushing to Notion.

Each direction owns different fields:

| | `push` | `pull` |
|---|---|---|
| task, owner, direction, due, evidence, meeting | **writes** | ignores |
| status | writes **once**, at page creation | **reads back** |

**Push must never re-send status.** It is set when the page is created and never
again, so a card you dragged to Done stays there. The deliberate cost: once
pushed, ticking something off on the *local* board no longer reaches Notion.
Notion owns which column a card sits in.

*Added during the build.* Detail in §9.

**D10 — The UI is Notion's dark design language, specifically.** Everything is
driven by custom properties: one spacing scale, one radius scale, and elevation
built from a **lighter edge** plus a shadow that varies with height.

The mistake worth remembering: Notion's own card shadow is
`rgba(15,15,15,.1) 0 0 0 1px`, which is dark-on-white and correct in *their*
light mode. Copied into a `#191919` dark theme it is invisible, so every card
rendered as a flat rectangle and the whole UI read as unfinished placeholder
boxes. **On a dark ground, depth comes from a lighter edge, not a darker
shadow.**

*Added during the build.* Full token tables in `CLAUDE.md`.

## 4. Data

The full `events` → `episodes` → `commitments` schema, plus on `commitments`:

```sql
mention_count  INTEGER NOT NULL DEFAULT 1
mentions       TEXT     -- JSON array of ISO dates, oldest first

-- where the row came from, and whether a human has touched its content
origin         TEXT    NOT NULL DEFAULT 'extracted'  -- 'extracted' | 'manual'
edited         INTEGER NOT NULL DEFAULT 0

-- where it lives in the board the human actually works in
external_id    TEXT     -- Notion page id, NULL = never pushed
external_url   TEXT
pushed_at      TEXT
```

Plus `commitment_mentions` (the history behind `mention_count`: what was said,
when, by whom) and `extraction_drops` (every rejected commitment, with the
model's original JSON).

`status` is `'todo' | 'doing' | 'done'` on the board. The main PRD's
`'open'|'done'|'dropped'` collapses to this: `todo` and `doing` are both open.
Store the board value; derive the other when email/Slack arrive.

`origin` and `edited` exist so **refresh cannot undo human work** — see §8.
`external_id` is what makes push idempotent: without a stored page id, every
push would create a duplicate page for every row.

**Any column added after the first release needs an explicit `ALTER`.**
`CREATE TABLE IF NOT EXISTS` does nothing to a table that already exists, so
without one a database with real data in it breaks on the next query.

Keeping `extraction_drops` paid for itself. When the quote check improved, three
wrongly-dropped commitments were recovered from stored payloads with **no model
call** — `pkm revalidate` re-runs the current checker over what is already on
disk.

## 5. Extraction

One model call per meeting episode. Return JSON only:

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

Rules for the prompt:
- Only things someone committed to **doing**. Not topics discussed, not
  decisions made, not ideas raised. If nothing was promised, return `[]` — an
  empty board is correct output, not a failure.
- `quote` must appear verbatim in the transcript. Verify this in code and drop
  any commitment that fails, rather than trusting the model. This one check
  catches most hallucinated tasks; it is the highest-value code in the repo.
- Resolve relative dates ("by Friday", "next week") against the meeting date,
  which is passed in.

Model: `claude-haiku-4-5` on the Anthropic path. Cost is a couple of cents per
meeting. Overridable per D8.

**The quote check is tiered, and source-agnostic.** A match is recorded as
`exact`, `whitespace`, or `markup`; anything else is dropped as `not_found` or
`too_short`. The `markup` tier strips markdown, Slack and HTML formatting —
emphasis delimiters, links, bullets, blockquote and heading markers — before
comparing.

That tier was added after a real failure. Granola renders its "Next Steps"
section in bold, so a line stored as `- **Raise 50% of outstanding invoice**`
did not match the model's clean quote, and **every commitment in that block was
silently rejected.** The fix is deliberately not Granola-specific: any source
that decorates its text would have caused the same thing.

**Correction to the original §5.** It specified: *"Speaker attribution comes
from Granola's mic-vs-system labelling — microphone is me."* This is not
available. Granola's exported notes are summaries and carry no per-turn mic
labels. Attribution instead comes from **`PKM_ME`** — a comma-separated list of
your own names and aliases, folded to `me` during normalisation. Set it
correctly or every promise you made is filed as one made to you.

## 6. Board

Single localhost page, Python stdlib `http.server`. No Node, no React, no
bundler, no build step. `pkm/board.html` is the whole UI.

The original three endpoints grew into the set below, because controlling the
output turned out to matter more than viewing it.

| | |
|---|---|
| `GET /api/tasks` | all commitments with meeting + evidence joined |
| `PATCH /api/tasks/{id}` | status (drag or checkbox) **and** content edits |
| `POST /api/tasks` | add a task by hand, attached to a meeting |
| `DELETE /api/tasks/{id}` | |
| `POST /api/sync` | inbox ingest → extraction → return count |
| `GET /api/notes` · `GET /api/notes/{id}` | sources, and one source with its exact stored transcript, its tasks and its drops |
| `POST /api/notes` · `POST /api/uploads` | paste or upload a note (writes to the inbox first, per D2) |
| `DELETE /api/notes/{id}` | removes rows; moves the file to `~/.pkm/trash` |
| `POST /api/notes/{id}/reextract` | re-run extraction for one meeting and **merge** |
| `GET`/`PUT /api/config` · `POST /api/config/test` · `POST /api/config/reveal` · `GET /api/config/models` | credentials, visible and editable |
| `GET /api/version` | so a stale server process announces itself |

Card face shows: task text, the verbatim quote with speaker, source meeting, due
date (red when overdue), and mention pips. Clicking opens a panel with the full
history of when it was raised, and every field editable.

**Writes require an `X-Ruger: 1` header.** The process holds API keys and any
page in the browser can reach localhost; a custom header forces a CORS preflight
this server never approves. There is an Origin check too, and no permissive
CORS. `PKM_DB` and `PKM_INBOX` are deliberately **not** writable over HTTP — a
browser must not be able to repoint the database.

**`GET /api/version`** exists because of a real hour lost: `board.html` is read
from disk per request, so an old running server happily serves the *new* page
while routing on *old* code. Every new endpoint 404s and the page spins forever.
The page now checks the version and shows a red banner telling you to restart.

## 7. Done when

All met.

- [x] Drop real Granola exports into `~/.pkm/inbox`, run one command, open
      localhost to a board where **most tasks are ones I recognise as real.**
- [x] Checking a box survives a refresh.
- [x] Re-running the sync on the same files produces zero duplicate cards.
- [x] A commitment repeated across two meetings shows as one card reading `2×`.

Plus, added later:

- [x] Refreshing a meeting's extraction never undoes human work.
- [x] `pkm push` twice in a row creates nothing the second time.
- [x] A card dragged to Done in Notion stays in Done across later pushes.

Precision beats recall. Ten right tasks is a tool I open daily; thirty tasks
where twelve are noise is a tab I close. When tuning the prompt, prefer dropping
a borderline task over surfacing it.

**Tests:** no framework — scripts that print PASS/FAIL and exit non-zero.
**364 assertions** across seven suites. Everything except `test_live.py` feeds
canned model output through the real validator, dedup and store path, so it
costs nothing and needs no key. All use temp dirs and in-memory or temp SQLite;
none touch `~/.pkm`.

## 8. Not in v0

Auto-closing loops (detecting that a later meeting resolved a task), email,
Slack, embeddings, entity resolution beyond speaker names, the MCP server,
notifications, a morning digest.

The MCP server is a Phase 2 wrapper over the same tables. Don't scaffold it now.

**Two items were built anyway, on explicit instruction:** due-date editing and
adding a task by hand. Everything else on the list still stands.

Adding those forced a rule that is easy to break by accident. **Refresh must
never undo human work.** `sync.reextract_episode` MERGES; it does not clear the
episode first. Manual rows (`origin='manual'`) are untouched, rows a human
edited (`edited=1`) keep their wording and only get fresh evidence, and rows the
model stops returning are kept and reported rather than deleted — a worse second
extraction must not lose a real commitment.

### Known gaps

- **Cross-meeting dedup still keys on task text.** Two cards on the live board
  came from near-identical quotes but wordings sharing almost no tokens, so
  Jaccard scored them below 0.6 and both survived. Within a meeting this is
  already fixed by matching on quote (D4); across meetings it is not.
- **Granola's local API is still not automated** — step 4 below.
- **The Notion token has no Settings-page field.** It lives in `.env`, which is
  where `pkm notion setup` writes the database id too.

## 9. The Notion layer

`pkm/connectors/notion.py`. The local SQLite database stays the source of truth
for content; the Notion database is derived from it and rebuildable.

```bash
pkm notion                      # who the token is, and what it can see
pkm notion setup --parent URL   # create the database
pkm notion setup --database URL # or adopt one you already have
pkm push --dry-run              # what would go out
pkm push                        # send it
pkm pull                        # bring status changes back
pkm unlink                      # forget page ids; next push rebuilds
pkm sync --push                 # the whole chain
```

Two setup steps happen on Notion's side and no API call can do them for you:
create an internal integration for the token, then share the target page or
database with that integration. Skip the second and every call returns 404.

**Never assume the target database has Ruger's shape.** `board_profile` reads it
once before any write and fits every page to what came back. The live database
this was first pointed at — a stock Notion "Tasks" template — differed in both
ways that matter:

- its title column is called **`Name`**, not `Task`. A hardcoded title key
  creates a set of untitled cards.
- its Status is Notion's real **`status`** type offering *Not started / In
  progress / Done*. That needs `{"status": …}`, not `{"select": …}`, **and its
  options cannot be created over the API** — so writing Ruger's own `"To do"` is
  a 400 on every single page.

So local status is mapped onto an option that already exists, reading the
column's own vocabulary back through the same aliases `pull` uses. If nothing
matches, status is omitted rather than forced: a card in the default column is
recoverable, a card that 400'd and never arrived is not.

Reading status back is deliberately generous — renaming a Notion column is a
normal thing to do, so "Not started", "WIP" and "Completed" are all understood.
Anything unrecognised is left alone and counted, never guessed at.

**Nothing deletes across the boundary by default.** A Notion page whose
commitment is gone here is reported as orphaned and only archived with
`--prune`. A row whose page vanished from Notion is reported and kept. Same rule
as refresh.

The API version is pinned. Later Notion versions split a database into "data
sources" and change the shape of a page's `parent`, so the version is part of
the contract this connector implements, not an incidental header.

---

## 10. Capture — a task the moment you think of it

The meeting path is retrospective: something was said, and Ruger finds it later.
The gap it leaves is the thought you have walking away from the call. Capture
closes it: one keystroke, say what needs doing, and it is in Notion before you
have finished putting the laptop down.

**D11 — a capture is just another note in the inbox.** The dictated text is
written to `~/.pkm/inbox` and then read back through the ordinary connector, so
ingest → extract → dedup → push is one path with one more producer on the front
of it. No direct-to-Notion route, no second extractor, and the local board stays
the place a bad reading gets fixed (D5). This is D2 holding for a third time,
after the paste box and the Wispr importer.

**D12 — the microphone is Wispr Flow's, not Ruger's.** The menu item opens a
focused native dialog; you type into it, or hit your dictation shortcut and speak
into it. Ruger records no audio, ships no audio anywhere, and needs no
transcription model.

That is not a workaround, it is the better design. The extraction model on this
machine is `google/gemma-4-31B-it`, which is text-only, so audio would mean
switching providers or carrying a local Whisper — a second model, a second
failure mode, and a slower round trip, to reproduce a capability already running
on the machine. Recording behind a flag stays available if a hands-free version
is ever wanted; it is deliberately not built.

*SwiftBar cannot host a text field or a live mic — its plugins are text output
plus click actions. A dialog is the only seamless option available, which is why
this is a dialog.*

**D13 — straight through to Notion, then a notification.** Extract and push
without asking, then say what happened: "2 tasks added". A confirmation step
would add a click to the one flow whose entire value is that it is instant, and
the correction surface already exists in two places. The accepted cost: a
misheard word reaches Notion and is fixed there or on the board.

**D14 — captures own their prompt, and their quote thresholds.**
`prompts/extract_capture.md` is a separate file, because the meeting prompt is
tuned to be ruthless about chatter — *not ideas raised, not things someone
"should" do, drop anything with no named owner* — and a capture is the exact
opposite: deliberate, terse, and usually unaddressed. Run through the meeting
rules, "book the trade show banner" is an idea with no owner and gets dropped.

Owner defaults to **me** unless a name is spoken: "send Maya the deck" is mine,
"Maya is sending me the deck" is Maya's. §5's mine/theirs split stays meaningful
without making you narrate pronouns.

The quote minimums also have to move. §5 requires four words and fifteen
characters, which is right for a transcript and wrong for a note that is one
sentence long: "book the banner" is three words and would be dropped as
`too_short`. For `kind='capture'` the floor is two words and six characters.
**The contiguous-span requirement does not move** — that is the part that catches
invention, and it is the reason this is a threshold change rather than an
exemption.

**D15 — `kind='capture'`, not a new `source`.** `events.source` is constrained to
`meeting | email | slack`, and widening a SQLite `CHECK` means rebuilding the
table under a live database. `episodes.kind` carries no constraint, so a capture
is `source='meeting'`, `kind='capture'` at zero migration cost. D1's schema is
untouched.

**D16 — the menu bar item is a native app, because the box has to be there
already.** SwiftBar was the first version and it could not get there: its plugins
are text output plus click actions with no input widget, so capture was a second
click into a separate window. `menubar/RugerBar.swift` is an `NSStatusItem` whose
click opens a popover with the box focused and the counts underneath.

It owns no logic. Counts come from `pkm status --json`, a capture goes to
`pkm capture --notify`, and the test suite asserts the JSON keys the Swift reads —
rename one and the app would quietly render zeros. That is what keeps a compiled
component from becoming a second source of truth about the board.

*This is the repo's only build step, and it stays optional: `pkm status` and
`pkm capture` work without it. No app bundle, because a plain executable with an
`.accessory` policy is already a menu bar app.*

Also worth recording, because it looked like a limit and was not: AppleScript's
`display dialog` gives a **one-line** field that scrolls sideways. Dictating a
paragraph into it feels like hitting a character cap. The capture cap is 20,000
characters — around 3,000 words — and exists only to keep a transcript-sized paste
out of a single prompt.

---

## 11. The same job twice, and moving cards by saying so

Two problems that arrive together once capture makes it cheap to add work: the
same errand entering twice under different words, and having no way to close
something without opening a browser.

**D17 — D4's LLM tie-break, finally.** D4 said *"escalate to an LLM tie-break only
for candidate pairs if this misclassifies. Not yet — this is deliberately the dumb
version."* It has misclassified: Jaccard over stopworded tokens cannot see that
"Send Nila the KT doc" and "Share the handover sheet with Nila" are one delivery,
because after stopwording they share almost nothing.

`pkm/similar.py` asks a model, and three properties are non-negotiable:

- **It never blocks ingest.** A failing judge falls back to the lexical answer.
  Losing a meeting because a tie-break timed out would be far worse than the
  duplicate it was preventing.
- **One question, not one per pair.** Every candidate goes into a single call, so
  the cost is one call rather than N — which is what makes it affordable at all.
- **It merges only on a confident, unambiguous, offered id.** A wrong merge hides a
  real commitment inside another card; a wrong split leaves two cards you can see.
  An id that was not in the list it was shown is a hallucination and is refused.

*Calibration, measured rather than guessed.* The first floor was 0.2 overlap and
it was wrong: on the live board the paraphrase pairs worth asking about scored
**0.08 and 0.09**, so nothing ever reached the judge and `pkm dedupe` reported "no
duplicates" without having asked anything. The floor is 0.05, and the sweep asks
one question per row with no floor at all.

**D18 — the board is swept, not just the new arrivals.** Better matching from here
on does nothing about what is already there, so `pkm dedupe` reviews open rows and
prints its case before touching anything. `--apply` merges, keeping **every
mention**: the survivor's count then covers both askers and the detail panel still
shows who said what, because a merge must not erase the evidence that two people
wanted this (D5). The survivor is the row already in Notion, so the card someone
may have opened is the one that stays.

**D19 — an instruction may move a card, and Status finally goes out.** "mark the
deck one done", "push the invoice to friday", "that one is actually Maya's".
D9 says push never re-sends Status so a routine re-push cannot drag a card out of
Done — and it says the override "wants to be an explicit `push --force-status`".
This is it: `notion.push(force_status={ids})`, given only the ids an instruction
moved. Every other push is untouched.

**This is the only path where a model changes records that already exist**, so it
cannot lean on the verbatim-quote check — an instruction quotes nothing. The safety
is structural instead: only ids that were offered and are still open, only four
fields (status, due date, owner, rename), every value validated here rather than
trusted, no delete at all, and each change reported field by field so a wrong move
is visible immediately. **Ambiguity is refused, not guessed**: two candidate
matches means no change and a note saying why.

**D20 — one box, so it needs a router.** Run "mark the deck one done" through the
capture prompt and you get a new task called that, which is worse than doing
nothing. A small router call classifies create-vs-command first, and **every one of
its failure modes resolves to `create`**: a spurious task is visible and deletable,
while treating new work as a command would silently drop it.

*A bug worth recording, because it broke both features at once and did it
quietly.* The OpenAI-compatible provider judged every response by
`parsed["commitments"]` — one prompt's key, hardcoded into the shape check that
exists precisely because a schema might be ignored. So the judge and the router
were both read as wrong-shaped, walked the whole fallback ladder, and raised. The
judge reported itself "unavailable" and the router failed safe to `create`, which
is exactly why nobody would have noticed. The check now derives its required keys
from the schema it was handed.

### Subtasks — Phase 2, specced but not built

A captured task often implies its own steps, and the context to break it down is
already in the note. When this is built: **child to-do blocks in the Notion page
body**, not sub-item relations and not extra rows.

Sub-items would need the target database to have them enabled, which the API
cannot do — the same class of assumption that put seven untitled cards on the
first live board. Extra rows would multiply cards on both boards and need a
parent column, so a schema change. Checklist blocks work on any database
including a stock template, sit next to the evidence quote where they read as
part of the record, and cost nothing to add later.

---

## Build order

| | | |
|---|---|---|
| **Step 1** | Ingest — `schema.sql`, `db.py`, inbox connector, `episodes.py` | **done** |
| **Step 2** | Extraction — prompt + verbatim-quote validation + dedup | **done** |
| **Step 3** | Server + board | **done** |
| **Step 4** | Automate the Granola pull | superseded by the Wispr importer |
| **Step 5** | Notion push/pull | **done** |
| **Step 6** | The timer, `pkm status`, the menu bar app | **done** |
| **Step 7** | Capture (§10) — dialog → inbox → extract → Notion | **done** |
| **Step 8** | Fuzzy dedup + instructions (§11) | **done** |
| **Step 9** | Subtasks as checklist blocks (§10) | not started |

Step 4 was always conditional on step 2 proving worth it. It has, so this is the
next real piece of work — though `sync --push` plus paste-into-the-UI has made
manual ingest cheap enough that it is no longer urgent.

**Where the product is won or lost:** `pkm/prompts/extract_commitments.md`.
Iterate there, run `pkm sync --table`, eyeball the output against the
transcripts, repeat. Code changes are rarely the answer to a bad board.
