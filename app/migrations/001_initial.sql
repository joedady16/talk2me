CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE users (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email        TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL DEFAULT '',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE documents (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title      TEXT NOT NULL,
    filename   TEXT NOT NULL,
    sentences  JSONB NOT NULL,             -- array of speakable sentences
    position   INTEGER NOT NULL DEFAULT 0, -- last sentence index played
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX documents_user_idx ON documents (user_id, created_at DESC);

-- Raw transcripts: every exchange is kept so memory can be (re)built from it.
CREATE TABLE conversations (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at   TIMESTAMPTZ,
    summary    TEXT,
    processed  BOOLEAN NOT NULL DEFAULT false  -- true once memories have been extracted
);
CREATE INDEX conversations_user_idx ON conversations (user_id, started_at DESC);
CREATE INDEX conversations_unprocessed_idx ON conversations (id) WHERE NOT processed;

CREATE TABLE messages (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    conversation_id BIGINT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content         TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX messages_conversation_idx ON messages (conversation_id, id);

-- Distilled long-term memories. 'event' = something the user did, 'fact' = something true about them.
-- embedding dimension matches bge-small-en-v1.5 (384); change via a migration if the model changes.
CREATE TABLE memories (
    id                     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id                BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind                   TEXT NOT NULL CHECK (kind IN ('event', 'fact')),
    content                TEXT NOT NULL,
    tags                   TEXT[] NOT NULL DEFAULT '{}',
    occurred_at            TIMESTAMPTZ,
    source_conversation_id BIGINT REFERENCES conversations(id) ON DELETE SET NULL,
    superseded_by          BIGINT REFERENCES memories(id) ON DELETE SET NULL,
    embedding              vector(384),
    search                 TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX memories_user_idx ON memories (user_id, created_at DESC) WHERE superseded_by IS NULL;
CREATE INDEX memories_search_idx ON memories USING gin (search);
CREATE INDEX memories_tags_idx ON memories USING gin (tags);
CREATE INDEX memories_embedding_idx ON memories USING hnsw (embedding vector_cosine_ops);

CREATE TABLE tasks (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id      BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title        TEXT NOT NULL,
    notes        TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done')),
    due_at       TIMESTAMPTZ,
    outcome      TEXT,  -- "how did it go?" answer; feeds future recall
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);
CREATE INDEX tasks_user_idx ON tasks (user_id, status, due_at);

CREATE TABLE reminders (
    id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id   BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    task_id   BIGINT REFERENCES tasks(id) ON DELETE CASCADE,
    message   TEXT NOT NULL,
    remind_at TIMESTAMPTZ NOT NULL,
    fired_at  TIMESTAMPTZ
);
CREATE INDEX reminders_due_idx ON reminders (remind_at) WHERE fired_at IS NULL;

CREATE TABLE journal (
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    day     DATE NOT NULL,
    summary TEXT NOT NULL,
    PRIMARY KEY (user_id, day)
);
