"""Serveur Auto-DJ (S5) : API 1-doc, SSE, banque, autosave — `python -m app.server`.

I/O longues jamais dans le thread serveur : scan banque -> thread de fond,
waveform -> endpoint sync (threadpool anyio), yt-dlp -> pool de ytdl.py,
moteur -> son propre thread (pacing device). Lancé par lancer.sh (PID dans
data/server.pid, log data/server.log).
"""
import argparse
import asyncio
import atexit
import hashlib
import json
import os
import re
import threading
import time
import urllib.parse

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from . import state as st
from .engine import Engine, _meta, waveform
from .queue_rules import build_queue
from .ytdl import Downloader

STATIC = Path(__file__).resolve().parent / "static"
AUDIO_EXT = (".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg")
SCAN_EXT = AUDIO_EXT + (".webm", ".opus")  # conteneurs yt-dlp bestaudio : la banque doit les voir (§3.3)
TERM_KEEP = 10.0                                  # s d'affichage d'un job téléchargement fini
IMPORT_MAX = 200 * 1024 * 1024                    # taille max d'un fichier importé (octets)


def _sse(name: str | None, data: dict) -> bytes:
    """D = event nommé (`event:`) sinon message de fond -> es.onmessage côté UI."""
    d = json.dumps(data, default=str)
    return (f"event: {name}\ndata: {d}\n\n" if name else "data: " + d + "\n\n").encode()


def _multipart_files(body: bytes, ctype: str) -> list[tuple[str, bytes]]:
    """[(filename, payload)] des champs fichiers d'un multipart/form-data.

    ponytail: python-multipart absent (pip verrouillé) -> parse minimal sans deps ;
    corps déjà en RAM (request.body) -> IMPORT_MAX protège le disque, pas la RAM.
    """
    m = re.search(r'boundary=(?:"([^"]+)"|([^;,]+))', ctype or "")
    if not m or not body:
        return []
    delim = b"--" + (m.group(1) or m.group(2)).strip().encode()
    out = []
    for part in body.split(delim):
        if part[:2] == b"--":                       # closing --
            break
        head, sep, payload = part.lstrip(b"\r\n").partition(b"\r\n\r\n")
        if not sep:
            continue                                # préambule / fragment
        nm = re.search(rb'filename="([^"]*)"', head)
        if nm:
            out.append((nm.group(1).decode("utf-8", "replace"),
                        payload[:-2] if payload.endswith(b"\r\n") else payload))
    return out


def _hard_exit(code: int = 0) -> None:
    """Sortie process (module-level : monkeypatchée par les tests)."""
    os._exit(code)


