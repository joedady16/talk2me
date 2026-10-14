-- Accounts: users without a password_hash can't log in. The pre-auth placeholder user
-- (if present) is claimed by the first signup so its data is kept.
ALTER TABLE users
    ADD COLUMN password_hash TEXT,
    ADD COLUMN timezone TEXT NOT NULL DEFAULT 'UTC';

-- Only a SHA-256 of the session token is stored, so a DB leak doesn't leak live sessions.
CREATE TABLE sessions (
    token_hash   BYTEA PRIMARY KEY,
    user_id      BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL,
    user_agent   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX sessions_user_idx ON sessions (user_id);
CREATE INDEX sessions_expiry_idx ON sessions (expires_at);

CREATE TABLE notes (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    content    TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX notes_user_idx ON notes (user_id, created_at DESC);

-- The exact Claude API message history for the conversation (content blocks incl. thinking
-- and tool calls). Append-only: earlier entries must never be edited, or the API rejects
-- the replayed thinking blocks. `messages` remains the human-readable transcript.
ALTER TABLE conversations
    ADD COLUMN api_messages JSONB NOT NULL DEFAULT '[]',
    ADD COLUMN last_activity_at TIMESTAMPTZ NOT NULL DEFAULT now();
CREATE INDEX conversations_activity_idx ON conversations (user_id, last_activity_at DESC);

CREATE INDEX reminders_user_due_idx ON reminders (user_id, remind_at) WHERE fired_at IS NULL;
