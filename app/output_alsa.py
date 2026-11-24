"""Sortie audio native ALSA via ctypes (libasound.so.2) — backend de lecture de Seamless.

Un flux stéréo FLOAT_LE, pacing temps réel par writei bloquant. Aucune dépendance
PortAudio/sounddevice (libportaudio2 absente de la machine ; libasound.so.2 présente).

Interface : open() -> write(block: np.ndarray float32 (frames, ch)) -> close().
Windows un jour : autre module avec la même interface write(), rien d'autre à changer.
"""
import ctypes
import ctypes.util

import numpy as np

SND_PCM_STREAM_PLAYBACK = 0
SND_PCM_ACCESS_RW_INTERLEAVED = 3
SND_PCM_FORMAT_FLOAT_LE = 14


def _alsa_lib() -> ctypes.CDLL:
    path = ctypes.util.find_library("asound") or "libasound.so.2"
    return ctypes.CDLL(path)


_alsa = _alsa_lib()
_alsa.snd_pcm_open.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
_alsa.snd_pcm_hw_params_malloc.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
_alsa.snd_pcm_hw_params_set_rate_near.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                                  ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_int)]
_alsa.snd_pcm_hw_params_set_period_size_near.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                                         ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int)]
_alsa.snd_pcm_hw_params_set_buffer_size_near.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                                         ctypes.POINTER(ctypes.c_ulong)]
_alsa.snd_pcm_writei.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
_alsa.snd_pcm_writei.restype = ctypes.c_long  # snd_pcm_sframes_t (long), sinon int tronque


class ALSAOut:
    """Sortie PCM temps réel. write() bloque avec la horloge de la carte (pacing gratuit)."""

    def __init__(self, rate: int = 48000, channels: int = 2, period: int = 1024, buffer_frames: int = 8192):
        self.rate = rate
        self.channels = channels
        self.period = period
        self._buf = buffer_frames
        self.h = ctypes.c_void_p()
        # ponytail: périodes fixes (1024/8192) suffisantes pour du DJ ; affiner si latence ressentie
        if _alsa.snd_pcm_open(ctypes.byref(self.h), b"default", SND_PCM_STREAM_PLAYBACK, 0) != 0:
            raise OSError("ALSA: ouverture device 'default' impossible")
        p = ctypes.c_void_p()
        _alsa.snd_pcm_hw_params_malloc(ctypes.byref(p))
        try:
            self._setup(p, rate, channels, period, buffer_frames)
        finally:
            _alsa.snd_pcm_hw_params_free(p)
        _alsa.snd_pcm_prepare(self.h)

    def _setup(self, p, rate, channels, period, buffer_frames):
        h = self.h
        rate_u = ctypes.c_uint(rate)
        dir_ = ctypes.c_int(0)
        period_u = ctypes.c_ulong(period)
        buf_u = ctypes.c_ulong(buffer_frames)
        _alsa.snd_pcm_hw_params_any(h, p)
        _alsa.snd_pcm_hw_params_set_access(h, p, SND_PCM_ACCESS_RW_INTERLEAVED)
        _alsa.snd_pcm_hw_params_set_format(h, p, SND_PCM_FORMAT_FLOAT_LE)
        _alsa.snd_pcm_hw_params_set_channels(h, p, channels)
        _alsa.snd_pcm_hw_params_set_rate_near(h, p, ctypes.byref(rate_u), ctypes.byref(dir_))
        self.rate = rate_u.value  # taux réellement accepté par le device
        _alsa.snd_pcm_hw_params_set_period_size_near(h, p, ctypes.byref(period_u), ctypes.byref(dir_))
        self.period = period_u.value
        _alsa.snd_pcm_hw_params_set_buffer_size_near(h, p, ctypes.byref(buf_u))
        self._buf = buf_u.value
        if _alsa.snd_pcm_hw_params(h, p) != 0:
            raise OSError(f"ALSA: params {self.rate} Hz/{channels}ch FLOAT_LE incompatibles avec ce device")

    def write(self, block: np.ndarray) -> int:
        """Écrit un bloc (frames, ch) float32 interleave. Bloque selon la conso du device.

        Lève OSError si le device est perdu (recover impossible) : le moteur pose
        `_out_dead`, met en pause et publie `audio_error`. Avant, un `break` discret
        laissait le moteur croire qu'il jouait (position qui avance, aucun son).
        """
        data = np.ascontiguousarray(block, dtype=np.float32)
        frames = data.shape[0]
        pos = 0
        while pos < frames:
            chunk = data[pos:]
            w = _alsa.snd_pcm_writei(self.h, chunk.ctypes.data_as(ctypes.c_void_p), chunk.shape[0])
            if w > 0:
                pos += w
                continue
            if w == 0:                    # aucune frame acceptée : bouclerait à vide à 100 % CPU
                raise OSError("ALSA: writei n'accepte aucune frame (device bloqué)")
            if _alsa.snd_pcm_recover(self.h, int(w), 1) < 0:   # -EPIPE/underflow irrécupérable
                raise OSError(f"ALSA: device perdu (writei={int(w)}, recover impossible)")
        return frames

    def close(self):
        if self.h.value:
            _alsa.snd_pcm_drain(self.h)
            _alsa.snd_pcm_close(self.h)
            self.h = ctypes.c_void_p()