def _sha256_file(p: str, cache: dict) -> str:
    """SHA-256 streaming d'un fichier (cache par import) ; "" si illisible."""
    if p not in cache:
        try:
            h = hashlib.sha256()
            with open(p, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            cache[p] = h.hexdigest()
        except OSError:
            cache[p] = ""
    return cache[p]


def _same_file(p: str, size: int, digest: str, cache: dict) -> bool:
    """Vrai si `p` est byte-identique au payload (taille puis SHA-256)."""
    try:
        return os.path.getsize(p) == size and _sha256_file(p, cache) == digest
    except OSError:
        return False


class _Conn:
    """Abonné SSE : file asyncio remplie depuis les threads moteur/downloader."""

    __slots__ = ("loop", "q")

    def __init__(self, loop):
        self.loop, self.q = loop, asyncio.Queue(maxsize=256)

    def push(self, item):                          # n'importe quel thread
        try:
            self.loop.call_soon_threadsafe(self._put, item)
        except RuntimeError:                       # boucle fermée au shutdown
            pass

    def _put(self, item):
        try:
            self.q.put_nowait(item)
        except asyncio.QueueFull:                  # client lent : tick lâché, ok
            pass


class Srv:
    """État vivant du serveur (banque, offres, bus, cycles d'autosave)."""

    def __init__(self, music_dirs, downloads_dir, data_dir, out_factory=None):
        self._music = [os.fspath(d) for d in music_dirs]
        self._downloads = os.fspath(Path(downloads_dir))
        self._data = os.fspath(Path(data_dir))
        self._conns: set = set()
        self._lock = threading.Lock()              # banque + jobs terminés + historique
        self._scan_lock = threading.Lock()         # un seul walk/ffprobe à la fois (scans concurrents)
        self._bank: dict = {}                      # path -> row (membres de la banque)
        self._rows: dict = {}                      # row de tout path connu (banque + file)
        self._term: dict = {}                      # id job -> instant de fin (prune UI)
        self._preview_active = None                # audition en cours: {"path", "real_queue"}
        self.autofill = None                       # hook posé par build_app (file auto)
        boot = st.load(self._data)                 # historique + réglage auto_queue persistés
        self._history: list = list(boot.get("history") or [])
        self._auto: dict = st.norm_auto_queue((boot.get("settings") or {}).get("auto_queue"))
        self.engine = Engine(out_factory, data_dir=self._data)
        self.engine.events = self._on_engine       # thread du moteur -> bus SSE
        self.dl = Downloader(self._downloads, self._on_dl)
        offer = st.offer_restore(self._data)       #offre avant tout autosave
        self._offer = offer or None
        self._resolved = offer is None             # l'autosave écrase state.json après la décision
        if self._resolved:
            # Pas d'offre (aucune piste en cours restaurable) : la session repart de l'état
            # sauvé — file + réglages — sinon l'autosave réécrirait state.json avec [].
            # Aucun play : la file attend un PLAY volontaire (pas de surprise sonore).
            q = [p for p in (boot.get("queue") or []) if isinstance(p, str)]
            sett = boot.get("settings") or {}
            self.engine.set_crossfade(sett.get("crossfade", 8.0))
            self.engine.set_crossfade_curve(sett.get("crossfade_curve", st.XF_DEFAULT))
            self.engine.set_volume(sett.get("volume", 1.0))
            self.engine.set_duck(bool(sett.get("ducking", False)))
            if q:
                self.engine.set_queue(q)
                # set_queue passe par la command queue du mixeur : on attend (borné) qu'il
                # l'ait prise, sinon un save() immédiat relirait [] et perdrait la file.
                t0 = time.monotonic()
                while self.engine.status()["next"] != q and time.monotonic() - t0 < 1.0:
                    time.sleep(0.005)
        threading.Thread(target=self._scan, daemon=True).start()
        threading.Thread(target=self._autosave, daemon=True).start()
        atexit.register(self._shutdown)

    # ---------------- banque --------------------------------------------
    def _scan(self, dirs=None) -> None:
        """Scan récursif fatiguant : ffprobe des seuls paths inconnus — thread de fond.

        Sérialisé (`_scan_lock`) : boot, fin de download et import peuvent demander un
        scan en même temps ; sans verrou, deux walks ffprobaient les mêmes fichiers.
        Les rows connues sont réutilisées (un re-scan ne re-ffprobe pas 745 fichiers).
        """
        with self._scan_lock:
            with self._lock:
                known = dict(self._bank)
            rows = {}
            for d in set(dirs if dirs is not None else self._music + [self._downloads]):
                try:
                    for root, _dirs, files in os.walk(d):
                        for f in files:
                            p = os.path.join(root, f)
                            if not f.lower().endswith(SCAN_EXT):
                                continue
                            r = known.get(p)
                            if r is None:
                                m = _meta(p)
                                r = {"path": p, "title": m["title"], "artist": m["artist"],
                                     "dur": m["duration"] or 0.0,
                                     "source": "downloads" if p.startswith(self._downloads) else "music"}
                            rows[p] = r
                except Exception:
                    pass                                    # dir effacée pendant le scan -> suite
            if not rows:
                return
            with self._lock:
                # `exists` au merge : un scan lancé avant un bank/clear ne ressuscite pas
                # les fichiers supprimés entre-temps.
                self._bank = {p: r for p, r in self._bank.items() if os.path.exists(p)}
                rows = {p: r for p, r in rows.items() if os.path.exists(p)}
                self._bank.update(rows)
                self._rows.update(rows)

    def _scan_async(self, dirs=None) -> None:
        """Scan de fond non bloquant (fin de download, import, bank/clear)."""
        threading.Thread(target=self._scan, args=(dirs,), daemon=True,
                         name="autodj-scan").start()

    def _in_roots(self, p: str) -> bool:
        """Vrai si `p` est sous un dossier musique ou downloads/ (realpath, anti-traversée)."""
        try:
            rp = os.path.realpath(p)
        except OSError:
            return False
        for d in self._music + [self._downloads]:
            root = os.path.realpath(d)
            if rp == root or rp.startswith(root + os.sep):
                return True
        return False

    def _size_index(self) -> dict:
        """{taille: [paths]} des fichiers de downloads/ (dédup d'import : taille -> hash)."""
        idx = {}
        for dirpath, _dirs, files in os.walk(self._downloads):
            for f in files:
                if f.startswith(".import-"):
                    continue                            # temporaire d'un import concurrent
                p = os.path.join(dirpath, f)
                try:
                    idx.setdefault(os.path.getsize(p), []).append(p)
                except OSError:
                    pass
        return idx

    def import_files(self, files: list[tuple[str, bytes]]):
        """Copie des uploads -> downloads/imports/ (collisions -1, -2) + scan de fond.

        Dédup par contenu (taille puis SHA-256) : un fichier byte-identique déjà dans
        downloads/ n'est pas recopié (compteur `skipped`). I/O longue : appelée depuis
        le threadpool (endpoint sync). Retourne {"added", "skipped"}, ou un message
        d'erreur (l'endpoint le renvoie en 400)."""
        dest = os.path.join(self._downloads, "imports")
        for name, payload in files:                 # batch validé AVANT la moindre écriture
            if not name.lower().endswith(AUDIO_EXT):
                return "extension audio attendue (mp3/wav/flac/m4a/aac/ogg) : " + \
                    os.path.basename(name or "?")
            if len(payload) > IMPORT_MAX:
                return f"fichier trop gros (> 200 Mo) : {os.path.basename(name or '?')}"
        try:
            os.makedirs(dest, exist_ok=True)
        except Exception:
            return "répertoire imports indisponible"
        by_size = self._size_index()
        hashed: dict = {}                           # path -> sha256 (candidat relu une fois)
        added = skipped = 0
        for name, payload in files:
            clean = "".join("_" if (c < " " or c in ':*?"<>|') else c
                            for c in os.path.basename(name.replace("\\", "/")).strip())
            digest = hashlib.sha256(payload).hexdigest()
            if any(_same_file(c, len(payload), digest, hashed)
                   for c in by_size.get(len(payload), ())):
                skipped += 1                        # contenu déjà présent : aucune copie
                continue
            stem, ext = os.path.splitext(clean)
            target = os.path.join(dest, clean)
            i = 1
            while os.path.exists(target):           # collision -> -1, -2 ...
                target = os.path.join(dest, f"{stem}-{i}{ext}"); i += 1
            tmp = os.path.join(dest, ".import-" + os.urandom(8).hex())
            try:
                with open(tmp, "wb") as w:
                    w.write(payload)
                os.replace(tmp, target)             # atomique : jamais de fichier à moitié
                added += 1
                by_size.setdefault(len(payload), []).append(target)
            except Exception:
                try: os.unlink(tmp)
                except Exception: pass
                return f"écriture impossible : {clean}"
        self._scan_async([self._downloads])         # re-scan en fond : l'import répond tout de suite
        return {"added": added, "skipped": skipped}

    def _track(self, p: str) -> dict:
        """Row UI d'un path (banque si connue, sinon meta à la volée, cache)."""
        with self._lock:
            row = self._rows.get(p)
        if row is not None:
            return row
        src = "downloads" if p.startswith(self._downloads) else "music"
        if os.path.exists(p):
            m = _meta(p)
            row = {"path": p, "title": m["title"], "artist": m["artist"],
                   "dur": m["duration"] or 0.0, "source": src}
        else:                                               # fichier parti/restauré : l'UI le montre tel quel
            row = {"path": p, "title": os.path.splitext(os.path.basename(p))[0],
                   "artist": "", "dur": 0.0, "source": "queue"}
        with self._lock:
            self._rows[p] = row
        return row

    def bank_rows(self) -> list[dict]:
        """Rows de la banque encore présentes sur disque (copie sous lock)."""
        with self._lock:
            rows = list(self._bank.values())
        return [r for r in rows if os.path.exists(r["path"])]

    def history(self) -> list[str]:
        """Derniers morceaux joués, plus récent en premier (copie sous lock)."""
        with self._lock:
            return list(self._history)

    def push_history(self, p) -> None:
        """Empile un path en tête (dédoublonné), cap state.HIST_MAX."""
        if not isinstance(p, str) or not p:
            return
        with self._lock:
            self._history = [p] + [x for x in self._history if x != p]
            del self._history[st.HIST_MAX:]

    # ---------------- events -> SSE ---------------------------------------
    def _on_engine(self, name, data):
        if name == "track_started" and not self._preview_active:
            self.push_history(data.get("path"))         # une audition ne salit pas l'historique
        if name == "queue_empty" and self._preview_active:
            qa = self._preview_active               # audition finie naturellement :
            self._preview_active = None             # vraie file restaurée, moteur au repos
            self.engine.requeue(qa["real_queue"], start=False)
            self.save()
        self._pub(name, data, named=True)                   # toast/rerender côté UI
        if name in ("track_started", "queue_empty") and self.autofill:
            try:
                self.autofill()                         # re-remplissage auto (jamais bloquant)
            except Exception as e:
                print("autofill:", type(e).__name__, e)  # jamais tuer le thread du moteur

    def _on_dl(self, name, data):
        if name in ("download_ready", "download_error"):
            now = time.monotonic()
            with self._lock:
                for j in self.dl.jobs():
                    if j["status"] in ("done", "error"):
                        self._term.setdefault(j["id"], now)
        if name == "download_ready":
            self._scan_async([self._downloads])             # re-scan léger du seul downloads/
        # download_error -> message non-nommé : l'UI (onmessage) rafraïchit la liste jobs
        self._pub(name, data, named=name == "download_ready")

    def _pub(self, name, data, named=True):
        item = _sse(name if named else None, {"type": name, **data})
        for c in list(self._conns):
            c.push(item)

    # ---------------- snapshot / save --------------------------------------
    def snapshot(self) -> dict:
        """Tout l'état UI en 1 réponse (forme contractuelle app/static)."""
        s = self.engine.status()
        playing = None
        if s["path"]:
            playing = {"path": s["path"], "pos": s["pos"], "dur": s["dur"],
                       "fading": s["fading"], "playing": s["playing"],
                       "incoming": s["incoming"]}     # piste entrante pendant un crossfade
        settings = {"crossfade": s["crossfade"], "crossfade_curve": s["crossfade_curve"],
                    "volume": s["volume"], "ducking": s["ducking"],
                    "auto_queue": dict(self._auto)}
        bq = [self._track(p) for p in s["next"]]
        now = time.monotonic()
        with self._lock:
            bank = [r for r in self._bank.values() if os.path.exists(r["path"])]
            term = dict(self._term)
        downloading = []
        for j in self.dl.jobs():
            if j["status"] in ("done", "error") and now - term.get(j["id"], now) > TERM_KEEP:
                continue
            d = {k: j.get(k) for k in ("id", "url", "title", "status")}
            if j.get("err"):
                d["err"] = j["err"]
            downloading.append(d)
        qa = self._preview_active
        return {"playing": playing, "queue": bq, "bank": bank, "downloading": downloading,
                "settings": settings, "restore_offer": self._offer,
                "history": self.history(),
                "preview": {"path": qa["path"]} if qa else None}

    def save(self) -> None:
        """state.json = SCHEMA ; rien tant que l'offre de restauration est en attente."""
        if not self._resolved:
            return
        try:
            s = self.engine.status()
            qa = self._preview_active               # audition : la vraie file, pas la file suspendue
            with self._lock:
                bank = list(self._bank)
                hist = list(self._history)
            st.save({"queue": list(qa["real_queue"]) if qa else list(s["next"]),
                     "playing": {"path": s["path"], "pos": s["pos"]} if s["path"] else None,
                     "settings": {"crossfade": s["crossfade"],
                                  "crossfade_curve": s["crossfade_curve"],
                                  "volume": s["volume"], "ducking": s["ducking"],
                                  "auto_queue": dict(self._auto)},
                     "history": hist,
                     "bank": bank}, self._data)
        except Exception:
            pass                                            # perte d'un autosave : jamais de crash

    def _autosave(self):
        while True:
            time.sleep(2.0)
            self.save()

    def _shutdown(self):
        try:
            if self.engine.status()["playing"]:
                self.engine.play_toggle()                   # pause -> écrit zéros, pas de clic
        except Exception:
            pass
        self.save()
        # ponytail: engine n'a pas d'API close ; device rendu par le noyau à l'exit
        # (close() concurrent de write() = risque writei->handle fermé ; Version 2: engine.close())
        try:
            self.dl.shutdown()
        except Exception:
            pass


def build_app(music_dirs, downloads_dir, data_dir, out_factory=None) -> FastAPI:
    srv = Srv(music_dirs, downloads_dir, data_dir, out_factory)
    app = FastAPI(title="Auto-DJ", docs_url=None, redoc_url=None, openapi_url=None)

    async def _body(req: Request) -> dict:
        try:
            j = await req.json()
            return j if isinstance(j, dict) else {}
        except Exception:
            return {}

    def err(msg, code=400):
        return JSONResponse({"error": msg}, status_code=code)

    def guard(req, exc):                    # filet : JAMAIS de stack trace au client
        try:
            return JSONResponse({"error": "interne: " + type(exc).__name__}, status_code=200)
        except Exception:
            return JSONResponse({"error": "interne"}, status_code=200)

    app.add_exception_handler(Exception, guard)

    @app.middleware("http")
    async def _no_store(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["Cache-Control"] = "no-store"   # stale CSS/JS hier : jamais de cache
        return resp

    def qbase() -> list[str]:
        """File de référence des ops queue : real_queue pendant une audition
        (la file moteur est suspendue le temps de l'écoute)."""
        qa = srv._preview_active
        return list(qa["real_queue"]) if qa else srv.engine.status()["next"]

    def put_queue(paths: list[str], start=False):
        """set_queue, ou requeue (purge l'audition : le DJ reprend la main)."""
        if srv._preview_active:
            srv._preview_active = None
            srv.engine.requeue(paths, start=start)
        else:
            srv.engine.set_queue(paths)

    def autofill(queue=None) -> int:
        """Re-remplit la file si auto_queue actif et file à venir < min (jamais en
        audition). `queue` : file de référence si l'appelant vient de la calculer."""
        a = srv._auto
        if not a["enabled"] or srv._preview_active:
            return 0
        q = qbase() if queue is None else list(queue)
        if len(q) >= a["min"]:
            return 0
        new = build_queue(srv.bank_rows(), a["fill"], queue_paths=q,
                          history=srv.history())
        if not new:
            return 0
        for p in new:
            srv._track(p)
        put_queue(qbase() + new if queue is None else q + new)
        srv.save()
        return len(new)

    srv.autofill = autofill            # hook appelé par la pompe d'événements moteur

    @app.get("/api/state")
    def api_state():
        return srv.snapshot()

    @app.post("/api/play")
    async def api_play(req: Request):
        qa = srv._preview_active
        if qa:                          # PLAY pendant une audition : la file suit l'écoute
            srv._preview_active = None
            srv.engine.requeue(qa["real_queue"], start=True)
            srv.save()
            return {"ok": True}
        srv_m = srv.engine.status()
        if not srv_m["playing"] and srv_m["path"] is None and not srv_m["next"]:
            # Les commandes de file passent par le mixeur (FIFO) : un POST /api/queue
            # juste avant n'est pas encore visible ici. Une relecture bornée évite le
            # faux « file vide » quand on ajoute un morceau puis qu'on lance la lecture.
            await asyncio.sleep(0.05)
            srv_m = srv.engine.status()
            if not srv_m["playing"] and srv_m["path"] is None and not srv_m["next"]:
                return {"error": "empty"}                   # lecture sur file vide : jamais de crash
        srv.engine.play_toggle()
        return {"ok": True}

    @app.post("/api/skip")
    async def api_skip():
        qa = srv._preview_active
        if qa:                          # SKIP pendant une audition : file restaurée + start
            srv._preview_active = None
            srv.engine.requeue(qa["real_queue"], start=True)
            srv.save()
            return {"ok": True}
        srv.engine.skip()
        return {"ok": True}

    @app.post("/api/preview")
    async def api_preview(req: Request):
        b = await _body(req)
        p = b.get("path")
        if not isinstance(p, str) or not p:
            return err("path attendu")
        if not os.path.exists(p):
            return err("fichier introuvable")
        if not srv._in_roots(p):
            return err("fichier hors bibliothèque")
        qa = srv._preview_active        # re-audition : realQueue et was_playing inchangés
        if qa:
            real, was = list(qa["real_queue"]), qa.get("was_playing", False)
        else:
            s0 = srv.engine.status()
            real, was = list(s0["next"]), s0["playing"]
        srv._preview_active = {"path": p, "real_queue": real, "was_playing": was}
        srv.save()
        srv.engine.preview(p)           # l'audition suspend la file moteur elle-même
        return {"ok": True}

    @app.post("/api/preview/stop")
    async def api_preview_stop(req: Request):
        """Fin d'audition : file restaurée, moteur relancé seulement s'il jouait avant."""
        await _body(req)
        qa = srv._preview_active
        if not qa:
            return {"ok": True, "started": False, "queue": len(srv.engine.status()["next"])}
        started = bool(qa.get("was_playing", False))
        srv._preview_active = None
        srv.engine.requeue(qa["real_queue"], start=started, cut=True)
        srv.save()
        return {"ok": True, "started": started, "queue": len(qa["real_queue"])}

    @app.post("/api/duck")
    async def api_duck(req: Request):
        await _body(req)
        s = srv.engine.status()
        srv.engine.set_duck(not s["ducking"])
        return {"ducking": not s["ducking"]}

    @app.post("/api/volume")
    async def api_volume(req: Request):
        b = await _body(req)
        v = b.get("value")
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0.0 <= v <= 1.0:
            return err("value 0..1 attendu")
        v = float(v)
        srv.engine.set_volume(v)
        srv.save()
        return {"volume": v}

    @app.post("/api/settings")
    async def api_settings(req: Request):
        b = await _body(req)
        out = {}
        if "crossfade" in b:
            x = b.get("crossfade")
            if not isinstance(x, (int, float)) or isinstance(x, bool):
                return err("crossfade 2..16 attendu")
            x = min(max(float(x), 2.0), 16.0)
            srv.engine.set_crossfade(x)
            out["crossfade"] = x
        if "crossfade_curve" in b:
            c = b.get("crossfade_curve")
            if c not in st.XF_CURVES:
                return err("crossfade_curve invalide")
            srv.engine.set_crossfade_curve(c)
            out["crossfade_curve"] = c
        if "auto_queue" in b:
            if not isinstance(b.get("auto_queue"), dict):
                return err("auto_queue: objet attendu")
            srv._auto = st.norm_auto_queue(b["auto_queue"], srv._auto)   # fusion partielle
            out["auto_queue"] = dict(srv._auto)
        if not out:
            return err("crossfade, crossfade_curve ou auto_queue attendu")
        srv.save()
        if "auto_queue" in out:
            autofill()                      # activé -> remplit tout de suite si sous le seuil
        return out

    @app.post("/api/queue")
    async def api_queue(req: Request):
        b = await _body(req)
        p = b.get("paths")
        if not isinstance(p, list):
            return err("paths: liste de chemins attendue")
        paths = [str(x) for x in p]
        # rows pré-chargées (titles affichés d'emblée) : ffprobe possible -> threadpool,
        # jamais l'event loop (40 chemins hors banque = 1,6 s de boucle bloquée).
        await run_in_threadpool(lambda: [srv._track(q) for q in paths])
        put_queue(paths)
        srv.save()
        return {"ok": True}

    @app.post("/api/queue/auto")
    async def api_queue_auto(req: Request):
        """File auto : ajoute (ou remplace la file à venir) `count` morceaux de la banque."""
        b = await _body(req)
        c = b.get("count", 10)
        if isinstance(c, bool) or not isinstance(c, (int, float)):
            return err("count 1..50 attendu")
        n = int(min(max(float(c), 1), 50))
        replace = b.get("replace") is True
        cur = qbase()                               # file à venir = hors morceau en cours
        skip = () if replace else cur               # replace : on repart de toute la banque
        new = build_queue(srv.bank_rows(), n, queue_paths=skip, history=srv.history())
        if not new:
            return err("aucun morceau disponible (banque vide ou tout est déjà en file)")
        await run_in_threadpool(lambda: [srv._track(q) for q in new])
        put_queue(new if replace else cur + new)
        srv.save()
        return {"ok": True, "added": len(new), "queue": len(new if replace else cur + new)}

    @app.post("/api/queue/insert-next")
    async def api_insert(req: Request):
        b = await _body(req)
        p = b.get("path")
        if not isinstance(p, str) or not p:
            return err("path attendu")
        nl = [p] + qbase()
        put_queue(nl)
        await run_in_threadpool(lambda: [srv._track(q) for q in nl])
        srv.save()
        return {"ok": True}

    @app.post("/api/queue/remove")
    async def api_remove(req: Request):
        b = await _body(req)
        p = b.get("path")
        if not isinstance(p, str) or not p:
            return err("path attendu")
        nl = [q for q in qbase() if q != p]
        put_queue(nl)
        srv.save()
        autofill(nl)                    # retirer un morceau peut passer sous le seuil
        return {"ok": True}

    @app.post("/api/import")
    async def api_import(req: Request):
        try:
            raw = await req.body()
        except Exception:
            return err("corps illisible")
        files = _multipart_files(raw, req.headers.get("content-type", ""))
        if not files:
            return err("multipart/form-data attendu (champ files)")
        added = await run_in_threadpool(srv.import_files, files)   # I/O longue : threadpool
        if isinstance(added, str):
            return err(added)
        return {"ok": True, **added}

    def forget(gone: set) -> list[str]:
        """Purge cues/banque/historique/file/audition des chemins réels supprimés."""
        if gone:
            try:                                        # cues des fichiers partis (tolérant)
                cf = os.path.join(srv._data, "cues.json")
                with open(cf) as f:
                    cues = json.load(f)
                if isinstance(cues, dict):
                    keep = {k: v for k, v in cues.items() if os.path.realpath(k) not in gone}
                    if len(keep) != len(cues):
                        tmp = cf + ".tmp"
                        with open(tmp, "w") as f:
                            json.dump(keep, f)
                        os.replace(tmp, cf)
            except Exception:
                pass                                    # absent/corrompu : rien à purger
            with srv._lock:
                srv._bank = {p: r for p, r in srv._bank.items()
                             if os.path.realpath(p) not in gone}
                srv._rows = {p: r for p, r in srv._rows.items()
                             if os.path.realpath(p) not in gone}
                srv._history = [p for p in srv._history if os.path.realpath(p) not in gone]
        s = srv.engine.status()
        qa = srv._preview_active
        if qa:                                      # audition en cours : on ne la casse pas
            real = [p for p in qa["real_queue"] if os.path.realpath(p) not in gone]
            qa["real_queue"] = real
            if os.path.realpath(qa["path"]) in gone:
                srv._preview_active = None
                srv.engine.requeue(real, start=qa.get("was_playing", False), cut=True)
            return real
        nxt = [p for p in s["next"] if os.path.realpath(p) not in gone]
        if nxt != s["next"]:
            put_queue(nxt)
        if s["path"] and os.path.realpath(s["path"]) in gone:
            srv.engine.skip()                       # piste courante supprimée : le moteur enchaîne
        return nxt

    @app.post("/api/bank/clear")
    async def api_bank_clear(req: Request):
        """Vide downloads/ (imports + téléchargements) ; la musique perso n'est jamais touchée."""
        b = await _body(req)
        scope = b.get("scope", "downloads")
        if scope != "downloads":
            return err("scope invalide (downloads)")
        if any(j["status"] in ("queued", "running") for j in srv.dl.jobs()):
            return err("téléchargement en cours", 409)
        root = os.path.realpath(srv._downloads)
        gone, removed = set(), 0
        for dirpath, _dirs, files in os.walk(root):
            for f in files:                         # dossiers conservés, fichiers seuls
                p = os.path.join(dirpath, f)
                rp = os.path.realpath(p)
                if not rp.startswith(root + os.sep):
                    continue                        # jamais hors de downloads/ (lien symbolique)
                try:
                    os.unlink(p)
                except OSError:
                    continue                        # fichier verrouillé : reste en banque
                gone.add(rp)
                removed += 1
        nxt = forget(gone)
        srv._scan_async([srv._downloads])           # comme la fin d'un job de téléchargement
        srv.save()
        return {"ok": True, "removed": removed,
                "bank": len(srv.bank_rows()), "queue": len(nxt)}

    @app.post("/api/bank/remove")
    async def api_bank_remove(req: Request):
        """Supprime UN import/téléchargement (downloads/ seulement), références purgées.

        La musique perso est en lecture seule : tout chemin hors de downloads/ est refusé.
        Fichier déjà absent : on purge quand même les références (banque/file/historique).
        """
        b = await _body(req)
        p = b.get("path")
        if not isinstance(p, str) or not p.strip():
            return err("path attendu")
        root = os.path.realpath(srv._downloads)
        rp = os.path.realpath(p)
        if not rp.startswith(root + os.sep):
            return err("seuls les imports/téléchargements sont supprimables", 403)
        removed = 0
        if os.path.exists(rp):
            try:
                os.unlink(rp)
                removed = 1
            except OSError as exc:                  # verrouillé (Windows : piste en lecture)
                return err("suppression impossible : " + type(exc).__name__, 409)
        nxt = forget({rp})
        srv.save()
        return {"ok": True, "removed": removed,
                "bank": len(srv.bank_rows()), "queue": len(nxt)}

    @app.post("/api/download")
    async def api_download(req: Request):
        b = await _body(req)
        url = b.get("url")
        if not isinstance(url, str) or not url.strip():
            return err("url attendue")
        u = urllib.parse.urlparse(url.strip())
        if u.scheme not in ("http", "https") or not u.netloc:
            return err("url invalide (http/https attendu)")
        jid = srv.dl.start(url.strip())
        return {"id": jid}

    @app.get("/api/waveform")
    def api_waveform(path: str = ""):
        # Seulement les fichiers de la bibliothèque : pas de décodage ffmpeg d'un
        # chemin arbitraire fourni par le client (realpath vérifié).
        if not path or not srv._in_roots(path) or not os.path.exists(path):
            return []                                       # le front affiche sa barre plate
        try:
            return waveform(path, 400, data_dir=srv._data)  # sync : anyio threadpool, pas l'event loop
        except Exception:
            return []

    @app.post("/api/restore")
    async def api_restore(req: Request):
        b = await _body(req)
        conf = bool(b.get("confirm"))
        offer = srv._offer
        if not offer:
            # Aucune offre (déjà consommée, ou rien à reprendre) : ne RIEN réinitialiser.
            # Avant, un POST tardif écrasait les réglages live (crossfade/volume/auto).
            srv._resolved = True
            return {"ok": False, "error": "no_offer"}
        srv._offer = None
        srv._resolved = True                                # l'autosave remplace state.json dès maintenant
        sett = (offer or {}).get("settings") or {}
        srv.engine.set_crossfade(sett.get("crossfade", 8.0))
        srv.engine.set_crossfade_curve(sett.get("crossfade_curve", st.XF_DEFAULT))
        srv.engine.set_volume(sett.get("volume", 1.0))
        srv.engine.set_duck(bool(sett.get("ducking", False)))
        srv._auto = st.norm_auto_queue(sett.get("auto_queue"))
        if conf and offer and isinstance(offer.get("playing"), dict) and offer["playing"].get("path"):
            path_p = offer["playing"]["path"]
            paths = [path_p] + [p for p in offer.get("queue") or [] if isinstance(p, str)]
            srv.engine.set_queue(paths)
            try:
                srv.engine.set_position(float(offer["playing"].get("pos") or 0.0))
            except (TypeError, ValueError):
                pass
            srv.save()
            return {"ok": True}
        # « Nouvelle session » : on ne reprend pas la piste en cours, mais la file sauvée
        # est conservée — sinon l'autosave qui suit écrivait [] et perdait la file.
        q = [p for p in (offer or {}).get("queue") or [] if isinstance(p, str)]
        if q:
            srv.engine.set_queue(q)

            def _wait_taken():                          # la file passe par la queue du mixeur
                t0 = time.monotonic()
                while srv.engine.status()["next"] != q and time.monotonic() - t0 < 1.0:
                    time.sleep(0.005)

            await run_in_threadpool(_wait_taken)        # jamais l'event loop (jusqu'à 1 s)
        srv.save()
        return {"ok": False, "error": None}

    _quit = {"done": False}                     # idempotence de /api/quit (1 seul arrêt)
    _quit_lock = threading.Lock()

    @app.post("/api/quit")
    async def api_quit(req: Request):
        """Sauve puis coupe le process ~250 ms après la réponse (le JSON part d'abord)."""
        await _body(req)
        srv.save()

        def _bye():
            with _quit_lock:
                if _quit["done"]:
                    return
                _quit["done"] = True
            try:
                os.unlink(os.path.join(srv._data, "server.pid"))
            except OSError:
                pass
            try:
                srv._shutdown()
            except Exception:
                pass
            _hard_exit(0)

        t = threading.Timer(0.25, _bye)
        t.daemon = True
        t.start()
        return {"ok": True}

    @app.get("/api/events")
    async def api_events():
        conn = _Conn(asyncio.get_running_loop())
        srv._conns.add(conn)

        async def gen():
            # ponytail: tick pos ~4 Hz cadencé par le timeout de la même boucle (pas de thread dédié)
            try:
                yield _sse(None, {"type": "tick"})          # hello immédiat (réveille proxys bufferisés)
                hb = 0.0
                while True:
                    try:
                        item = await asyncio.wait_for(conn.q.get(), 0.25)
                        if item is None:
                            break
                        yield item
                        continue
                    except asyncio.TimeoutError:
                        pass
                    s = srv.engine.status()
                    if s["playing"] and s["path"] is not None:   # ~4 Hz pendant la lecture
                        yield _sse("pos", {"type": "pos", "path": s["path"],
                                           "pos": s["pos"], "dur": s["dur"]})
                        hb = 0.0
                    else:
                        hb += 0.25
                        if hb >= 15.0:                      # heartbeat : casse les proxys inactifs
                            yield _sse(None, {"type": "tick"})
                            hb = 0.0
            finally:
                srv._conns.discard(conn)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no",
                                          "Connection": "keep-alive"})

    # Cache-bust : index.html servi à la main, assets réécrits ?v=<mtime mtime>.
    # Idempotent (l'ancien ?v= est réécrit, jamais doublé). Recompute par requête :
    # ~0 coût (3 getmtime + 1 lecture) — pas de cache à invalider.
    _ASSET = re.compile(r'(?P<pre>(?:href|src)="(?:\./|/)?)(?P<url>style\.css|app\.js)(?P<post>\?v=\d+)?"')

    @app.get("/")
    def ui_index():
        try:
            html = (STATIC / "index.html").read_text(encoding="utf-8")
        except OSError:
            return JSONResponse({"error": "UI absente"}, status_code=404)

        def bust(m):
            try:
                mt = int(os.path.getmtime(STATIC / m.group("url")))
            except OSError:
                return m.group(0)                      # asset parti : ref telle quelle
            return f'{m.group("pre")}{m.group("url")}?v={mt}"'

        return HTMLResponse(_ASSET.sub(bust, html))

    @app.get("/style.css")
    def ui_css():
        return FileResponse(STATIC / "style.css", media_type="text/css")

    @app.get("/app.js")
    def ui_js():
        return FileResponse(STATIC / "app.js", media_type="application/javascript")

    app.mount("/static", StaticFiles(directory=STATIC, check_dir=False), name="ui")
    return app


