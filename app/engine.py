"""Moteur Auto-DJ : mixage stéréo float32, crossfade sur cues, ducking, zéro silence.

Un thread mixeur unique propriétaire du rendu ; l'horloge est le `out.write()` bloquant
(pacing device). Une piste ouverte = un thread lecteur + un sous-process ffmpeg
(f32le stéréo @ rate du device, blocs de 2048 frames dans une queue bornée).
Enveloppe/cues : RMS 100 Hz (décode mono 1 kHz), seuil -35 dBFS, cache RAM + data/cues.json.
"""
import json, os, shutil, subprocess, sys, threading, time, traceback
from collections import OrderedDict
from queue import Queue, Empty
from typing import TypedDict

import numpy as np

from .state import XF_CURVES

_FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
_FFPROBE = shutil.which("ffprobe") or "ffprobe"
_THR = 0.0178                      # -35 dBFS

MIX = 1024                         # frames par bloc rendu (= période ALSAOut par défaut)
DBLK = 2048                        # frames par bloc décodé dans la queue
QUEUE_MAX = 1024                   # bornage : max ~16 Mo par piste ; queue pleine -> pipe se bloque
RAMP = 0.10                        # rampe gain master (s)
ENTER = 0.22                       # fondu d'entrée piste (promo sans crossfade) (s)
TAIL = 0.03                        # rampe de fin de piste quand pas de crossfade
GATE = 0.22                        # fondu du rendu : démarrage/reprise et pause/stop (s)
PRELOAD = 2.0                      # ouverture piste suivante N s avant le début du fade
SKIP_MIN = 0.6                     # fondu de skip/audition : bornes (s), suit le crossfade réglé
SKIP_MAX = 2.0
CUT = 0.35                         # durée du fondu courbe "cut" (coupe franche sans clic)
CORRUPT_SLACK = 0.25               # tolérance "EOF trop tôt" (s) vs cue_out
END_EPS = 1e-3                     # epsilon de fin de piste : pos peut rester à 3e-14 de cue_out (arrondi float)
DUCK = 0.25

_RAM_LOCK = threading.Lock()
_RAM_MAX = 128                     # ~65 Ko d'enveloppe par morceau (100 floats/s) -> ~8 Mo
_RAM: OrderedDict = OrderedDict()  # path -> Analysis (LRU : une grosse banque ne gonfle pas la RAM)


class Analysis(TypedDict):
    path: str; title: str; artist: str; duration: float
    cue_in: float; cue_out: float
    envelope: np.ndarray           # float32, 100 Hz (1 valeur / 10 ms)


