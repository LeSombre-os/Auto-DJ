"""Tests headless du moteur — sortie factice (DummyOut), WAV synthétiques, ffmpeg réel.

Sinusoïdes aux fréquences basses (40 / 30 Hz) et niveau connu : détection cue ±0.15 s,
maths de fade et ratios duck vérifiables par mesures (RMS, DFT manuelle).
"""
import json, sys, time, wave
from collections import OrderedDict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pytest

import app.engine as eng
from app.engine import Engine, analyze, waveform, xf_gains, SKIP_MIN, SKIP_MAX

RATE = 48000
F_A, F_B = 40.0, 30.0


class DummyOut:
    """Sortie factice : write() ne bloque pas (rendu accéléré), pacing optionnel."""
    def __init__(self, pace=0.0):
        self.rate = RATE
        self.log = []              # int -> bloc de zéros (frames) ; ndarray -> audio
        self.total = 0
        self.pace = pace

    def write(self, b):
        self.total += b.shape[0]
        self.log.append(b if b.any() else b.shape[0])
        if self.pace:
            time.sleep(self.pace)


def engine_with(tmp_path, pace=0.003):
    dummy = DummyOut(pace=pace)
    evs = []
    e = Engine(lambda r: dummy, data_dir=str(tmp_path))
    e.events = lambda n, d: evs.append((n, dict(d)))
    return e, dummy, evs


def names(evs, n):
    return [d for k, d in evs if k == n]


def wait_for(cond, timeout=12.0, step=0.02):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if cond():
                return True
        except Exception:
            pass
        time.sleep(step)
    return False


def make_wav(path, f0, amp=0.5, off=2.0, dur=12.0, sr=RATE):
    n = int(dur * sr)
    s = np.zeros(n, np.float32)
    n0, n1 = int(off * sr), int((dur - off) * sr)
    s[n0:n1] = (amp * np.sin(2 * np.pi * f0 * np.arange(n0, n1) / sr)).astype(np.float32)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes((s * 32000).astype(np.int16).tobytes())


def stream(dummy):
    parts = [x if isinstance(x, np.ndarray) else np.zeros((x, 2), np.float32) for x in dummy.log]
    return np.concatenate(parts) if parts else np.zeros((0, 2), np.float32)


def dft_amp(x, f, sr=RATE):
    ph = np.arange(len(x))
    c, s = np.cos(2 * np.pi * f * ph / sr), np.sin(2 * np.pi * f * ph / sr)
    return 2 * float(np.hypot(float((x * c).sum()), float((x * s).sum()))) / len(x)


def audio_total(dummy):
    return sum(x.shape[0] for x in dummy.log if isinstance(x, np.ndarray))


def longest_zero_run(x):
    z = np.abs(x) <= 0.005
    if not z.any():
        return 0
    d = np.diff(z.astype(np.int8))
    starts = list(np.nonzero(d == 1)[0] + 1)
    ends = list(np.nonzero(d == -1)[0] + 1)
    if z[0]: starts = [0] + starts
    if z[-1]: ends = ends + [len(z)]
    return int(max(e - s for s, e in zip(starts, ends)))


def test_analyze_high_frequency_not_truncated(tmp_path):
    """Enveloppe décodée à 8 kHz : un morceau dont l'énergie est au-dessus de ~500 Hz
    ne doit pas être vu comme quasi silencieux. À 1 kHz, cue_out tombait à 0,01 s ->
    le morceau « finissait » instantanément (enchaînement en rafale, UI bloquée au bout)."""
    p = tmp_path / "hi.wav"
    make_wav(p, f0=660, amp=0.5, off=0.0, dur=4.0)
    a = analyze(str(p), data_dir=str(tmp_path))
    assert a["cue_in"] < 0.1, a
    assert a["cue_out"] > 3.5, a


def test_analyze_cues(tmp_path):
    a = tmp_path / "a.wav"; make_wav(a, F_A)
    r = analyze(str(a), data_dir=str(tmp_path))
    assert abs(r["cue_in"] - 2.0) < 0.15
    assert abs(r["cue_out"] - 10.0) < 0.15
    assert abs(r["duration"] - 12.0) < 0.2
    assert 1150 <= len(r["envelope"]) <= 1260
    assert r["title"] == "a"
    persist = json.load(open(tmp_path / "cues.json"))[str(a)]
    assert abs(persist["cue_in"] - 2.0) < 0.15 and abs(persist["cue_out"] - 10.0) < 0.15
    bad = tmp_path / "bad.txt"; bad.write_bytes(b"garbage" * 512)
    b = analyze(str(bad), data_dir=str(tmp_path))
    assert b["cue_in"] == 0.0 and b["cue_out"] == b["duration"]   # fallback, pas d'exception


