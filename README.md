# Talk2Me

Upload a document (PDF, Word, PowerPoint, Excel, TXT, Markdown, HTML) and have it read back
with realistic local voices ([Kokoro](https://github.com/thewh1teagle/kokoro-onnx)).

## Run

```bash
cp .env.example .env   # then set POSTGRES_PASSWORD
docker compose up -d --build
```

Open http://localhost:8000. The first start downloads the voice model (~340 MB) into the
`talk2me-models` volume; later starts are instant.

Postgres 17 + pgvector runs alongside the app (`talk2me-pg` volume). Uploads and the audio
cache live in the `talk2me-data` volume.

Every table is scoped by `user_id`. Until authentication is added, all requests run as
`DEFAULT_USER_EMAIL`; `current_user()` in `app/main.py` is the one place to plug in auth.

| Env var         | Default    | |
|-----------------|------------|---|
| `DEFAULT_VOICE` | `af_heart` | Any Kokoro voice id (`af_*`/`am_*` US, `bf_*`/`bm_*` UK, ...) |
| `MAX_UPLOAD_MB` | `50`       | |
| `DATABASE_URL`  | set by compose | `postgresql://user:pass@host:5432/db` |
| `DEFAULT_USER_EMAIL` | `owner@localhost` | User all requests run as (pre-auth) |

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
  ingest.py    markitdown -> speech-friendly sentences
  db.py        Postgres pool + migration runner
  migrations/  Schema (documents, conversations, memories w/ pgvector, tasks, reminders)
  tts/         TTSEngine interface + Kokoro implementation
  static/      Web player
```

## Roadmap

1. **Reader** (done): upload, sentence-by-sentence playback, resume position, audio cache.
2. **Assistant**: push-to-talk (STT), LLM with note/task/reminder tools, spoken reminders.
3. **Memory**: extract events/facts from conversations, hybrid (pgvector + Postgres full-text) recall,
   nightly journal, "you did something similar before" suggestions.
