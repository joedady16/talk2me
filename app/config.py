import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool_env(name: str) -> bool | None:
    value = os.getenv(name, "").strip().lower()
    if value in ("1", "true", "yes"):
        return True
    if value in ("0", "false", "no"):
        return False
    return None


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR", "./data")))
    models_dir: Path = field(default_factory=lambda: Path(os.getenv("MODELS_DIR", "./models")))
    default_voice: str = os.getenv("DEFAULT_VOICE", "af_heart")
    max_upload_mb: int = int(os.getenv("MAX_UPLOAD_MB", "50"))
    # No password default on purpose: supply it via DATABASE_URL (compose builds it from .env).
    database_url: str = os.getenv("DATABASE_URL", "postgresql://talk2me@localhost:5432/talk2me")
    db_pool_size: int = int(os.getenv("DB_POOL_SIZE", "10"))

    # Auth. The first account can always be created; more need ALLOW_SIGNUP=true.
    allow_signup: bool = _bool_env("ALLOW_SIGNUP") or False
    # None = secure cookies whenever the request arrived over HTTPS.
    cookie_secure: bool | None = _bool_env("COOKIE_SECURE")
    # Pre-auth placeholder user whose data the first signup inherits.
    legacy_user_email: str = os.getenv("LEGACY_USER_EMAIL", "owner@localhost")

    # Assistant
    claude_model: str = os.getenv("CLAUDE_MODEL", "claude-opus-5-5")
    claude_effort: str = os.getenv("CLAUDE_EFFORT", "low")  # conversational turns; raise for harder tasks
    whisper_model: str = os.getenv("WHISPER_MODEL", "base.en")
    conversation_idle_minutes: int = int(os.getenv("CONVERSATION_IDLE_MINUTES", "30"))

    @property
    def anthropic_configured(self) -> bool:
        return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def audio_cache_dir(self) -> Path:
        return self.data_dir / "audio"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.models_dir, self.uploads_dir, self.audio_cache_dir):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
