import asyncio
import logging
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from .config import settings
from .db import close_db, ensure_user, get_conn, init_db
from .ingest import document_to_sentences
from .tts import KokoroEngine, TTSEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("talk2me")

SUPPORTED_EXTENSIONS = {
    ".pdf", ".docx", ".pptx", ".xlsx", ".txt", ".md", ".markdown", ".html", ".htm", ".csv", ".json", ".xml",
}
STATIC_DIR = Path(__file__).parent / "static"

engine: TTSEngine | None = None
default_user_id: int | None = None


async def _load_engine() -> None:
    global engine
    try:
        engine = await asyncio.to_thread(KokoroEngine, settings.models_dir)
        log.info("TTS engine '%s' ready with %d voices", engine.name, len(engine.voices()))
    except Exception:
        log.exception("Failed to load TTS engine")


@asynccontextmanager
async def lifespan(_: FastAPI):
    global default_user_id
    settings.ensure_dirs()
    await asyncio.to_thread(init_db)
    default_user_id = await asyncio.to_thread(ensure_user, settings.default_user_email, "Owner")
    # Load in the background so the UI can show progress during the first-run model download.
    loader = asyncio.create_task(_load_engine())
    yield
    loader.cancel()
    await asyncio.to_thread(close_db)


app = FastAPI(title="Talk2Me", lifespan=lifespan)


def current_user() -> int:
    """The single place to swap in real authentication (session/JWT) later."""
    return default_user_id


UserId = Annotated[int, Depends(current_user)]


def _engine() -> TTSEngine:
    if engine is None:
        raise HTTPException(503, "TTS engine is still loading")
    return engine


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
    return {"ready": engine is not None}


@app.get("/api/voices")
def voices():
    tts = _engine()
    return {"default": settings.default_voice, "voices": tts.voices()}


@app.get("/api/documents")
def list_documents(user_id: UserId):
    with get_conn() as conn:
        return conn.execute(
            "SELECT id, title, filename, position, jsonb_array_length(sentences) AS total, created_at "
            "FROM documents WHERE user_id = %s ORDER BY created_at DESC, id DESC",
            (user_id,),
        ).fetchall()


@app.post("/api/documents", status_code=201)
def upload_document(file: UploadFile, user_id: UserId):
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
                (user_id, Path(filename).stem, filename, Jsonb(sentences)),
            ).fetchone()["id"]
        tmp_path.rename(settings.uploads_dir / f"{doc_id}{suffix}")
    finally:
        tmp_path.unlink(missing_ok=True)

    return {"id": doc_id, "title": Path(filename).stem, "total": len(sentences)}


@app.get("/api/documents/{doc_id}")
def get_document(doc_id: int, user_id: UserId):
    return _get_document(doc_id, user_id)


class Position(BaseModel):
    position: int


@app.put("/api/documents/{doc_id}/position", status_code=204)
def save_position(doc_id: int, body: Position, user_id: UserId):
    with get_conn() as conn:
        conn.execute(
            "UPDATE documents SET position = %s WHERE id = %s AND user_id = %s",
            (max(0, body.position), doc_id, user_id),
        )


@app.delete("/api/documents/{doc_id}", status_code=204)
def delete_document(doc_id: int, user_id: UserId):
    with get_conn() as conn:
        deleted = conn.execute(
            "DELETE FROM documents WHERE id = %s AND user_id = %s", (doc_id, user_id)
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
    user_id: UserId,
    voice: str | None = None,
    speed: float = Query(1.0, ge=0.5, le=2.0),
):
    tts = _engine()
    voice = voice or settings.default_voice
    if voice not in tts.voices():
        raise HTTPException(400, f"Unknown voice '{voice}'")

    sentences = _get_document(doc_id, user_id)["sentences"]
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


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
