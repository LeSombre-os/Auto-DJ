"""Téléchargement Seamless : yt-dlp en sous-process, max 2 jobs, jamais le thread serveur.

Le fichier produit est repéré par diff des fichiers de dest_dir avant/après le
sous-process (ponytail: --no-part + gabarit %(title)s-%(id)s => le job laisse
exactement un fichier ; si un AUTRE job finit dans la même fenêtre, on prend le
plus récent — attribution exacte exigerait un parse du stdout, Version 2).
stderr -> fichier temp (jamais un pipe) : aucun deadlock possible ; attente via
`proc.wait(timeout<=POLL)` + deadline -> la détection est quasi instantanée.
"""
import os, subprocess, sys, tempfile, threading, time, uuid
from concurrent.futures import ThreadPoolExecutor

TIMEOUT = 600.0                     # hard timeout par job (10 min)
POLL = 0.25                         # grain du watchdog (s)
_FIELDS = ("id", "url", "title", "status", "file")   # contrat jobs()


def _cmd(url: str, dest_dir: str) -> list[str]:
    """Commande du contrat (figée, test offline + sanity console)."""
    return [sys.executable or "python3", "-m", "yt_dlp", "-f", "bestaudio/best",
            "--no-playlist", "--no-part", "-o",
            os.path.join(dest_dir, "%(title)s-%(id)s.%(ext)s"), url]


def _real_run(url: str, dest_dir: str, stderr):
    """Lancement réel (venv) ; stderr -> fichier."""
    return subprocess.Popen(_cmd(url, dest_dir), stdout=subprocess.DEVNULL, stderr=stderr)


def _last_err(errf) -> str:
    """Dernière ligne non vide du stderr (1 ligne, min pour l'UI)."""
    try:
        errf.seek(0)
        txt = errf.read().decode("utf-8", "replace")
    except Exception:
        return ""
    for line in reversed(txt.splitlines()):
        if line.strip():
            return line.strip()[:250]
    return ""


class Downloader:
    """Queue de téléchargements. events_cb(event:str, data:dict), appelé depuis le worker."""

    def __init__(self, dest_dir: str, events_cb, run_cmd=None):
        self._dest = os.path.abspath(dest_dir)
        self._events = events_cb
        self._run = run_cmd or _real_run        # injection test (offline, sans réseau)
        self._jobs: dict = {}
        self._lock = threading.RLock()          # réentrant : _fail->_set NUnc (deadlock sinon)
        self._stopping = False
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ytdl")
        try:
            os.makedirs(self._dest, exist_ok=True)
        except Exception:
            pass                                # job -> "error" au moment du diff

    def start(self, url: str) -> str:
        with self._lock:
            for j in self._jobs.values():           # même URL déjà en cours : pas de 2e téléchargement
                if j["url"] == url and j["status"] in ("queued", "running"):
                    return j["id"]
            jid = uuid.uuid4().hex[:12]
            self._jobs[jid] = {"id": jid, "url": url, "title": url,
                               "status": "queued", "file": None}
            stopping = self._stopping
        if stopping:
            self._fail(jid, "aborted")
        else:
            self._pool.submit(self._worker, jid)     # queue illimitée : start() ne bloque jamais
        return jid

    def jobs(self) -> list[dict]:
        with self._lock:
            out = []
            for j in self._jobs.values():
                d = {k: j[k] for k in _FIELDS}
                if j.get("err"):
                    d["err"] = j["err"]             # clé additionnelle (UI) : 1 ligne stderr
                out.append(d)
        return out

    def shutdown(self):
        """Arrêt global : kill des procs en cours, threads pool libérés, join filet 2 s."""
        with self._lock:
            self._stopping = True
            for j in self._jobs.values():
                if j.get("status") == "queued":
                    j.update(status="error", err="aborted")
                p = j.get("proc")
                if p:
                    try: p.kill()
                    except Exception: pass
        # wait=True : TPE réveille ses threads idle (ils pègent Ques bloqués en
        # _work_queue.get) ; busy finissent vite (procs déjà killés) -> ~0 ms.
        self._pool.shutdown(wait=True, cancel_futures=True)
        for t in threading.enumerate():          # filet contractuel ; instantané en pratique
            if t.name.startswith("ytdl_"):
                t.join(2.0)

    # ---------------- worker ------------------------------------------
    def _worker(self, jid: str):
        self._set(jid, status="running")
        errf = tempfile.TemporaryFile()
        try:
            before = set(os.listdir(self._dest))     # OSError ici/allé -> except -> "error"
            with self._lock:                         # lock tenu pendant l'ouverture (~10 ms) :
                if self._stopping:                   # shutdown() ne peut pas manquer le proc
                    self._fail(jid, "aborted")
                    return
                proc = self._run(self._jobs[jid]["url"], self._dest, errf)
                self._jobs[jid].update(proc=proc, status="running")
            deadline = time.monotonic() + TIMEOUT
            while True:                        # attente Popen + watchdog hard : sleep(0) fixe
                try:                           # stderr = fichier => rien à lire, pas de blocage
                    proc.wait(timeout=min(max(deadline - time.monotonic(), 0.0), POLL))
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() <= deadline:
                        continue
                    proc.kill()
                    try: proc.wait(timeout=10)
                    except Exception: pass
                    self._fail(jid, f"timeout: yt-dlp > {TIMEOUT:.0f} s")
                    return
            when = lambda f: (                        # mtime tolérant (dest supprimée en vol)
                os.path.getmtime(os.path.join(self._dest, f)) if
                os.path.exists(os.path.join(self._dest, f)) else 0.0)
            new = sorted(set(os.listdir(self._dest)) - before, key=when)
            if proc.returncode == 0 and new:
                f = os.path.join(self._dest, new[-1])            # plus récent
                title = os.path.splitext(new[-1])[0]              # = basename sans extension
                self._set(jid, status="done", file=f, title=title)
                self._ev("download_ready", {"file": f, "title": title})
            else:
                self._fail(jid, _last_err(errf) or f"yt-dlp exit {proc.returncode}")
        except Exception as e:
            self._fail(jid, _last_err(errf) or f"{type(e).__name__}: {e}")
        finally:
            try: errf.close()
            except Exception: pass

    def _fail(self, jid: str, err: str):
        self._set(jid, status="error", err=str(err)[:250])
        self._ev("download_error", {"id": jid})

    def _set(self, jid: str, **kw):
        with self._lock:
            j = self._jobs.get(jid)
            if j:
                j.update(kw)

    def _ev(self, name: str, data: dict):
        try: self._events(name, data)
        except Exception: pass
