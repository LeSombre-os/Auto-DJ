"""Sélection du backend audio par plateforme — aucun device réel."""
import sys, types

import numpy as np, pytest

from app.engine import Engine
from app.output_sd import SDOut

def _mod(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items(): setattr(m, k, v)
    return m

def _patch(monkeypatch, platform, alsa_ok=True, sd_ok=True):
    log = []
    def cls(tag, ok):
        class Out:
            def __init__(self, rate):
                log.append(tag)
                if not ok: raise OSError(tag + " hs")
        return Out
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setitem(sys.modules, "app.output_alsa", _mod("app.output_alsa", ALSAOut=cls("alsa", alsa_ok)))
    monkeypatch.setitem(sys.modules, "app.output_sd", _mod("app.output_sd", SDOut=cls("sd", sd_ok)))
    return log

@pytest.mark.parametrize("plat,alsa_ok,expected", [
    ("linux", True, ["alsa"]), ("linux", False, ["alsa", "sd"]),
    ("win32", True, ["sd"]), ("darwin", True, ["sd"])])
def test_backend_par_plateforme(monkeypatch, plat, alsa_ok, expected):
    log = _patch(monkeypatch, plat, alsa_ok)
    Engine._default_out(48000)
    assert log == expected

def test_linux_sans_aucun_backend(monkeypatch):
    _patch(monkeypatch, "linux", alsa_ok=False, sd_ok=False)
    with pytest.raises(OSError, match="ALSA.*sounddevice"):
        Engine._default_out(48000)

def test_sdout_sans_sounddevice(monkeypatch):
    monkeypatch.setitem(sys.modules, "sounddevice", None)
    with pytest.raises(OSError, match="pip install sounddevice"):
        SDOut(48000)

def test_sdout_write_et_close(monkeypatch):
    class Stream:
        def __init__(self, **kw): self.kw, self.data, self.stops, self.closes = kw, [], 0, 0
        def start(self): pass
        def write(self, b): self.data.append(b.copy()); return False
        def stop(self): self.stops += 1
        def close(self): self.closes += 1
    monkeypatch.setitem(sys.modules, "sounddevice", _mod("sounddevice", OutputStream=Stream))
    out = SDOut(48000, 2, 1024, 8192)
    s = out._stream
    assert out.write(np.zeros((1024, 2), np.float32)) == 1024
    assert s.kw == {"samplerate": 48000, "channels": 2, "dtype": "float32", "blocksize": 1024}
    assert s.data[0].shape == (1024, 2)
    out.close(); out.close()
    assert out._stream is None and s.stops == 1 and s.closes == 1
