import asyncio
import logging
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import anthropic
from fastapi import FastAPI, HTTPException, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from . import auth
from .assistant import agent
from .auth import CurrentUser
from .config import settings
from .db import close_db, get_conn, init_db
from .ingest import document_to_sentences
from .stt import Transcriber
from .tts import KokoroEngine, TTSEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("talk2me")
for noisy in ("httpx", "httpx2", "huggingface_hub"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

SUPPORTED_EXTENSIONS = {
    ".pdf", ".docx", ".pptx", ".xlsx", ".txt", ".md", ".markdown", ".html", ".htm", ".csv", ".json", ".xml",
}
MAX_VOICE_MB = 10
STATIC_DIR = Path(__file__).parent / "static"

engine: TTSEngine | None = None
transcriber: Transcriber | None = None


async def _load_models() -> None:
    global engine, transcriber
    try:
        engine = await asyncio.to_thread(KokoroEngine, settings.models_dir)
        log.info("TTS engine '%s' ready with %d voices", engine.name, len(engine.voices()))
    except Exception:
        log.exception("Failed to load TTS engine")
    try:
        transcriber = await asyncio.to_thread(Transcriber, settings.whisper_model, settings.models_dir)
        log.info("Speech-to-text ready")
    except Exception:
        log.exception("Failed to load speech-to-text model")


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.ensure_dirs()
    await asyncio.to_thread(init_db)
    if not settings.anthropic_configured:
        log.warning("ANTHROPIC_API_KEY is not set; the assistant is disabled")
    # Load in the background so the UI can show progress during first-run model downloads.
    loader = asyncio.create_task(_load_models())
    yield
    loader.cancel()
    await asyncio.to_thread(close_db)


app = FastAPI(title="Talk2Me", lifespan=lifespan)
app.include_router(auth.router)


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, exc: RequestValidationError):
    # FastAPI echoes submitted values by default; never send passwords back or into logs.
    errors = [{k: v for k, v in e.items() if k not in ("input", "ctx")} for e in exc.errors()]
    return JSONResponse({"detail": errors}, status_code=422)


@app.middleware("http")
async def reject_cross_origin_writes(request: Request, call_next):
    """CSRF defence in depth on top of SameSite=Lax cookies."""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != request.headers.get("host"):
            return JSONResponse({"detail": "Cross-origin request rejected"}, status_code=403)
    return await call_next(request)


def _engine() -> TTSEngine:
    if engine is None:
        raise HTTPException(503, "TTS engine is still loading")
    return engine


def _check_voice(tts: TTSEngine, voice: str | None) -> str:
    voice = voice or settings.default_voice
    if voice not in tts.voices():
        raise HTTPException(400, f"Unknown voice '{voice}'")
    return voice


def _get_document(doc_id: int, user_id: int) -> dict:
    with get_conn() as conn:
        doc = conn.execute(
            "SELECT * FROM documents WHERE id = %s AND user_id = %s", (doc_id, user_id)
        ).fetchone()
    if doc is None:
        raise HTTPException(404, "Document not found")
    return doc


@app.get("/api/health")
def health():
    return {
        "ready": engine is not None,
        "speech_to_text": transcriber is not None,
        "assistant": settings.anthropic_configured,
    }


@app.get("/api/voices")
def voices(_: CurrentUser):
    tts = _engine()
    return {"default": settings.default_voice, "voices": tts.voices()}


# ---- documents -----------------------------------------------------------


@app.get("/api/documents")
def list_documents(user: CurrentUser):
    with get_conn() as conn:
        return conn.execute(
            "SELECT id, title, filename, position, jsonb_array_length(sentences) AS total, created_at "
            "FROM documents WHERE user_id = %s ORDER BY created_at DESC, id DESC",
            (user.id,),
        ).fetchall()


@app.post("/api/documents", status_code=201)
def upload_document(file: UploadFile, user: CurrentUser):
    filename = Path(file.filename or "upload").name
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise HTTPException(415, f"Unsupported file type '{suffix}'")

    with tempfile.NamedTemporaryFile(dir=settings.uploads_dir, suffix=suffix, delete=False) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = Path(tmp.name)

    try:
        if tmp_path.stat().st_size > settings.max_upload_mb * 1024 * 1024:
            raise HTTPException(413, f"File exceeds {settings.max_upload_mb} MB")
        try:
            sentences = document_to_sentences(tmp_path)
        except Exception as exc:
            log.exception("Failed to ingest %s", filename)
            raise HTTPException(422, f"Could not read document: {exc}") from exc
        if not sentences:
            raise HTTPException(422, "No readable text found (scanned PDFs need OCR)")

        with get_conn() as conn:
            doc_id = conn.execute(
                "INSERT INTO documents (user_id, title, filename, sentences) VALUES (%s, %s, %s, %s) RETURNING id",
                (user.id, Path(filename).stem, filename, Jsonb(sentences)),
            ).fetchone()["id"]
        tmp_path.rename(settings.uploads_dir / f"{doc_id}{suffix}")
    finally:
        tmp_path.unlink(missing_ok=True)

    return {"id": doc_id, "title": Path(filename).stem, "total": len(sentences)}


@app.get("/api/documents/{doc_id}")
def get_document(doc_id: int, user: CurrentUser):
    return _get_document(doc_id, user.id)


class Position(BaseModel):
    position: int


