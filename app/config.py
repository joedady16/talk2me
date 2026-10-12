import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR", "./data")))
    models_dir: Path = field(default_factory=lambda: Path(os.getenv("MODELS_DIR", "./models")))
    default_voice: str = os.getenv("DEFAULT_VOICE", "af_heart")
    max_upload_mb: int = int(os.getenv("MAX_UPLOAD_MB", "50"))
    # No password default on purpose: supply it via DATABASE_URL (compose builds it from .env).
    database_url: str = os.getenv("DATABASE_URL", "postgresql://talk2me@localhost:5432/talk2me")
    db_pool_size: int = int(os.getenv("DB_POOL_SIZE", "10"))
    # Every request runs as this user until authentication is added.
    default_user_email: str = os.getenv("DEFAULT_USER_EMAIL", "owner@localhost")

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
