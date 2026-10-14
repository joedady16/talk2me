"""Local speech-to-text with faster-whisper; audio never leaves the server."""

import logging
from pathlib import Path

from faster_whisper import WhisperModel

log = logging.getLogger(__name__)


class Transcriber:
    def __init__(self, model_name: str, models_dir: Path):
        log.info("Loading Whisper model '%s' (first run downloads it)...", model_name)
        self._model = WhisperModel(
            model_name, device="cpu", compute_type="int8", download_root=str(models_dir / "whisper")
        )

    def transcribe(self, audio_path: Path) -> str:
        segments, _ = self._model.transcribe(
            str(audio_path), language="en", vad_filter=True, beam_size=5
        )
        return " ".join(s.text.strip() for s in segments).strip()