@app.put("/api/documents/{doc_id}/position", status_code=204)
def save_position(doc_id: int, body: Position, user: CurrentUser):
    with get_conn() as conn:
        conn.execute(
            "UPDATE documents SET position = %s WHERE id = %s AND user_id = %s",
            (max(0, body.position), doc_id, user.id),
        )


@app.delete("/api/documents/{doc_id}", status_code=204)
def delete_document(doc_id: int, user: CurrentUser):
    with get_conn() as conn:
        deleted = conn.execute(
            "DELETE FROM documents WHERE id = %s AND user_id = %s", (doc_id, user.id)
        ).rowcount
    if not deleted:
        raise HTTPException(404, "Document not found")
    shutil.rmtree(settings.audio_cache_dir / str(doc_id), ignore_errors=True)
    for f in settings.uploads_dir.glob(f"{doc_id}.*"):
        f.unlink(missing_ok=True)


@app.get("/api/documents/{doc_id}/audio/{index}")
def sentence_audio(
    doc_id: int,
    index: int,
    user: CurrentUser,
    voice: str | None = None,
    speed: float = Query(1.0, ge=0.5, le=2.0),
):
    tts = _engine()
    voice = _check_voice(tts, voice)
    sentences = _get_document(doc_id, user.id)["sentences"]
    if not 0 <= index < len(sentences):
        raise HTTPException(404, "Sentence index out of range")

    cache_dir = settings.audio_cache_dir / str(doc_id)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{tts.name}_{voice}_{speed:.2f}_{index}.wav"
    if not path.exists():
        tmp = path.with_suffix(".part")
        tmp.write_bytes(tts.synthesize_wav(sentences[index], voice, speed))
        tmp.rename(path)
    return FileResponse(path, media_type="audio/wav")


class SpeakRequest(BaseModel):
    text: str = Field(min_length=1, max_length=1500)
    voice: str | None = None
    speed: float = Field(1.0, ge=0.5, le=2.0)


@app.post("/api/tts")
def speak(body: SpeakRequest, _: CurrentUser):
    tts = _engine()
    voice = _check_voice(tts, body.voice)
    return Response(tts.synthesize_wav(body.text, voice, body.speed), media_type="audio/wav")


# ---- assistant -----------------------------------------------------------


def _assistant_reply(user, text: str) -> str:
    try:
        return agent.respond(user, text)
    except agent.AssistantUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except anthropic.RateLimitError as exc:
        raise HTTPException(429, "The assistant is busy right now; try again in a moment") from exc
    except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
        log.exception("Claude API error")
        raise HTTPException(502, "The assistant couldn't be reached; try again") from exc


class ChatRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


@app.post("/api/assistant/message")
def assistant_message(body: ChatRequest, user: CurrentUser):
    return {"transcript": body.text, "reply": _assistant_reply(user, body.text.strip())}


@app.post("/api/assistant/voice")
def assistant_voice(audio: UploadFile, user: CurrentUser):
    if transcriber is None:
        raise HTTPException(503, "Speech-to-text is still loading")
    with tempfile.NamedTemporaryFile(dir=settings.uploads_dir, suffix=".audio", delete=False) as tmp:
        shutil.copyfileobj(audio.file, tmp)
        tmp_path = Path(tmp.name)
    try:
        if tmp_path.stat().st_size > MAX_VOICE_MB * 1024 * 1024:
            raise HTTPException(413, f"Recording exceeds {MAX_VOICE_MB} MB")
        transcript = transcriber.transcribe(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)  # recordings are never kept
    if not transcript:
        return {"transcript": "", "reply": "Sorry, I didn't catch that."}
    return {"transcript": transcript, "reply": _assistant_reply(user, transcript)}


@app.get("/api/assistant/transcript")
def assistant_transcript(user: CurrentUser):
    return agent.current_transcript(user.id)


@app.post("/api/assistant/new", status_code=204)
def assistant_new_conversation(user: CurrentUser):
    agent.end_conversation(user.id)


@app.get("/api/tasks")
def list_tasks(user: CurrentUser, status: str = Query("open", pattern="^(open|done)$")):
    with get_conn() as conn:
        return conn.execute(
            "SELECT id, title, notes, due_at, outcome, completed_at FROM tasks WHERE user_id = %s AND status = %s "
            "ORDER BY due_at NULLS LAST, created_at LIMIT 100",
            (user.id, status),
        ).fetchall()


@app.get("/api/reminders")
def upcoming_reminders(user: CurrentUser):
    with get_conn() as conn:
        return conn.execute(
            "SELECT id, message, remind_at FROM reminders WHERE user_id = %s AND fired_at IS NULL "
            "ORDER BY remind_at LIMIT 100",
            (user.id,),
        ).fetchall()


@app.post("/api/reminders/due")
def claim_due_reminders(user: CurrentUser):
    """Polled by the browser. Atomically marks due reminders fired so each is delivered once."""
    with get_conn() as conn:
        return conn.execute(
            "UPDATE reminders SET fired_at = now() WHERE id IN ("
            " SELECT id FROM reminders WHERE user_id = %s AND fired_at IS NULL AND remind_at <= now()"
            " ORDER BY remind_at FOR UPDATE SKIP LOCKED"
            ") RETURNING id, message, remind_at",
            (user.id,),
        ).fetchall()


@app.get("/api/notes")
def recent_notes(user: CurrentUser):
    with get_conn() as conn:
        return conn.execute(
            "SELECT id, content, created_at FROM notes WHERE user_id = %s ORDER BY created_at DESC LIMIT 50",
            (user.id,),
        ).fetchall()


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
