import io
from abc import ABC, abstractmethod

import numpy as np
import soundfile as sf


class TTSEngine(ABC):
    """Swap engines (Kokoro, ElevenLabs, OpenAI, ...) by implementing this interface."""

    name: str

    @abstractmethod
    def voices(self) -> list[str]: ...

    @abstractmethod
    def synthesize(self, text: str, voice: str, speed: float = 1.0) -> tuple[np.ndarray, int]:
        """Return (mono float32 samples, sample rate)."""

    def synthesize_wav(self, text: str, voice: str, speed: float = 1.0) -> bytes:
        samples, sample_rate = self.synthesize(text, voice, speed)
        buf = io.BytesIO()
        sf.write(buf, samples, sample_rate, format="WAV", subtype="PCM_16")
        return buf.getvalue()
