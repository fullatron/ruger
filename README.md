# Ruger

Meeting notes in, verified tasks out.

Ruger reads what was said in a call, pulls out the things people committed to
doing, throws away anything it cannot back up with a word-for-word quote, and
sends the rest to Notion. You work the list in Notion. The local page is the log
of what was sent, with the evidence still attached.

```
Wispr Flow recordings ─┐
menu bar capture ──────┼──▶  ~/.pkm/inbox/*.md  ──▶  extract  ──▶  Notion
paste / drop a note ───┘         markdown files       + quote check      ▲
                                                                         │
                              the local page is the log of what went ────┘
```

Four things worth knowing up front:

- **Every task carries the line that produced it.** If the model invents a task,
  it usually cannot produce the quote that proves it, so the task gets dropped.
- **Notion owns a card once it exists.** Ruger creates it and never overwrites it
  again, so your edits there stick.
- **Files are the durable artifact.** Delete the database, sync again, and
  everything comes back from `~/.pkm/inbox`.
- **Precision over recall.** Ten right tasks make a tool you open daily. Thirty
  with twelve wrong make a tab you close.

Full spec: [`v0PRD.md`](v0PRD.md). Working notes for changing the code:
[`CLAUDE.md`](CLAUDE.md).

## Setup

```bash
uv venv .venv
uv pip install --python .venv/bin/python anthropic openai
```

Python 3.14 and two SDKs. No Node, no bundler, no build step (the menu bar app is
one optional `swiftc` call).

Settings live in a gitignored `.env` at the repo root, or in real environment
variables, which win. The **Settings** tab writes the same file — pick a provider,
paste a key, press **Test connection**.

