"""Sortie audio multiplateforme via sounddevice/PortAudio (Windows, macOS, fallback Linux).

Même interface que ALSAOut : write(block (frames, ch) float32) -> int, close().
`import sounddevice` est paresseux (dans __init__) : importer ce module ne charge
jamais PortAudio.
"""
import numpy as np


class SDOut:
    """Sortie PCM temps réel ; write() bloque le temps que PortAudio consomme le bloc."""

    def __init__(self, rate: int = 48000, channels: int = 2, period: int = 1024, buffer_frames: int = 8192):
        try:
            import sounddevice as sd
        except ImportError as exc:
            raise OSError("sounddevice requis : pip install sounddevice") from exc
        self.rate, self.channels, self.period = rate, channels, period
        # ponytail: buffer_frames accepté pour l'interface, latence PortAudio par défaut ;
        # passer latency=buffer_frames/rate si des dropouts apparaissent sur une machine faible
        try:
            self._stream = sd.OutputStream(samplerate=rate, channels=channels,
                                           dtype="float32", blocksize=period)
            self._stream.start()
        except Exception as exc:
            self.close()
            raise OSError(f"sounddevice : sortie {rate} Hz/{channels}ch indisponible — {exc}") from exc

    def write(self, block: np.ndarray) -> int:
        data = np.ascontiguousarray(block, dtype=np.float32)
        self._stream.write(data)          # bloquant = pacing temps réel
        return data.shape[0]

    def close(self):
        s = getattr(self, "_stream", None)
        self._stream = None               # idempotent
        if s is not None:
            try: s.stop()
            except Exception: pass
            try: s.close()
            except Exception: pass
