-- Ruger — storage schema (v0).
--
-- Decision D1: this is the full events -> episodes -> commitments schema, even
-- though only source='meeting' is populated in v0. Adding email or Slack later
-- is a new connector that writes events and groups them into episodes; it is
-- not a migration. Do not "simplify" this into a meetings-only table.
--
--   events    raw atomic items as ingested. One Granola export = one event.
--             Later: one email = one event, one Slack message = one event.
--   episodes  a coherent unit of discourse that extraction runs over.
--             Meetings are 1 event -> 1 episode. Slack will be many message
--             events -> one channel-day episode; email, many -> one thread.
--   commitments  what someone promised to do, extracted from an episode.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY,
    source       TEXT    NOT NULL CHECK (source IN ('meeting', 'email', 'slack')),
    external_id  TEXT    NOT NULL,   -- stable id from the source (path, message id, ...)
    occurred_at  TEXT    NOT NULL,   -- ISO 8601 date or datetime
    actor        TEXT,               -- who produced it: speaker / sender / author
    title        TEXT,
    body         TEXT    NOT NULL,   -- the raw text, verbatim
    raw_path     TEXT,               -- file on disk it came from, if any
    content_hash TEXT    NOT NULL,   -- sha256 of body; changes mean re-extract
    ingested_at  TEXT    NOT NULL,
    UNIQUE (source, external_id)
);

CREATE TABLE IF NOT EXISTS episodes (
    id               INTEGER PRIMARY KEY,
    source           TEXT    NOT NULL CHECK (source IN ('meeting', 'email', 'slack')),
    external_id      TEXT    NOT NULL,
    kind             TEXT    NOT NULL,   -- 'meeting' | 'thread' | 'channel_day'
    title            TEXT,
    started_at       TEXT    NOT NULL,   -- ISO 8601; the meeting date
    ended_at         TEXT,
    participants     TEXT,               -- JSON array of names as spoken
    transcript       TEXT    NOT NULL,   -- text extraction runs against
    content_hash     TEXT    NOT NULL,   -- sha256 of transcript
    extracted_at     TEXT,               -- NULL = never extracted
    extracted_hash   TEXT,               -- content_hash at time of extraction
    extraction_model TEXT,
    UNIQUE (source, external_id)
);

-- Many-to-many on purpose: meetings are 1:1 today, Slack will not be.
CREATE TABLE IF NOT EXISTS episode_events (
    episode_id INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    event_id   INTEGER NOT NULL REFERENCES events(id)   ON DELETE CASCADE,
    PRIMARY KEY (episode_id, event_id)
);

CREATE TABLE IF NOT EXISTS commitments (
    id            INTEGER PRIMARY KEY,
    episode_id    INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    event_id      INTEGER          REFERENCES events(id)   ON DELETE SET NULL,

    task          TEXT    NOT NULL,   -- imperative, one line
    task_norm     TEXT    NOT NULL,   -- stopworded token string, for dedup
    direction     TEXT    NOT NULL CHECK (direction IN ('mine', 'theirs')),
    owner         TEXT    NOT NULL,   -- name as spoken, or 'me'
    owner_norm    TEXT    NOT NULL,   -- lowercased, aliases folded to 'me'
    due_date      TEXT,               -- YYYY-MM-DD or NULL

    -- D5: evidence lives on the card face, so it lives on the row.
    quote         TEXT    NOT NULL,   -- verbatim from the transcript
    speaker       TEXT,               -- who said it

    -- Board status. The main PRD's open|done|dropped collapses to this:
    -- todo and doing are both open. Store the board value, derive the other.
    status        TEXT    NOT NULL DEFAULT 'todo'
                  CHECK (status IN ('todo', 'doing', 'done')),

    -- D4: one task, with a mention count.
    mention_count INTEGER NOT NULL DEFAULT 1,
    mentions      TEXT,               -- JSON array of ISO dates, oldest first

    -- Where the row came from, and whether a human has touched its content.
    -- Re-extraction reads both: it never rewrites a manual row, and it never
    -- overwrites wording a human edited. Without these, "refresh" would
    -- silently undo your work.
    origin        TEXT    NOT NULL DEFAULT 'extracted'
                  CHECK (origin IN ('extracted', 'manual')),
    edited        INTEGER NOT NULL DEFAULT 0,

    -- Where this commitment lives in the board the human actually works in
    -- (Notion). Storing the page id locally is what makes push idempotent:
    -- without it a second push would create a duplicate page for every row.
    external_id   TEXT,               -- Notion page id, NULL = never pushed
    external_url  TEXT,               -- clickable link back to that page
    pushed_at     TEXT,               -- when its content last went out

    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);

-- The full history behind mention_count: what was said, when, by whom.
-- Feeds the detail panel (§6). mentions/mention_count stay in sync with this.
CREATE TABLE IF NOT EXISTS commitment_mentions (
    id            INTEGER PRIMARY KEY,
    commitment_id INTEGER NOT NULL REFERENCES commitments(id) ON DELETE CASCADE,
    episode_id    INTEGER NOT NULL REFERENCES episodes(id)    ON DELETE CASCADE,
    occurred_at   TEXT    NOT NULL,
    quote         TEXT    NOT NULL,
    speaker       TEXT,
    created_at    TEXT    NOT NULL,
    UNIQUE (commitment_id, episode_id, quote)
);

-- Dropped extractions: quote failed the verbatim check (§5). Kept so prompt
-- iteration has a record of what the model hallucinated.
CREATE TABLE IF NOT EXISTS extraction_drops (
    id         INTEGER PRIMARY KEY,
    episode_id INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    reason     TEXT    NOT NULL,
    payload    TEXT    NOT NULL,   -- the raw JSON object the model returned
    created_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_commitments_status     ON commitments(status);
CREATE INDEX IF NOT EXISTS idx_commitments_owner_norm ON commitments(owner_norm, status);
CREATE INDEX IF NOT EXISTS idx_commitments_episode    ON commitments(episode_id);
CREATE INDEX IF NOT EXISTS idx_episodes_started       ON episodes(started_at);
CREATE INDEX IF NOT EXISTS idx_events_source_time     ON events(source, occurred_at);
