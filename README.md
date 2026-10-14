# Talk2Me

- **Reader:** upload a document (PDF, Word, PowerPoint, Excel, TXT, Markdown, HTML) and have it
  read back with realistic local voices ([Kokoro](https://github.com/thewh1teagle/kokoro-onnx)).
- **Assistant:** talk to it to capture notes, tasks and reminders. Speech is transcribed locally
  ([faster-whisper](https://github.com/SYSTRAN/faster-whisper)), Claude decides what to do, and
  replies and reminders are spoken back.

## Run

```bash
cp .env.example .env   # set POSTGRES_PASSWORD and ANTHROPIC_API_KEY
docker compose up -d --build
```

Open http://localhost:8000 and create your account. The first start downloads the voice
(~340 MB) and speech-recognition (~150 MB) models into the `talk2me-models` volume.

The microphone only works on `localhost` or over HTTPS (browser rule). To use it from another
device, put the app behind a TLS reverse proxy (Caddy, Traefik, nginx).

Postgres 17 + pgvector runs alongside the app (`talk2me-pg` volume). Uploads and the audio
cache live in the `talk2me-data` volume.

## Accounts

Email + password (Argon2id), server-side sessions in an HttpOnly, SameSite=Lax cookie (only a
SHA-256 of the token is stored). Every table is scoped by `user_id`.

- The **first** account can always be created. Further sign-ups need `ALLOW_SIGNUP=true`.
- The first account inherits any data created before accounts existed.
- Logins are rate-limited per email and per IP (in-process; use a shared store if you run
  several app instances).
- Cross-origin writes are rejected. Cookies are `Secure` automatically over HTTPS
  (`COOKIE_SECURE=true/false` to force).

| Env var         | Default    | |
|-----------------|------------|---|
| `DEFAULT_VOICE` | `af_heart` | Any Kokoro voice id (`af_*`/`am_*` US, `bf_*`/`bm_*` UK, ...) |
| `MAX_UPLOAD_MB` | `50`       | |
| `DATABASE_URL`  | set by compose | `postgresql://user:pass@host:5432/db` |
| `ANTHROPIC_API_KEY` | | Required for the assistant |
| `CLAUDE_MODEL`  | `claude-opus-5-5` | |
| `CLAUDE_EFFORT` | `low` | `low`…`max`; low keeps voice replies fast |
| `WHISPER_MODEL` | `base.en` | `small.en` is more accurate, slower |
| `ALLOW_SIGNUP`  | `false` | Allow accounts beyond the first |
| `COOKIE_SECURE` | auto | Force the cookie `Secure` flag on/off |

## Database migrations

Add `app/migrations/NNN_description.sql`; pending files are applied in order at startup
(guarded by an advisory lock, tracked in `schema_migrations`). Never edit an applied migration.

## Run without Docker

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
docker compose up -d db   # and uncomment its ports: mapping in docker-compose.yml
DATABASE_URL=postgresql://talk2me:<password>@localhost:5432/talk2me uvicorn app.main:app --reload
```

## Layout

```
app/
  main.py      FastAPI routes + static UI
  auth.py      Accounts, sessions, rate limiting
  stt.py       faster-whisper transcription
  assistant/   agent.py (Claude conversation loop), tools.py (notes/tasks/reminders)
  ingest.py    markitdown -> speech-friendly sentences
  db.py        Postgres pool + migration runner
  migrations/  Schema (documents, conversations, memories w/ pgvector, tasks, reminders)
  tts/         TTSEngine interface + Kokoro implementation
  static/      Web player
```

## Roadmap

1. **Reader** (done): upload, sentence-by-sentence playback, resume position, audio cache.
2. **Assistant** (done): accounts, push-to-talk, Claude with note/task/reminder tools, spoken reminders.
3. **Memory**: extract events/facts from conversations, hybrid (pgvector + Postgres full-text) recall,
   nightly journal, "you did something similar before" suggestions.

## Assistant notes

- Conversation history sent to Claude is stored verbatim (`conversations.api_messages`) and is
  append-only. Don't edit earlier entries or change `SYSTEM_PROMPT`/`TOOLS` per request: that
  breaks prompt caching and invalidates replayed thinking blocks.
- A conversation ends after 30 idle minutes (`CONVERSATION_IDLE_MINUTES`) or "New conversation".
- Reminders are delivered by the open browser tab (polled every 20 s, each delivered once).
  Reminders due while no tab is open are spoken the next time you open the app.
- Voice recordings are transcribed and deleted immediately; only the text is kept.