| Provider | What you need |
|---|---|
| **Anthropic** (default) | `ANTHROPIC_API_KEY`, or nothing if you've run `ant auth login`. JSON shape is enforced server-side, which makes it the most reliable. |
| **Google Gemini** | A key from [aistudio.google.com/apikey](https://aistudio.google.com/apikey). |
| **OpenAI-compatible** | Any `/v1/chat/completions` endpoint — OpenAI, Featherless, Together, Groq, OpenRouter, vLLM, Ollama, LM Studio. Needs a base URL and a model id. |

```ini
# .env — gitignored, chmod 600
PKM_PROVIDER=anthropic
PKM_API_KEY=sk-ant-…
PKM_ME=Alex                 # your name, as it appears in your notes
PKM_NOTION_TOKEN=ntn_…      # a separate secret from the model key
PKM_NOTION_DB=…             # written by `pkm notion setup`
```

**`PKM_ME` matters more than it looks.** It decides who promised what in any note
without speaker labels. Get it wrong and every promise *you* made is filed as one
made *to* you.

Then connect Notion. Two steps happen on Notion's side and no API call can do them
for you: create an internal integration at notion.so/my-integrations for the
token, then open the parent page and use ••• → Connections to give that
integration access. Skip the second and every call returns 404.

```bash
.venv/bin/python -m pkm doctor                     # check what resolved
.venv/bin/python -m pkm notion setup --parent URL  # create the database
```

## Using it

```bash
sh scripts/build-menubar.sh     # optional: the menu bar app
sh scripts/install-agents.sh    # run everything automatically at login
```

That installs three background agents: the importer (every 5 minutes), the local
server, and the menu bar app. After that Ruger runs on its own.

**Capture.** Click the `◉` in the menu bar and a text box is already open. Type,
or use your dictation shortcut and speak. `⌘↩` sends it.

```
send maya the revised deck tomorrow and also book the trade show banner,
and I need to chase theo about the invoice by friday
        │
        ▼
· Send Maya the revised deck        due Aug 6
· Chase Theo about the invoice      due Aug 7
· Book the trade show banner        no date
```

Three seconds later a notification says what landed in Notion. Ruger records no
audio: the box is a text field, and dictation is your OS's job or Wispr Flow's.

**Instructions work in the same box.** "mark the deck one done", "push the invoice
to friday", "that banner task is actually Maya's". It works out whether you are
adding work or changing it. If two tasks could match, it changes nothing and says
which two.

**Meetings arrive on their own.** If you use Wispr Flow, finished calls are
imported every 5 minutes and turned into tasks without you doing anything. You can
also paste a note or drop `.md` files in the **Add notes** tab, or put files in
`~/.pkm/inbox` yourself.

**The log** is at [127.0.0.1:8765](http://127.0.0.1:8765): what you captured, what
you asked for, what was sent to Notion, what came back, and the quote behind each
task. It refreshes itself every few seconds, so you can leave it open. It is a
record, not a second task list, so there is nothing to drag or tick — that is
Notion's job. **Sources** shows each note's transcript, the tasks read out of it,
and everything the quote check dropped.

## Commands

```bash
.venv/bin/python -m pkm            # sync the inbox, then serve the log
```

| | |
|---|---|
| `pkm capture "…"` | one line → tasks → Notion (also takes instructions) |
| `pkm status` | board counts, and whether the timer is alive |
| `pkm sync [--table] [--push]` | ingest and extract; optionally print or push |
| `pkm wispr` | import Wispr Flow meetings into the inbox |
| `pkm push [--dry-run] [--resend]` | create the missing Notion pages |
| `pkm pull [--prune]` | bring status changes back |
| `pkm dedupe [--apply]` | find tasks that are the same job worded differently |
| `pkm drops` | what the quote check rejected, and why |
| `pkm revalidate [--apply]` | re-check old drops against the current checker |
| `pkm doctor` | inbox, database, credentials, your name |
| `pkm notion` | who the token is and what it can see |
| `pkm table` · `pkm serve` · `pkm unlink` · `pkm models` | print the board, serve only, forget Notion links, list models |

## Behaviour worth knowing

**Tasks get dropped, on purpose.** Every task must quote the notes word for word,
and that quote is checked against the transcript. Whitespace and formatting are
tolerated; reworded text is not. `pkm drops` shows what was rejected. A higher drop
rate from a smaller model is the system working, not failing.

**Notion owns a card once it exists.** Push creates pages and never overwrites
them, so a rename or a date you change in Notion sticks. The cost: a later, better
extraction will not update a card that already exists. `pkm push --resend`
overrides it.

**Status only travels one way,** Notion → Ruger, read back by `pkm pull`. The
exception is an instruction: "mark it done" or "push it to friday" is explicit
intent about that one card, so it is sent, content and all.

**The same job twice becomes one card.** Two people asking for the same thing
merges into one task with a `2×` count, keeping both quotes. Similar wording is
matched directly; a paraphrase is settled by asking the model, which only merges
when it is confident. `PKM_FUZZY_DEDUP=0` turns that off.

**A dictated note is read differently from a meeting.** Captures get their own
prompt and a lower minimum quote length, because "book the banner" is a real task
and three words long.

**A schema is requested, never trusted.** Some endpoints accept a JSON schema and
quietly ignore it, so a response is judged by the shape that came back rather than
the status code. Details in [`CLAUDE.md`](CLAUDE.md).

## Configuration

All optional; real environment variables beat `.env`.

| | |
|---|---|
| `PKM_INBOX` `PKM_TRASH` `PKM_DB` | paths, default under `~/.pkm` |
| `PKM_ME` | your names and aliases, comma-separated |
| `PKM_PROVIDER` `PKM_BASE_URL` `PKM_API_KEY` `PKM_MODEL` | which model answers |
| `PKM_CONTEXT_LENGTH` | refuse over-long transcripts before spending a call |
| `PKM_DEDUP_THRESHOLD` `PKM_FUZZY_DEDUP` | duplicate matching |
| `PKM_HOST` `PKM_PORT` | default 127.0.0.1:8765 |
| `PKM_NOTION_TOKEN` `PKM_NOTION_DB` `PKM_NOTION_PARENT` | the Notion board; ids accept a pasted URL |
| `PKM_WISPR_HOME` `PKM_TICK_LOG` `PKM_ENV_FILE` | where Wispr Flow, the tick log and `.env` live |

Provider-native names also work: `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`,
`OPENAI_API_KEY`, `OPENAI_BASE_URL`, `NOTION_TOKEN`.

## Layout

```
pkm/extract.py       one model call per note + the verbatim-quote check
pkm/capture.py       a dictated line -> inbox -> tasks -> Notion
pkm/instruct.py      "mark the deck one done" -> edits to existing tasks
pkm/similar.py       spotting the same job worded differently
pkm/connectors/      inbox (markdown), wispr (recordings), notion (push/pull)
pkm/prompts/         every prompt, editable without touching code
pkm/providers/       the only code that knows which model answers
pkm/db.py            all SQL          pkm/server.py   the local server
pkm/board.html       the whole UI     pkm/status.py   counts + timer liveness
menubar/             the menu bar app (Swift)
scripts/             the timer, the capture dialog, the installers
```

**Where the product is won or lost:** `pkm/prompts/`. Iterate there, run
`pkm sync --table`, compare the output against the transcripts, repeat. Code
changes are rarely the answer to a bad board.

## Tests

No framework — scripts that print PASS/FAIL and exit non-zero. **686 assertions**
across twelve suites, none of which need an API key or a network.

```bash
for t in scratch/test_*.py; do
  case "$t" in *test_live*) continue;; esac    # that one spends tokens
  .venv/bin/python "$t" || break
done
```

| | |
|---|---|
| `test_ingest` `test_extraction` `test_providers` | ingest, the quote check, JSON salvage |
| `test_ui` `test_tasks` `test_server` | notes, the read-only board, the endpoints |
| `test_notion` `test_wispr` `test_capture` | push/pull, the importer, capture |
| `test_status` `test_instruct` `test_capture_handoff` | the menu bar, instructions, the handoff contract |
| `test_live` | a real provider call. **Costs tokens** |

Everything except `test_live.py` feeds canned model output — or, for Notion, a
stdlib HTTP server standing in for `api.notion.com` — through the real validator,
dedup and store path. They use temp directories and never touch `~/.pkm`.