def _music_dirs(cli_dirs, downloads):
    """--music-dir > AUTODJ_MUSIC_DIR (os.pathsep) > ~/Music, ./music s'ils existent.

    `downloads` est toujours scanné (en dernier). Aucun dossier créé ici : un chemin
    absent est simplement ignoré par le scan (seul downloads/ est créé par main()).
    """
    if cli_dirs:
        dirs = list(cli_dirs)
    else:
        dirs = [p for p in os.environ.get("AUTODJ_MUSIC_DIR", "").split(os.pathsep) if p]
    if not dirs:
        dirs = [d for d in (os.path.expanduser("~/Music"), "music") if os.path.isdir(d)]
    return list(dict.fromkeys([os.path.expanduser(d) for d in dirs] + [downloads]))


def main():
    ap = argparse.ArgumentParser(description="Auto-DJ — serveur UI/API local")
    ap.add_argument("--port", type=int, default=8092)
    ap.add_argument("--music-dir", action="append",        # répétable
                    help="dossier musique (défaut : $AUTODJ_MUSIC_DIR, ~/Music, ./music)")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--downloads", default="downloads")
    a = ap.parse_args()
    dirs = _music_dirs(a.music_dir, a.downloads)
    os.makedirs(a.downloads, exist_ok=True)
    try:
        app = build_app(dirs, a.downloads, a.data_dir)
    except OSError as exc:                 # device audio absent : message net, pas de stack
        raise SystemExit(f"Auto-DJ : démarrage impossible — {exc}")
    print(f"Auto-DJ : banques {dirs}", flush=True)
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