def _meta(path: str) -> dict:
    base = os.path.splitext(os.path.basename(path))[0]
    m = {"title": base, "artist": "", "duration": 0.0}
    try:
        o = subprocess.run(
            [_FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
            capture_output=True, timeout=20, check=True)
        j = json.loads(o.stdout)
        fmt, st = j.get("format", {}), (j.get("streams") or [{}])[0]
        tags = fmt.get("tags") or st.get("tags") or {}
        if tags.get("title"): m["title"] = tags["title"]
        if tags.get("artist"): m["artist"] = tags["artist"]
        for d in (fmt.get("duration"), st.get("duration")):
            try:
                m["duration"] = float(d); break
            except (TypeError, ValueError):
                pass
    except Exception:
        pass
    return m


def _env(path: str, ffmpeg: str) -> np.ndarray:
    """Enveloppe 100 Hz = RMS / fenêtre de 10 ms, depuis un décodage mono 8 kHz.

    ponytail: décodage à 8 kHz (Nyquist 4 kHz), pas 1 kHz. À 1 kHz, tout ce qui
    dépasse ~500 Hz est filtré : un morceau dont l'intro/outro est aigu donnait une
    enveloppe quasi nulle -> cue_out = 0,01 s, morceau « fini » instantanément,
    enchaînement en rafale et UI bloquée en fin de piste. Le RMS est calculé au fil
    de l'eau : mémoire bornée (~100 floats/s, pas de buffer de tout le morceau).
    """
    W = 80                                     # 8 kHz / 100 fenêtres par seconde = 10 ms
    try:
        p = subprocess.Popen([ffmpeg, "-hide_banner", "-loglevel", "error", "-i", path,
                              "-ar", "8000", "-ac", "1", "-f", "f32le", "pipe:1"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except Exception:
        return np.zeros(0, np.float32)
    chunks = []
    carry = np.zeros(0, np.float32)
    try:
        while True:
            b = p.stdout.read(200000)          # ~6 s de données 8 kHz
            if not b: break
            raw = np.frombuffer(b, np.float32)
            if len(carry):
                raw = np.concatenate((carry, raw))
            n = (len(raw) // W) * W            # fenêtres de 10 ms complètes
            if n:
                chunks.append(np.sqrt((raw[:n].reshape(-1, W).astype(np.float64) ** 2)
                                      .mean(axis=1)).astype(np.float32))
            carry = raw[n:]
    except Exception:
        pass
    try: p.stdout.close(); p.wait()
    except Exception: pass
    if not chunks:
        return np.zeros(0, np.float32)
    return np.concatenate(chunks)


def _persist(data_dir: str, path: str, cue_in: float, cue_out: float, duration: float):
    full = os.path.join(data_dir, "cues.json")
    with _RAM_LOCK:
        try:
            all_ = json.load(open(full)) if os.path.exists(full) else {}
        except Exception:
            all_ = {}
        all_[path] = {"cue_in": cue_in, "cue_out": cue_out, "duration": duration}
        try:
            tmp = full + ".tmp"
            with open(tmp, "w") as f: json.dump(all_, f)
            os.replace(tmp, full)
        except Exception:
            pass


def analyze(path: str, ffmpeg=_FFMPEG, data_dir="data") -> Analysis:
    with _RAM_LOCK:
        hit = _RAM.get(path)
        if hit is not None:
            _RAM.move_to_end(path)             # LRU : l'entrée relue redevient récente
    if hit is not None:
        return dict(hit)
    m = _meta(path)
    env = _env(path, ffmpeg)
    dur = max(m["duration"], len(env) / 100.0, 0.0)
    idx = np.nonzero(np.abs(env) > _THR)[0]
    if len(env) == 0 or len(idx) == 0:
        # ponytail: piste muette/incodable -> fallback [0, duration] (vallée entière jouée)
        cue_in, cue_out = 0.0, dur
    else:
        cue_in = float(idx[0]) / 100.0
        cue_out = min((float(idx[-1]) + 1.0) / 100.0, dur)
    a = Analysis(path=path, title=m["title"], artist=m["artist"], duration=dur,
                 cue_in=cue_in, cue_out=cue_out, envelope=env)
    with _RAM_LOCK:
        _RAM[path] = a
        _RAM.move_to_end(path)
        while len(_RAM) > _RAM_MAX:
            _RAM.popitem(last=False)          # plus vieille analyse jetée (recalculable)
    _persist(data_dir, path, cue_in, cue_out, dur)
    return dict(a)


def waveform(path: str, pixels: int, ffmpeg=_FFMPEG, data_dir="data") -> list[float]:
    env = analyze(path, ffmpeg, data_dir)["envelope"]
    if len(env) == 0 or pixels <= 0:
        return [0.0] * max(int(pixels), 0)
    buckets = np.array_split(np.abs(env), int(pixels))
    vals = np.array([b.max() if b.size else 0.0 for b in buckets], np.float32)
    peak = vals.max()
    return (vals / peak).tolist() if peak > 0 else [0.0] * len(vals)


def xf_gains(curve: str, pc) -> tuple[np.ndarray, np.ndarray]:
    """Gains (sortant, entrant) du crossfade pour une progression pc ∈ [0, 1].

    linear      : out+in = 1 (somme des amplitudes constante, historique)
    equal_power : out²+in² = 1 (somme des puissances constante — standard DJ,
                  meilleur rendu sur deux morceaux non corrélés)
    smooth      : smoothstep s = 3pc²-2pc³, out = 1-s, in = s (départ/arrivée adoucis)
    cut         : gains linéaires ; c'est la DURÉE qui est raccourcie (CUT, cf. _render)
    """
    pc = np.clip(np.asarray(pc, np.float32), 0.0, 1.0)
    if curve == "equal_power":
        a = pc * (np.pi / 2.0)
        return np.cos(a).astype(np.float32), np.sin(a).astype(np.float32)
    if curve == "smooth":
        s = (pc * pc * (3.0 - 2.0 * pc)).astype(np.float32)
        return (1.0 - s).astype(np.float32), s
    return (1.0 - pc).astype(np.float32), pc          # linear et cut


class _Track:
    """Piste ouverte : analyse (chiffrée RAM), pipe ffmpeg, thread lecteur -> queue bornée."""

    def __init__(self, path: str, rate: int, data_dir: str, ffmpeg: str, start: float | None = None):
        self.path, self.rate, self.data_dir, self.ffmpeg = path, rate, data_dir, ffmpeg
        self._start_arg = start           # seek explicite (restauration) sinon cue_in
        self.a: Analysis | None = None
        self.start: float | None = None
        self.q: Queue = Queue(maxsize=QUEUE_MAX)
        self.buf = np.zeros((0, 2), np.float32)
        self.consumed = 0                 # frames consommées par le mixeur depuis le début du stream
        self.tot = 0                      # frames décodées (lecteur)
        self.reach = 0.0                  # position fichier atteinte par le décodage (fin)
        self.eof = False; self.err = False; self.stop = False
        self.announced = False; self.corrupt_fired = False
        self.promo = None                 # instant de promotion (rampe d'entrée), None si via fade
        self.proc = None
        self.th = threading.Thread(target=self._run, daemon=True)
        self.th.start()

    def _run(self):
        try:
            self.a = analyze(self.path, self.ffmpeg, self.data_dir)
        except Exception:
            self.err = True
        if self._start_arg is not None:
            self.start = min(max(self._start_arg, 0.0), max(self.a["duration"] - 0.01, 0.0) if self.a else 0.0)
        elif self.a:
            self.start = float(self.a["cue_in"])
        bs = DBLK * 2 * 4
        try:
            cmd = (["-hide_banner", "-loglevel", "error"]
                   + (["-ss", f"{self.start:.3f}"] if self.start else [])   # seek AVANT -i (rapide)
                   + ["-i", self.path, "-map", "0:a", "-f", "f32le",
                      "-ac", "2", "-ar", str(self.rate), "pipe:1"])
            p = subprocess.Popen([self.ffmpeg] + cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self.proc = p
            carry = b""
            while not self.stop:
                b = p.stdout.read(1 << 16)
                if not b: break
                carry += b
                while len(carry) >= bs:
                    blk = np.frombuffer(bytes(carry[:bs]), np.float32).reshape(-1, 2)
                    carry = carry[bs:]
                    self.tot += DBLK
                    self._put(blk)
            if carry and not self.stop:
                self.tot += len(carry) // 8
                self._put(np.frombuffer(bytes(carry[:len(carry) // 8 * 8]), np.float32).reshape(-1, 2))
        except Exception:
            self.err = True
        finally:
            self.eof = True
            self.reach = (self.start or 0.0) + self.tot / self.rate
            try:
                if self.proc:
                    self.proc.stdout.close(); self.proc.wait()
            except Exception:
                pass

    def _put(self, blk):
        while not self.stop:              # throttle naturel : queue pleine -> os.read se bloque
            try:
                self.q.put(blk, timeout=0.1); return
            except Exception:
                pass

    def kill(self):
        self.stop = True
        try:
            if self.proc: self.proc.kill()
        except Exception:
            pass


class Engine:
    """Moteur. events: attribut callable (event:str, data:dict) ou None."""

    def __init__(self, out_factory=None, data_dir="data"):
        self._data_dir = data_dir
        self._ffmpeg = _FFMPEG
        self.events = None
        of = out_factory or self._default_out
        self._out = of(48000)
        self._rate = max(int(getattr(self._out, "rate", 48000)), 1)
        self._queue: list[str] = []
        self._cmds: Queue = Queue()
        self._cur: _Track | None = None
        self._nxt: _Track | None = None
        self._fade = None                 # (f0, L, kill, audio, curve)
        self._skip = False
        self._pending = None              # position de restauration pour la prochaine promo
        self._empty_fired = False
        self._preview_path = None         # path en audition (posé par preview(), purgé par requeue/fin)
        self._volume, self._ducking, self._xf = 1.0, False, 8.0
        self._xf_curve = "equal_power"    # linear | equal_power | smooth | cut
        self._g_from, self._g_to, self._g_t0 = 1.0, 1.0, 0.0
        self._t = 0.0                     # horloge de rendu (s de frames écrits)
        self._playing = False
        self._gate = 0.0                  # enveloppe de sortie : 0 silence, 1 plein gain
        self._out_dead = False            # device perdu : log une fois, plus de write
        self._th = threading.Thread(target=self._loop, daemon=True)
        self._th.start()

    @staticmethod
    def _default_out(rate):
        # imports paresseux : un backend absent ne doit pas casser l'import du module
        # (tests headless). Linux : ALSA natif d'abord (pas de PortAudio requis),
        # fallback sounddevice si PulseAudio/PipeWire n'expose pas de device ALSA direct.
        alsa_exc = None
        if sys.platform.startswith("linux"):
            try:
                from .output_alsa import ALSAOut
                return ALSAOut(rate)
            except Exception as exc:
                alsa_exc = exc
        try:
            from .output_sd import SDOut
            return SDOut(rate)
        except Exception as exc:
            if alsa_exc is None:
                raise
            raise OSError(f"aucune sortie audio — ALSA : {alsa_exc} ; sounddevice : {exc}") from alsa_exc

    # ---------------- API ---------------------------------------------
    def set_queue(self, paths):
        self._cmds.put(("q", list(paths)))

    def play_toggle(self): self._cmds.put(("toggle", None))

    def skip(self): self._cmds.put(("skip", None))

    def set_crossfade(self, seconds: float):
        self._xf = min(max(float(seconds), 2.0), 16.0)

    def set_crossfade_curve(self, name: str):
        """Courbe du crossfade ; valeur inconnue -> on garde l'actuelle (jamais d'exception)."""
        if name in XF_CURVES:
            self._xf_curve = name

    def _xf_len(self) -> float:
        """Longueur réglée, ou CUT quand la courbe est une coupe franche."""
        return CUT if self._xf_curve == "cut" else self._xf

    def _skip_len(self) -> float:
        """Fondu de skip / d'audition : suit le crossfade réglé (~xf/6), borné 0,6–2 s.

        Un skip ne doit pas s'entendre comme une coupure (0,3 s) ni traîner comme un
        crossfade de fin de piste (8 s). Courbe "cut" = l'utilisateur veut des coupes
        franches : on garde CUT.
        """
        if self._xf_curve == "cut":
            return CUT
        return min(max(self._xf / 6.0, SKIP_MIN), SKIP_MAX)

    def set_volume(self, v: float):
        self._volume = min(max(float(v), 0.0), 1.0); self._retarget()

    def set_duck(self, on: bool):
        self._ducking = bool(on); self._retarget()

    def set_position(self, seconds: float):
        self._cmds.put(("pos", float(seconds)))

    def preview(self, path: str):
        """Audition : `path` joue SEUL (démarrage à cue_in), file suspendue.
        Idempotent (même path déjà en audition -> no-op) ; fade court type skip si
        une piste joue ; file illisible -> event corrupt, jamais de crash."""
        self._cmds.put(("preview", str(path or "")))

    def requeue(self, paths, start=False, cut=False):
        """File restaurée après audition. start=True + moteur à l'arrêt : démarre
        la première comme un play ; moteur en lecture : remplace la file future.
        cut=True : l'audition audible est coupée (fondu de skip) au profit de la file."""
        self._cmds.put(("requeue", (list(paths), bool(start), bool(cut))))

    def status(self) -> dict:
        cur, nxt = self._cur, self._nxt
        rate = self._rate
        path = cur.path if cur else None
        pos = dur = cin = cout = 0.0
        if cur and cur.a is not None and cur.start is not None:
            pos = cur.start + cur.consumed / rate
            dur, cin, cout = cur.a["duration"], cur.a["cue_in"], cur.a["cue_out"]
        fading, audio = False, False
        fade = self._fade                 # une seule lecture : le mixeur peut la purger
        if fade is not None:
            f0, L, _kill, audio, _curve = fade
            if audio and cur is not None and cur.a is not None and cur.start is not None:
                fading = (cur.start + cur.consumed / rate) < f0 + L
            else:
                fading = self._t < f0 + L
        # piste entrante déjà audible (fade naturel en cours) : l'UI affiche SON titre et
        # SA progression, sinon le sortant (barre finie, pos ~ cue_out) reste affiché 6-8 s.
        # Clé toujours présente (None hors fade) : contrat stable côté UI.
        incoming = None
        if (fading and audio and nxt is not None and nxt.a is not None
                and nxt.start is not None):
            incoming = {"path": nxt.path,
                        "pos": nxt.start + nxt.consumed / rate,
                        "dur": nxt.a["duration"],
                        "cue_in": nxt.a["cue_in"],
                        "cue_out": nxt.a["cue_out"]}
        return {"playing": self._playing, "path": path, "pos": pos, "dur": dur,
                "cue_in": cin, "cue_out": cout, "fading": fading,
                "incoming": incoming,
                "next": list(self._queue), "volume": self._volume,
                "ducking": self._ducking, "crossfade": self._xf,
                "crossfade_curve": self._xf_curve,
                "preview": self._preview_path is not None}

    # ---------------- Mixeur ------------------------------------------
    def _ev(self, name, data):
        if self.events:
            try: self.events(name, data)
            except Exception: pass

    def _stop_if_preview(self):
        """Vidage du moteur (queue_empty : promote(None), corrupt sans suite, fin
        naturelle) : repos — rien ne rejoue sans PLAY volontaire/requeue(start=True)
        (aucune surprise sonore, file vide repoussée ne démarre pas seule)."""
        if self._preview_path:
            self._preview_path = None
        self._playing = False

    def _pop_cmds(self):
        while True:
            try: k, v = self._cmds.get_nowait()
            except Empty: return
            self._apply(k, v)

    def _apply(self, k, v):
        if k == "q":
            self._queue = list(v)
            # Suivante déjà préchargée mais retirée de la file : ne pas la jouer quand même
            # (sinon la piste supprimée/écrasée par l'UI s'enchaîne et la file affichée ment).
            tr = self._nxt
            if (tr is not None and tr.path != self._preview_path
                    and tr.path not in self._queue):
                tr.kill(); self._nxt = None
                if self._fade is not None and self._fade[2]:   # fade skip visait cette piste
                    self._fade = None
                    self._skip = bool(self._queue)             # re-planifie sur la nouvelle file
        elif k == "toggle":
            self._playing = not self._playing
        elif k == "skip":
            if self._cur is None: return
            if self._queue or self._nxt is not None:
                self._skip = True        # fondu de skip dès que la suivante a des données
            else:
                self._fade = (self._t, self._skip_len(), True, False, self._xf_curve)   # fade-out rendu puis stop
        elif k == "pos":
            if self._cur is not None and self._cur.a is not None:
                p = min(max(v, self._cur.a["cue_in"]), max(self._cur.a["cue_out"] - 0.05, 0.0))
                old = self._cur; old.kill()
                self._cur = _Track(old.path, self._rate, self._data_dir, self._ffmpeg, start=p)
                self._cur.promo = p     # rampe d'entrée calée sur la position audio de la piste
                if self._fade is not None:
                    # Fade en cours calé sur l'ancienne position et sur un entrant déjà consommé :
                    # on l'annule et on rend l'entrant à la file (il sera rouvert depuis cue_in).
                    self._fade = None
                    if self._nxt is not None and self._nxt.path != self._preview_path:
                        self._nxt.kill(); self._nxt = None
            else:
                self._pending = max(v, 0.0)
        elif k == "preview":
            if not isinstance(v, str) or not v:
                return
            if self._preview_path == v and (self._cur is not None or self._nxt is not None):
                return                            # audition déjà en cours sur ce path : no-op
            self._queue = []                      # file suspendue (le serveur la remet via requeue)
            self._preview_path = v
            if self._nxt is not None:             # remplace une audition pas encore audible
                self._nxt.kill()
            self._nxt = _Track(v, self._rate, self._data_dir, self._ffmpeg)
            self._skip = True                     # fondu de skip si une piste joue
            self._playing = True                  # l'audition démarre même moteur au repos
        elif k == "requeue":
            paths, start, cut = v
            self._queue = list(paths)
            if (self._nxt is not None and self._nxt.path == self._preview_path
                    and (self._cur is None or self._cur.path != self._preview_path)):
                # Audition encore dans _nxt (pas promue) : abandon, jamais de promotion.
                # Sans ça le _skip posé par preview survit et le mixeur promeut l'audition
                # (elle joue sans fin, la file restaurée ne reprend jamais).
                self._nxt.kill(); self._nxt = None
                self._fade = None                   # fade de skip en cours : il visait l'audition
                self._preview_path = None
                if start:                           # la file jouait : skip -> la suivante démarre
                    self._skip = True
                    self._playing = True
                else:
                    self._skip = False
                    self._playing = False
                    self._empty_fired = True        # file restaurée : pas de toast "file vide"
                return
            was_preview = cut and self._cur is not None and self._cur.path == self._preview_path
            self._preview_path = None
            if was_preview and start:
                self._skip = True                   # fondu de skip puis la file enchaîne
            elif was_preview:
                # stop d'audition : la file est restaurée, donc pas de toast "file vide"
                self._empty_fired = True
                self._fade = (self._t, self._skip_len(), True, False, self._xf_curve)
            elif start:
                self._playing = True

    def _retarget(self):
        tgt = self._volume * (DUCK if self._ducking else 1.0)
        if tgt == self._g_to: return
        g = self._g_from + (self._g_to - self._g_from)  # valeur actuelle ~ ancrée (rampe en cours)
        try:
            ts = self._t
            f_, t_, t0_ = self._g_from, self._g_to, self._g_t0
            if t_ != f_ and RAMP > 0:
                g = f_ + (t_ - f_) * min(max((ts - t0_) / RAMP, 0.0), 1.0)
        except Exception:
            pass
        self._g_from, self._g_to, self._g_t0 = g, tgt, self._t

    def _mgains(self, t0, t1) -> np.ndarray:
        f_, t_, t0_ = self._g_from, self._g_to, self._g_t0
        if t_ == f_: return np.full(MIX, t_, np.float32)
        ts = t0 + np.arange(MIX) / self._rate
        return (f_ + (t_ - f_) * np.clip((ts - t0_) / RAMP, 0.0, 1.0)).astype(np.float32)

    def _gate_ramp(self):
        """Enveloppe de sortie du rendu : 0 = silence, 1 = plein gain.

        Démarrage/reprise 0 -> 1, pause/stop 1 -> 0 avant d'écrire des zéros.
        Pas d'un bloc (MIX/rate) interpolé par échantillon : gain continu d'un bloc
        à l'autre, donc aucun clic. None au gain unitaire (cas courant, zéro alloc).
        """
        tgt = 1.0 if self._playing else 0.0
        g0 = self._gate
        if g0 == 1.0 and tgt == 1.0:
            return None
        step = (MIX / self._rate) / GATE
        g1 = min(g0 + step, 1.0) if tgt > g0 else max(g0 - step, 0.0)
        self._gate = g1
        if g1 == g0:
            return np.full(MIX, g1, np.float32)
        return np.linspace(g0, g1, MIX, dtype=np.float32)

    def _pull(self, tr, want) -> np.ndarray:
        """Frames (k<=want, 2) prêtes d'une piste ; jamais au-delà, jamais d'exception."""
        if tr.buf.shape[0] >= want:
            r, tr.buf = tr.buf[:want], tr.buf[want:]
            tr.consumed += want
            return r
        parts = [tr.buf] if tr.buf.shape[0] else []
        while sum(p.shape[0] for p in parts) < want:
            try:
                parts.append(tr.q.get_nowait())
            except Empty:
                break
        if parts:
            arr = np.concatenate(parts)
            r, tr.buf = arr[:want], arr[want:]
            tr.consumed += r.shape[0]
            return r
        return np.zeros((0, 2), np.float32)

    def _bad(self, tr) -> bool:
        """EOF trop tôt / aucun octet décodé."""
        if not tr.eof or tr.buf.shape[0] or tr.tot:
            return False
        if not tr.q.empty():
            return False
        if tr.a is None: return True
        return tr.tot == 0 or tr.reach < tr.a["cue_out"] - CORRUPT_SLACK

    def _open_next(self):
        p = self._queue[0]
        start = self._pending
        t = _Track(p, self._rate, self._data_dir, self._ffmpeg, start=start)
        self._pending = None
        self._nxt = t

    def _ready(self, tr) -> bool:
        return tr is not None and tr.a is not None and (tr.buf.shape[0] or not tr.q.empty())

    def _drop_nxt(self):
        """Piste suivante morte à l'ouverture : event corrupt, retrait de la file, retente."""
        tr = self._nxt
        if tr is None: return
        self._ev("corrupt", {"path": tr.path}); tr.kill()
        if self._queue and self._queue[0] == tr.path: self._queue.pop(0)
        self._nxt = None
        self._skip = True               # retente tout de suite avec la suivante
        if self._cur is None and self._preview_path == tr.path:
            # audition morte au repos (jamais audible) : failover -> fin d'audition propre
            self._preview_path = None; self._playing = False
            if not self._empty_fired:
                self._empty_fired = True; self._ev("queue_empty", {})

    def _promote(self, tr):
        """cur -> tr (tr peut être None : file vide). Pop la file quand la piste vient de queue[0]."""
        via_fade = self._fade is not None
        old = self._cur
        if old is not None: old.kill()
        self._cur = tr; self._nxt = None; self._fade = None; self._skip = False
        if tr is not None:
            if tr.path in self._queue:
                self._queue.remove(tr.path)   # jouée : retirée où qu'elle soit (réordonnancement UI)
            # pas de rampe d'entrée si un fade s'est déjà occupé du lancement
            tr.promo = tr.start + tr.consumed / self._rate if (not via_fade and tr.start is not None) else None
            if via_fade and old is not None:
                self._ev("ended_fade", {"from": old.path, "to": tr.path})
        elif not self._empty_fired:
            self._empty_fired = True
            self._ev("queue_empty", {})
            self._stop_if_preview()       # fin d'audition via promote(None) : repos

    def _write(self, blk):
        """write device ; device perdu -> log/event une fois, moteur en pause (pas de spam)."""
        try:
            self._out.write(blk)
        except Exception as exc:
            if not self._out_dead:
                self._out_dead = True
                traceback.print_exc()
                self._ev("audio_error", {"error": str(exc)})
            self._playing = False
            self._gate = 0.0
            time.sleep(0.25)              # device mort : ne pas marteler la boucle

    def _loop(self):
        zero = np.zeros((MIX, 2), np.float32)
        while True:
            try: self._pop_cmds()
            except Exception: pass
            # repos ET fondu de sortie fini : device chaud, position figée
            if not self._playing and self._gate <= 0.0:
                self._write(zero); continue
            try:
                blk = self._render()          # pause : rend encore le temps de la rampe
            except Exception:
                traceback.print_exc()
                self._cur = None; self._nxt = None; self._fade = None; self._skip = False
                self._gate = 0.0
                continue
            self._write(blk)

    def _render(self) -> np.ndarray:
        rate, t0 = self._rate, self._t
        self._t += MIX / rate
        t1 = self._t
        mix = np.zeros((MIX, 2), np.float32)
        g = self._mgains(t0, t1)
        gate = self._gate_ramp()
        if gate is not None:
            g *= gate                     # enveloppe de sortie (démarrage/pause/stop)
        cur, nxt, fade = self._cur, self._nxt, self._fade

        # --- planification : ouverture suivante (preload), lancement fade
        if self._nxt is None and self._queue and self._cur is None and self._playing:
            self._open_next()             # pas d'ouverture pendant un fondu de pause
        if cur is not None and cur.a is not None and cur.start is not None:
            pos = cur.start + cur.consumed / rate
            a = cur.a
            half = max((a["cue_out"] - a["cue_in"]) / 2.0, 0.0)
            xf = self._xf_len()                 # réglage, ou CUT si courbe "cut"
            fs = a["cue_out"] - min(xf, half)
            if self._nxt is None and self._queue and (self._skip or pos >= fs - PRELOAD):
                self._open_next()
            if self._nxt is not None and self._fade is None:
                if self._skip and self._ready(self._nxt):
                    self._fade = (t0, min(self._skip_len(), max(a["cue_out"] - pos, TAIL)), True,
                                  False, self._xf_curve)
                elif (not self._skip and pos >= fs and self._ready(self._nxt)
                      and self._nxt.a is not None):
                    # fade naturel calé sur la position audio : se termine pile à cue_out
                    L = min(xf, half, max(a["cue_out"] - pos, TAIL))
                    if L >= TAIL: self._fade = (pos, L, False, True, self._xf_curve)
            if self._bad(cur) and not cur.corrupt_fired:
                cur.corrupt_fired = True
                self._ev("corrupt", {"path": cur.path}); cur.kill()
                if self._fade is not None: self._fade = None   # fade sur piste morte : repart en skip (sinon gel)
                if self._queue or self._nxt is not None:
                    self._skip = True
                else:
                    self._cur = None
                    self._stop_if_preview()
                    if not self._empty_fired:
                        self._empty_fired = True; self._ev("queue_empty", {})

        # relecture d'état post-planification (cur/nxt/fade peuvent avoir changé)
        cur, nxt, fade = self._cur, self._nxt, self._fade

        # --- side fades / mixes
        gout = gin = None
        if fade is not None:
            f0, L, kill, audio, curve = fade
            if L <= 0:
                pc = np.ones(MIX, np.float32)
            elif audio:
                # p calé sur la position audio du sortant (insensible aux stalls)
                if cur is not None and cur.start is not None:
                    ts = cur.start + cur.consumed / rate + np.arange(MIX) / rate
                    pc = np.clip((ts - f0) / L, 0.0, 1.0).astype(np.float32)
                else:
                    pc = np.ones(MIX, np.float32)
            else:
                ts = t0 + np.arange(MIX) / rate
                pc = np.clip((ts - f0) / L, 0.0, 1.0).astype(np.float32)
            gout, gin = xf_gains(curve, pc)   # sortant / entrant selon la courbe réglée

        if cur is not None and cur.a is not None and cur.start is not None:
            pos = cur.start + cur.consumed / rate
            room = cur.a["cue_out"] - pos
            want = min(MIX, max(int(room * rate + 1e-6), 0))
            seg = self._pull(cur, want)
            k = seg.shape[0]
            if k:
                if not cur.announced:
                    cur.announced = True
                    self._ev("track_started", {"path": cur.path})
                tps = pos + np.arange(k) / rate      # temps audio des frames consommées
                if gout is not None:
                    w = gout[:k]
                elif nxt is None:                    # rampe de fin propre, jamais de coupure à bras
                    w = np.clip((cur.a["cue_out"] - tps) / TAIL, 0.0, 1.0).astype(np.float32)
                else:
                    w = np.ones(k, np.float32)
                gg = (g[:k] * w).astype(np.float32)
                if cur.promo is not None:
                    gg *= np.clip((tps - cur.promo) / ENTER, 0.0, 1.0).astype(np.float32)
                mix[:k] += seg * gg[:, None]
        if nxt is not None and nxt.a is not None and nxt.start is not None and gin is not None:
            npos = nxt.start + nxt.consumed / rate
            nroom = nxt.a["cue_out"] - npos
            want2 = min(MIX, max(int(nroom * rate + 1e-6), 0))
            seg2 = self._pull(nxt, want2)
            k2 = seg2.shape[0]
            if k2:
                tps2 = npos + np.arange(k2) / rate
                gg2 = (g[:k2] * gin[:k2]).astype(np.float32)
                if nxt.promo is not None:
                    gg2 *= np.clip((tps2 - nxt.promo) / ENTER, 0.0, 1.0).astype(np.float32)
                mix[:k2] += seg2 * gg2[:, None]

        # --- transitions en fin de bloc
        # promote-on-ready (play sur file fraîche / restauration / après coupure buffer)
        if self._cur is None and self._nxt is not None and self._nxt.a is not None:
            if self._bad(self._nxt):
                self._drop_nxt()
            elif self._ready(self._nxt):
                self._promote(self._nxt)
        # fin de fade kill (skip / corrupt) : promo immédiate, gain sortant déjà à 0
        if self._fade is not None:
            f0, L, kill = self._fade[:3]
            if t1 >= f0 + L and kill:
                if self._nxt is not None and self._bad(self._nxt):
                    self._drop_nxt()
                else:
                    self._promote(self._nxt)
        # fin naturelle : cue_out atteint (ou stream terminé dans la tolérance de 0.25 s)
        cur = self._cur
        if cur is not None and cur.a is not None and cur.start is not None:
            pos = cur.start + cur.consumed / rate
            if pos >= cur.a["cue_out"] - END_EPS or (cur.eof and not cur.buf.shape[0] and cur.q.empty()
                                          and cur.reach >= cur.a["cue_out"] - CORRUPT_SLACK):
                if self._nxt is not None and self._bad(self._nxt):
                    self._drop_nxt()
                if self._nxt is not None:
                    self._promote(self._nxt)
                else:
                    self._cur = None
                    self._stop_if_preview()
                    if not self._queue and not self._empty_fired:
                        self._empty_fired = True; self._ev("queue_empty", {})
        # rotation quand une piste existe (réarmera queue_empty à la fin suivante)
        if self._cur is not None or self._nxt is not None:
            self._empty_fired = False
        return mix