def test_waveform(tmp_path):
    a = tmp_path / "a.wav"; make_wav(a, F_A)
    w = waveform(str(a), 100, data_dir=str(tmp_path))
    assert len(w) == 100 and abs(max(w) - 1.0) < 1e-6
    assert w[5] < 0.05 and w[60] > 0.4


def test_play_starts_at_cue(tmp_path):
    a = tmp_path / "a.wav"; make_wav(a, F_A)
    e, dummy, evs = engine_with(tmp_path)
    e.set_queue([str(a)]); e.play_toggle()
    assert wait_for(lambda: len(names(evs, "track_started")) == 1), evs
    assert wait_for(lambda: audio_total(dummy) >= 2 * RATE, 10.0)   # 2 s de son rendu
    e.play_toggle()                                   # pause : fondu court puis position figée
    time.sleep(0.35)                                  # laisse la rampe de sortie (~0,22 s) finir
    pf = e.status()["pos"]; time.sleep(0.2)
    assert abs(e.status()["pos"] - pf) < 1e-4         # position figée pendant la pause
    s = stream(dummy)
    nz = np.nonzero(np.abs(s[:, 0]) > 0.005)[0]
    assert len(nz) > RATE
    first = int(nz[0])
    rms = float(np.sqrt((s[first:first + RATE, 0] ** 2).mean()))
    assert 0.22 < rms < 0.42                          # sinusoïde 0.5 crête -> RMS ~0.35
    st = e.status()
    assert st["path"] == str(a) and st["pos"] > 1.0   # pos démarre vers cue_in, pas 0


