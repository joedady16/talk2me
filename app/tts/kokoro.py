import logging
import urllib.request
from pathlib import Path

import numpy as np
from kokoro_onnx import Kokoro

from .base import TTSEngine

log = logging.getLogger(__name__)

_RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
MODEL_FILE = "kokoro-v1.0.onnx"
VOICES_FILE = "voices-v1.0.bin"

# Kokoro voice ids are prefixed by language: a=American, b=British English, etc.
_LANG_BY_PREFIX = {
    "a": "en-us", "b": "en-gb", "e": "es", "f": "fr-fr",
    "h": "hi", "i": "it", "j": "ja", "p": "pt-br", "z": "cmn",
}


def _ensure_file(path: Path, url: str) -> Path:
    if path.exists():
        return path
    log.info("Downloading %s (first run only)...", path.name)
    tmp = path.with_suffix(path.suffix + ".part")
    urllib.request.urlretrieve(url, tmp)
    tmp.rename(path)
    log.info("Saved %s", path)
    return path


class KokoroEngine(TTSEngine):
    name = "kokoro"

    def __init__(self, models_dir: Path):
        model = _ensure_file(models_dir / MODEL_FILE, f"{_RELEASE}/{MODEL_FILE}")
        voices = _ensure_file(models_dir / VOICES_FILE, f"{_RELEASE}/{VOICES_FILE}")
        self._kokoro = Kokoro(str(model), str(voices))

    def voices(self) -> list[str]:
        return sorted(self._kokoro.get_voices())

    def synthesize(self, text: str, voice: str, speed: float = 1.0) -> tuple[np.ndarray, int]:
        lang = _LANG_BY_PREFIX.get(voice[:1], "en-us")
        samples, sample_rate = self._kokoro.create(text, voice=voice, speed=speed, lang=lang)
        return samples, sample_rate