def test_crossfade_zero_silence(tmp_path):
    a = tmp_path / "a.wav"; make_wav(a, F_A)
    b = tmp_path / "b.wav"; make_wav(b, F_B)
    e, dummy, evs = engine_with(tmp_path, pace=0.002)
    e.set_queue([str(a), str(b)]); e.play_toggle()
    assert wait_for(lambda: len(names(evs, "ended_fade")) >= 1), evs
    assert wait_for(lambda: any(k == "queue_empty" for k, _ in evs), 15.0)
    s = stream(dummy)
    nz = np.nonzero(np.abs(s[:, 0]) > 0.005)[0]
    assert len(nz) > 4 * RATE
    first, last = int(nz[0]), int(nz[-1])
    core = s[first:last + 1, 0]
    assert longest_zero_run(core) < 480               # aucun trou > 10 ms au milieu de la session
    assert float(np.abs(np.diff(core)).max()) < 0.02  # aucune discontinuité (pas de clic)
    pre = core[RATE // 2: RATE // 2 + 2 * RATE]       # avant le fade : A seule
    assert dft_amp(pre, F_A) > 0.25 and dft_amp(pre, F_B) < 0.08
    post = core[-2 * RATE:]                           # fin : B seule (jamais le silence de fin d'A)
    assert dft_amp(post, F_B) > 0.25 and dft_amp(post, F_A) < 0.08
    win = RATE // 10
    lo, hi = first + 4 * RATE, last - 4 * RATE
    mins = min(float(np.sqrt((s[i:i + win, 0] ** 2).mean())) for i in range(lo, hi, win))
    assert 0.08 < mins < 0.34                         # creux du crossfade : les deux gains partiellement


def test_crossfade_curves_maths():
    """Courbes de fondu : equal-power (puissances), linéaire (amplitudes), smooth monotone."""
    pc = np.linspace(0.0, 1.0, 101, dtype=np.float32)
    out, inn = xf_gains("equal_power", pc)
    assert np.allclose(out ** 2 + inn ** 2, 1.0, atol=1e-5)      # out²+in² = 1 partout
    out, inn = xf_gains("linear", pc)
    assert np.allclose(out + inn, 1.0, atol=1e-6)                # out+in = 1 partout
    out, inn = xf_gains("smooth", pc)
    assert np.all(np.diff(inn) >= 0) and np.all(np.diff(out) <= 0)   # monotone, sans saut
    assert abs(float(inn[50]) - 0.5) < 1e-6 and abs(float(inn[10]) - 0.028) < 1e-3
    assert np.allclose(xf_gains("cut", pc), xf_gains("linear", pc))  # cut = gains linéaires


def test_cut_curve_short_fade(tmp_path):
    """cut : la durée du fondu naturel tombe à ≤ 0,4 s (mesurée sur le fade réel)."""
    a = tmp_path / "a.wav"; make_wav(a, F_A, dur=8.0)
    b = tmp_path / "b.wav"; make_wav(b, F_B, dur=8.0)
    seen, holder = [], {}

    class Spy(DummyOut):
        def write(self, blk):
            f = holder.get("e") and holder["e"]._fade
            if f is not None and not seen:
                seen.append(f[1])              # L du premier fondu lancé
            super().write(blk)

    dummy = Spy(pace=0.002)                # pacing : le décodeur doit être prêt pour le fade
    e = Engine(lambda r: dummy, data_dir=str(tmp_path)); holder["e"] = e
    evs = []; e.events = lambda n, d: evs.append((n, dict(d)))
    e.set_crossfade(16.0)
    e.set_crossfade_curve("cut")
    assert e.status()["crossfade_curve"] == "cut" and e.status()["crossfade"] == 16.0
    e.set_crossfade_curve("wobble")                     # inconnu -> ignoré
    assert e.status()["crossfade_curve"] == "cut"
    e.set_queue([str(a), str(b)]); e.play_toggle()
    assert wait_for(lambda: bool(seen), 15.0), evs
    assert 0.0 < seen[0] <= 0.4
    e.set_crossfade_curve("equal_power")
    assert e._xf_len() == 16.0                          # hors cut : le réglage reprend


def test_status_incoming_during_fade(tmp_path):
    """Crossfade naturel : status() expose la piste entrante (path/pos/dur/cues) pour que
    l'UI n'affiche pas la barre finie du sortant ; None hors fade (clé toujours présente)."""
    a = tmp_path / "a.wav"; make_wav(a, F_A, dur=8.0)
    b = tmp_path / "b.wav"; make_wav(b, F_B, dur=8.0)
    e, dummy, evs = engine_with(tmp_path, pace=0.01)     # ~2x temps réel : fade de ~1 s réel
    e.set_crossfade(2.0)
    st = e.status()
    assert "incoming" in st and st["incoming"] is None    # repos : clé présente, valeur None
    e.set_queue([str(a), str(b)]); e.play_toggle()
    assert wait_for(lambda: e.status()["incoming"] is not None, 15.0), evs
    st = e.status(); inc = st["incoming"]
    assert st["fading"] is True and st["path"] == str(a)  # sortant encore annoncé
    assert st["pos"] > st["cue_out"] - 2.1                # ... et déjà en fin de piste
    assert inc["path"] == str(b)
    assert set(inc) == {"path", "pos", "dur", "cue_in", "cue_out"}
    assert isinstance(inc["pos"], float) and isinstance(inc["dur"], float)
    assert 0.0 < inc["pos"] < inc["dur"] and inc["dur"] > 0.0
    assert abs(inc["cue_in"] - 2.0) < 0.2 and inc["cue_in"] < inc["cue_out"] <= inc["dur"] + 0.2
    assert st["pos"] > inc["pos"]                         # entrant en tête, sortant en fin
    assert wait_for(lambda: e.status()["path"] == str(b), 15.0), evs
    assert e.status()["incoming"] is None                 # fade fini : plus d'entrante


def test_queue_replace_skip_position(tmp_path):
    a = tmp_path / "a.wav"; make_wav(a, F_A)
    b = tmp_path / "b.wav"; make_wav(b, F_B)
    e, dummy, evs = engine_with(tmp_path, pace=0.002)
    e.set_queue([str(a)]); e.play_toggle()
    assert wait_for(lambda: len(names(evs, "track_started")) == 1), evs
    p0 = e.status()["pos"]; assert p0 > 0.0
    assert wait_for(lambda: e.status()["pos"] > p0 + 0.25, 6.0)     # pos avance
    e.set_queue([str(b)])
    assert wait_for(lambda: e.status()["next"] == [str(b)], 6.0)
    e.skip()
    assert wait_for(lambda: any(d.get("to") == str(b) for d in names(evs, "ended_fade")), 10.0), evs
    assert wait_for(lambda: any(d.get("path") == str(b) for d in names(evs, "track_started")), 10.0), evs
    pos_b = e.status()["pos"]
    assert pos_b < 4.6                                  # repart de cue_in de B (~2.0), pas en mi-crossfade
    e.skip()                                            # file vide plus après ? non : B restante
    assert wait_for(lambda: e.status()["path"] is None, 10.0), evs
    assert any(k == "queue_empty" for k, _ in evs)
    assert not e.status()["playing"]                    # vidage : repos (pas d'auto-démarrage)
    n_started = len(names(evs, "track_started"))
    e.set_queue([str(b)])                               # file repoussée sur moteur au repos
    assert wait_for(lambda: e.status()["next"] == [str(b)], 6.0)
    time.sleep(0.3)
    assert not e.status()["playing"] and e.status()["path"] is None
    assert len(names(evs, "track_started")) == n_started   # pos figée : rien n'a démarré seul
    e.play_toggle()                                     # PLAY volontaire -> démarre à cue_in
    assert wait_for(lambda: any(d.get("path") == str(b) for d in names(evs, "track_started")), 10.0), evs
    assert e.status()["pos"] < 4.6                      # démarre à cue_in de B (~2.0)


def test_skip_fondu_audible_pas_coupure(tmp_path):
    """Skip / audition : fondu borné 0,6–2 s qui suit le crossfade réglé, pas 0,3 s."""
    e, dummy, evs = engine_with(tmp_path, pace=0.002)
    e.set_crossfade(8.0)
    assert abs(e._skip_len() - 8.0 / 6.0) < 1e-9
    e.set_crossfade(2.0)
    assert e._skip_len() == SKIP_MIN                 # borne basse (2/6 -> 0,6)
    e.set_crossfade(16.0)
    assert e._skip_len() == SKIP_MAX                 # borne haute
    e.set_crossfade_curve("cut")
    assert e._skip_len() == eng.CUT                  # coupe franche voulue : on la garde

    a = tmp_path / "a.wav"; make_wav(a, F_A)
    b = tmp_path / "b.wav"; make_wav(b, F_B)
    e.set_crossfade(8.0); e.set_crossfade_curve("equal_power")
    e.set_queue([str(a), str(b)]); e.play_toggle()
    assert wait_for(lambda: len(names(evs, "track_started")) == 1), evs
    e.skip()
    assert wait_for(lambda: e._fade is not None, 5.0), evs
    L = e._fade[1]
    assert SKIP_MIN <= L <= SKIP_MAX, L
    assert wait_for(lambda: any(d.get("to") == str(b) for d in names(evs, "ended_fade")), 10.0), evs


def test_duck_ratio(tmp_path):
    a = tmp_path / "a.wav"; make_wav(a, F_A)
    e, dummy, evs = engine_with(tmp_path, pace=0.002)
    e.set_queue([str(a)]); e.play_toggle()
    assert wait_for(lambda: len(names(evs, "track_started")) == 1), evs
    assert wait_for(lambda: audio_total(dummy) >= RATE, 12.0)   # 1 s de son avant d'activer le duck
    e.set_duck(True)
    assert wait_for(lambda: e.status()["ducking"], 5.0)
    time.sleep(0.4)                                     # laisse la rampe 100 ms + marge passer
    s = stream(dummy)
    nz = np.nonzero(np.abs(s[:, 0]) > 0.005)[0]
    first, last = int(nz[0]), int(nz[-1])
    pre = float(np.sqrt((s[first:first + RATE, 0] ** 2).mean()))
    post = float(np.sqrt((s[max(first, last - RATE):last, 0] ** 2).mean()))
    assert pre > 0.2
    ratio = post / pre
    assert 0.19 < ratio < 0.31, f"ratio={ratio} pre={pre} post={post}"


def test_corrupt_skips_to_next(tmp_path):
    a = tmp_path / "ok.wav"; make_wav(a, F_A)
    bad = tmp_path / "bad.txt"; bad.write_bytes(b"this is not audio" * 64)
    e, dummy, evs = engine_with(tmp_path, pace=0.002)
    e.set_queue([str(bad), str(a)]); e.play_toggle()
    assert wait_for(lambda: len(names(evs, "corrupt")) >= 1, 12.0), evs
    assert names(evs, "corrupt")[0]["path"] == str(bad)
    assert wait_for(lambda: any(d.get("path") == str(a) for d in names(evs, "track_started")), 12.0), evs
    ic = next(i for i, (k, _) in enumerate(evs) if k == "corrupt")
    it = next(i for i, (k, d) in enumerate(evs) if k == "track_started" and d["path"] == str(a))
    assert ic < it
    assert e.status()["path"] == str(a)                 # le moteur a continué, pas de crash


def test_restore_position(tmp_path):
    a = tmp_path / "a.wav"; make_wav(a, F_A)
    e, dummy, evs = engine_with(tmp_path)
    e.set_queue([str(a)]); e.set_position(6.0); e.play_toggle()
    assert wait_for(lambda: len(names(evs, "track_started")) == 1), evs
    assert abs(e.status()["pos"] - 6.0) < 0.5           # restitution à la position sauvée (fast seek)


def test_preview_alone(tmp_path):
    """Audition : le path d'audition joue SEUL (file suspendue), fin naturelle -> repos."""
    a = tmp_path / "a.wav"; make_wav(a, F_A)
    b = tmp_path / "b.wav"; make_wav(b, F_B, dur=6.0)
    e, dummy, evs = engine_with(tmp_path, pace=0.002)
    e.set_queue([str(a)]); e.play_toggle()
    assert wait_for(lambda: len(names(evs, "track_started")) == 1), evs
    e.preview(str(b))                                   # audition pendant la lecture de a
    assert wait_for(lambda: e.status()["next"] == [] and e.status()["preview"], 5.0), evs
    assert wait_for(lambda: any(d.get("path") == str(b) for d in names(evs, "track_started")), 10.0), evs
    assert e.status()["path"] == str(b)
    assert wait_for(lambda: e.status()["pos"] > 2.2, 8.0)      # pos avance depuis cue_in (~2.0)
    assert wait_for(lambda: any(k == "queue_empty" for k, _ in evs), 15.0), evs
    s = stream(dummy)                                   # flux final : l'audition a joué jusqu'à cue_out
    nz = np.nonzero(np.abs(s[:, 0]) > 0.005)[0]
    tail = s[int(nz[-1]) - RATE:int(nz[-1]), 0]         # dernière seconde rendue : l'audition seule
    assert dft_amp(tail, F_B) > 0.25 and dft_amp(tail, F_A) < 0.08
    assert wait_for(lambda: not e.status()["preview"], 5.0)
    assert not e.status()["playing"]                    # fin d'audition : repos (aucune surprise)


def test_requeue_start_after_audition(tmp_path):
    """requeue(start=True) moteur à l'arrêt (post-audition) : la première démarre comme un play."""
    a = tmp_path / "a.wav"; make_wav(a, F_A, dur=6.0)
    b = tmp_path / "b.wav"; make_wav(b, F_B, dur=6.0)
    e, dummy, evs = engine_with(tmp_path, pace=0.002)
    e.preview(str(a))                                   # audition au repos -> a joue puis finit
    assert wait_for(lambda: any(d.get("path") == str(a) for d in names(evs, "track_started")), 10.0), evs
    assert wait_for(lambda: any(k == "queue_empty" for k, _ in evs), 15.0), evs
    assert wait_for(lambda: not e.status()["playing"] and not e.status()["preview"], 5.0)
    e.requeue([str(b)], start=True)                     # file remise + démarrage immédiat
    assert wait_for(lambda: any(d.get("path") == str(b) for d in names(evs, "track_started")), 10.0), evs
    st = e.status()
    assert st["path"] == str(b) and st["pos"] < 4.6     # démarre à cue_in de b (~2.0)
    assert st["next"] == [] and not st["preview"]


def test_preview_stop_before_audible_keeps_paused_track(tmp_path):
    """Piste en pause + audition coupée avant d'être audible : l'audition est abandonnée
    (jamais promue), la piste en pause le reste, la file restaurée ne démarre pas seule."""
    a = tmp_path / "a.wav"; make_wav(a, F_A)
    b = tmp_path / "b.wav"; make_wav(b, F_B)
    c = tmp_path / "c.wav"; make_wav(c, 50.0)
    e, dummy, evs = engine_with(tmp_path, pace=0.002)
    e.set_queue([str(a), str(b)]); e.play_toggle()
    assert wait_for(lambda: len(names(evs, "track_started")) == 1), evs
    e.play_toggle()                                     # pause : A reste la piste courante
    assert wait_for(lambda: not e.status()["playing"], 5.0)
    e.preview(str(c))                                   # audition C
    e.requeue([str(a), str(b)], start=False, cut=True)  # Échap immédiat (avant le fade)
    seen = []
    t0 = time.time()
    while time.time() - t0 < 1.0:                       # > 3 x SKIPFADE
        seen.append(e.status()["path"]); time.sleep(0.02)
    st = e.status()
    assert str(c) not in seen                           # l'audition n'est jamais promue
    assert st["path"] == str(a) and st["playing"] is False
    assert st["next"] == [str(a), str(b)]               # file restaurée, rien n'a démarré
    assert st["preview"] is False


def test_preview_stop_before_audible_resumes_queue(tmp_path):
    """Piste en lecture + audition coupée avant d'être audible : la file reprend
    (A continue ou B enchaîne), jamais l'audition."""
    a = tmp_path / "a.wav"; make_wav(a, F_A)
    b = tmp_path / "b.wav"; make_wav(b, F_B)
    c = tmp_path / "c.wav"; make_wav(c, 50.0)
    e, dummy, evs = engine_with(tmp_path, pace=0.002)
    e.set_queue([str(a), str(b)]); e.play_toggle()
    assert wait_for(lambda: len(names(evs, "track_started")) == 1), evs
    e.preview(str(c))
    e.requeue([str(a), str(b)], start=True, cut=True)
    seen = []
    t0 = time.time()
    while time.time() - t0 < 1.0:                       # > 3 x SKIPFADE
        seen.append(e.status()["path"]); time.sleep(0.02)
    assert str(c) not in seen                           # l'audition n'est jamais promue
    assert wait_for(lambda: e.status()["playing"] and not e.status()["fading"], 5.0), evs
    st = e.status()
    assert st["path"] != str(c) and st["path"] is not None
    assert set(st["next"]) <= {str(a), str(b)}          # file cohérente
    assert st["preview"] is False


def test_pause_resume_stop_no_click(tmp_path):
    """Transitions hors crossfade : pause -> rampe de sortie puis zéros réels,
    reprise -> rampe d'entrée à la même position, stop -> rampe puis silence."""
    a = tmp_path / "a.wav"; make_wav(a, F_A, dur=30.0)
    e, dummy, evs = engine_with(tmp_path, pace=0.002)
    e.set_queue([str(a)]); e.play_toggle()
    assert wait_for(lambda: audio_total(dummy) >= 2 * RATE, 10.0)

    # --- pause : le rendu continue le temps de la rampe, atterrit sur 0, puis silence
    n0 = len(dummy.log)
    e.play_toggle()
    assert wait_for(lambda: not e.status()["playing"], 5.0)
    pos_pause = e.status()["pos"]
    assert wait_for(lambda: len(dummy.log) >= n0 + 40, 5.0)
    post = dummy.log[n0:]
    ramp = [b for b in post if isinstance(b, np.ndarray)]
    assert 3 <= len(ramp) <= 40 and len(post) - len(ramp) >= 10   # rampe ~0,22 s puis zéros
    assert float(np.abs(ramp[-1][-1]).max()) < 1e-6               # la rampe finit pile à zéro
    assert float(np.abs(np.diff(np.concatenate(ramp)[:, 0])).max()) < 0.02   # aucun saut

    # --- reprise : fondu d'entrée, position continue (pas de retour à cue_in)
    n1 = len(dummy.log)
    e.play_toggle()
    assert wait_for(lambda: e.status()["playing"], 5.0)
    assert e.status()["pos"] >= pos_pause
    assert wait_for(lambda: sum(isinstance(b, np.ndarray) for b in dummy.log[n1:]) >= 20, 5.0)
    after = [b for b in dummy.log[n1:] if isinstance(b, np.ndarray)]
    r0 = float(np.sqrt((after[0][:, 0] ** 2).mean()))
    r1 = float(np.sqrt((after[-1][:, 0] ** 2).mean()))
    assert r0 < 0.5 * r1                                          # entrée en fondu, pas à plein gain

    # --- stop : skip sur file vidée -> rampe de sortie puis silence, sans exception
    e.set_queue([])
    assert wait_for(lambda: e.status()["next"] == [], 5.0)
    n2 = len(dummy.log)
    e.skip()
    assert wait_for(lambda: e.status()["path"] is None and not e.status()["playing"], 8.0)
    time.sleep(0.4)                                               # laisse la rampe finir
    tail = [b for b in dummy.log[n2:] if isinstance(b, np.ndarray)]
    assert tail and float(np.abs(tail[-1][-1]).max()) < 1e-6
    assert any(not isinstance(b, np.ndarray) for b in dummy.log[n2:])
    assert any(k == "queue_empty" for k, _ in evs)


def test_device_mort_ne_tue_pas_le_thread(tmp_path):
    """write() qui lève : event audio_error, moteur en pause, thread toujours vivant."""
    class FlipOut:
        rate = RATE
        dead = False
        def write(self, b):
            if self.dead: raise OSError("device mort")
        def close(self): pass
    out = FlipOut()
    evs = []
    e = Engine(lambda r: out, data_dir=str(tmp_path))
    e.events = lambda n, d: evs.append((n, dict(d)))
    out.dead = True
    assert wait_for(lambda: any(k == "audio_error" for k, _ in evs), 5.0)
    assert e._out_dead and not e.status()["playing"] and e._th.is_alive()


def test_file_epuisee_s_arrete_proprement(tmp_path):
    """Symptôme signalé : en fin de file l'UI restait au bout et rien ne se passait.
    Après le dernier morceau le moteur doit s'arrêter (playing False, path None, file
    vide) — et chaque morceau doit être joué en entier (pas de rafale cue_out ~0)."""
    e, dummy, evs = engine_with(tmp_path)
    a, b = tmp_path / "a.wav", tmp_path / "b.wav"
    make_wav(a, F_A, off=0.5, dur=3.0)
    make_wav(b, F_B, off=0.5, dur=3.0)
    e.set_crossfade(2)
    e.set_volume(0.0)
    e.set_queue([str(a), str(b)])
    e.play_toggle()
    assert wait_for(lambda: e.status()["path"] == str(b), 20.0), e.status()
    assert e.status()["pos"] < 3.0
    assert wait_for(lambda: not e.status()["playing"], 20.0), e.status()
    st = e.status()
    assert st["path"] is None and st["next"] == [], st
    assert any(k == "queue_empty" for k, _ in evs)


# ---------------- sortie ALSA (device factice, aucun matériel) ----------------

class FakeAlsa:
    """Faux module `_alsa` : writei/recover scriptés, appels tracés."""
    def __init__(self, writes, recover=0):
        self.writes, self.recover_ret, self.calls = list(writes), recover, []

    def snd_pcm_writei(self, h, ptr, frames):
        self.calls.append(("write", frames))
        if len(self.calls) > 64:                  # sans garde : write() bouclerait à l'infini
            raise AssertionError("boucle infinie dans ALSAOut.write")
        return self.writes.pop(0) if len(self.writes) > 1 else self.writes[0]

    def snd_pcm_recover(self, h, err, silent):
        self.calls.append(("recover", err))
        return self.recover_ret


def fake_alsa_out(monkeypatch, fake):
    from app import output_alsa as oa
    out = object.__new__(oa.ALSAOut)              # pas d'ouverture de device réel
    out.h, out.rate = 1, RATE
    monkeypatch.setattr(oa, "_alsa", fake)
    return out


def test_alsa_write_device_mort_leve_oserror(monkeypatch):
    """`cannot recovery from underrun, prepare failed: Broken pipe` (11x dans data/server.log) :
    write() retournait `frames` -> le moteur croyait jouer (position qui avance, aucun son,
    aucun audio_error, UI muette). Il doit lever pour déclencher _out_dead + pause."""
    dead = FakeAlsa([-32], recover=-5)            # -EPIPE, recover impossible
    out = fake_alsa_out(monkeypatch, dead)
    with pytest.raises(OSError):
        out.write(np.zeros((1024, 2), np.float32))
    assert ("recover", -32) in dead.calls
    # underflow récupérable : recover OK -> réessai et bloc entier rendu
    alive = FakeAlsa([-32, 1024], recover=0)
    out = fake_alsa_out(monkeypatch, alive)
    assert out.write(np.zeros((1024, 2), np.float32)) == 1024
    assert [c for c in alive.calls if c[0] == "recover"] == [("recover", -32)]


def test_alsa_write_zero_frame_ne_boucle_pas(monkeypatch):
    """writei == 0 : le while ne progressait pas -> thread mixeur figé à 100 % CPU,
    plus aucun event. Doit lever au lieu de tourner à vide."""
    out = fake_alsa_out(monkeypatch, FakeAlsa([0]))
    with pytest.raises(OSError):
        out.write(np.zeros((1024, 2), np.float32))


# ---------------- cache d'analyse borné ----------------

def test_ram_analyze_bornee_lru(tmp_path, monkeypatch):
    """_RAM non borné : ~65 Ko d'enveloppe par morceau (100 floats/s) -> ~60 Mo pour
    900 morceaux. LRU : borne dure, l'analyse reste correcte après éviction."""
    monkeypatch.setattr(eng, "_RAM_MAX", 2)
    monkeypatch.setattr(eng, "_RAM", OrderedDict())
    paths = []
    for i in range(3):
        p = tmp_path / f"lru{i}.wav"; make_wav(p, F_A, dur=4.0, off=0.5)
        paths.append(str(p)); eng.analyze(str(p), data_dir=str(tmp_path))
    assert list(eng._RAM) == paths[1:]            # la plus ancienne est sortie
    a = eng.analyze(paths[0], data_dir=str(tmp_path))     # recalcul transparent
    assert abs(a["cue_in"] - 0.5) < 0.15 and abs(a["cue_out"] - 3.5) < 0.15
    assert len(eng._RAM) == 2 and paths[0] in eng._RAM and paths[1] not in eng._RAM


# ---------------- mutations de file pendant un fade ----------------

def test_file_videe_pendant_fade_n_enchaine_pas(tmp_path):
    """File vidée alors que la suivante est déjà préchargée : elle ne doit pas jouer
    quand même (l'UI affichait next=[] et le morceau retiré s'enchaînait)."""
    a = tmp_path / "a.wav"; make_wav(a, F_A, dur=12.0)
    b = tmp_path / "b.wav"; make_wav(b, F_B, dur=12.0)
    e, dummy, evs = engine_with(tmp_path, pace=0.002)
    e.set_crossfade(2.0)
    e.set_queue([str(a), str(b)]); e.play_toggle()
    assert wait_for(lambda: e._nxt is not None and e._nxt.path == str(b), 10.0), evs
    e.set_queue([])
    assert wait_for(lambda: e.status()["path"] is None and not e.status()["playing"], 10.0), e.status()
    assert not any(d.get("path") == str(b) for d in names(evs, "track_started"))
    assert e.status()["next"] == []


def test_suivante_reordonnee_ne_se_rejoue_pas(tmp_path):
    """Suivante préchargée déplacée dans la file (réordonnancement UI) : elle joue une
    fois et sort de la file — avant, elle restait dans `next` et repassait ensuite."""
    a = tmp_path / "a.wav"; make_wav(a, F_A, dur=12.0)
    b = tmp_path / "b.wav"; make_wav(b, F_B, dur=12.0)
    c = tmp_path / "c.wav"; make_wav(c, 50.0, dur=12.0)
    e, dummy, evs = engine_with(tmp_path, pace=0.002)
    e.set_crossfade(2.0)
    e.set_queue([str(a), str(b)]); e.play_toggle()
    assert wait_for(lambda: e._nxt is not None and e._nxt.path == str(b), 10.0), evs
    e.set_queue([str(c), str(b)])                 # b (préchargée) passe en 2e position
    assert wait_for(lambda: e.status()["path"] == str(b), 10.0), e.status()
    assert e.status()["next"] == [str(c)]         # jouée = retirée de la file, pas rejouée


def test_set_position_pendant_fade_replanifie(tmp_path):
    """Seek pendant un crossfade : le fade restait calé sur l'ancienne position et
    l'entrant, consommé en silence, était promu à son cue_out (morceau mangé)."""
    a = tmp_path / "a.wav"; make_wav(a, F_A, dur=12.0)
    b = tmp_path / "b.wav"; make_wav(b, F_B, dur=12.0)
    e, dummy, evs = engine_with(tmp_path, pace=0.002)
    e.set_crossfade(8.0)
    e.set_queue([str(a), str(b)]); e.play_toggle()
    assert wait_for(lambda: (i := e.status()["incoming"]) and i["pos"] > 2.5, 10.0), evs
    e.set_position(2.5)
    # fade annulé puis replanifié : B est promu et joue depuis cue_in (avant : l'entrant
    # consommé en silence arrivait à son cue_out et était jeté — morceau mangé)
    assert wait_for(lambda: e.status()["path"] == str(b), 10.0), e.status()
    assert wait_for(lambda: e.status()["pos"] > 3.0, 5.0), e.status()
    assert e.status()["path"] == str(b)


class FakeTrack:
    """Piste factice sans décodeur : état minimal consommé par `_render()`."""
    def __init__(self, path, cue_in, cue_out, start, consumed):
        from queue import Queue
        self.path = path
        self.a = {"cue_in": cue_in, "cue_out": cue_out, "duration": cue_out + 1.0}
        self.start = start
        self.consumed = consumed
        self.buf = np.zeros((0, 2), np.float32)
        self.q = Queue()
        self.eof = False
        self.reach = cue_out
        self.tot = int(max(cue_out - start, 0) * RATE)
        self.err = self.stop = False
        self.announced = True
        self.corrupt_fired = False
        self.promo = None
        self.killed = False

    def kill(self):
        self.killed = True


def fake_engine(tmp_path, cur, nxt, fade):
    """Moteur nu (sans device ni threads) armé pour un seul `_render()`."""
    e = object.__new__(Engine)
    e._rate = RATE
    e._t = 100.0
    e._cur, e._nxt = cur, nxt
    e._fade = fade
    e._skip = False
    e._queue = [nxt.path] if nxt is not None else []
    e._playing = True
    e._gate = 1.0
    e._g_from = e._g_to = 1.0
    e._g_t0 = 0.0
    e._volume = 1.0
    e._ducking = False
    e._xf = 8.0
    e._xf_curve = "equal_power"
    e._data_dir = str(tmp_path)
    e._ffmpeg = "ffmpeg"
    e._pending = None
    e._empty_fired = False
    e._preview_path = None
    e._out_dead = False
    evs = []
    e.events = lambda n, d: evs.append((n, dict(d)))
    return e, evs


def test_cue_out_flottant_ne_bloque_pas_le_fade(tmp_path):
    """pos = cue_out - 3e-14 (1.39 + 12 783 840/48 000) : `pos >= cue_out` échoue,
    `room * rate` tombe à 0 frame, et le fade naturel ne se termine JAMAIS — la
    musique s'arrête, le curseur reste en fin de piste (bug live 2026-11-18)."""
    cue_in, cue_out = 1.39, 267.72
    cur = FakeTrack("sortant.mp3", cue_in, cue_out, cue_in,
                    int((cue_out - cue_in) * RATE))
    assert cur.start + cur.consumed / RATE < cue_out      # 267.71999999999997 < 267.72
    nxt = FakeTrack("entrant.mp3", 2.0, 10.0, 2.0, 0)
    e, evs = fake_engine(tmp_path, cur, nxt,
                         (cue_out - 8.0, 8.0, False, True, "equal_power"))

    e._render()

    assert e._cur is nxt, "le fade doit promouvoir l'entrant au cue_out"
    assert e._nxt is None and e._fade is None
    assert cur.killed
    assert any(k == "ended_fade" for k, _ in evs)


def test_piste_corrompue_pendant_fade_ne_bloque_pas(tmp_path):
    """cur meurt pendant un fade naturel : sans purge de `_fade`, plus aucune frame
    à rendre et le fade ne se termine jamais (même gel que le bug cue_out)."""
    cur = FakeTrack("sortant.mp3", 1.0, 100.0, 1.0, int(10 * RATE))
    cur.eof, cur.tot, cur.reach = True, 0, 0.0            # _bad(cur) == True
    nxt = FakeTrack("entrant.mp3", 2.0, 10.0, 2.0, 0)
    nxt.buf = np.zeros((2048, 2), np.float32)             # prêt à jouer
    e, evs = fake_engine(tmp_path, cur, nxt, (5.0, 8.0, False, True, "equal_power"))

    e._render()
    assert cur.killed and e._fade is None and e._skip
    assert any(k == "corrupt" for k, _ in evs)

    e._render()                                           # le skip repart en fade kill
    assert e._fade is not None and e._fade[2] is True
