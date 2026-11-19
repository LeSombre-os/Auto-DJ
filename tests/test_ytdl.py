"""Tests headless du downloader — stubs injectés via run_cmd, zéro réseau.

Les stubs Popen des vrais sous-process locaux (`python3 -c`) qui meurent vite,
créent un fichier (comme yt-dlp) ou dorment (pour le watchdog). Le vrai binôme
yt-dlp n'est JAMAIS appelé ici.
"""
import os, subprocess, sys, tempfile, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import app.ytdl as ytdl
from app.ytdl import Downloader


def reader():                     # collecteur d'events, signature (name, data) -> (n,d) tuple
    evs = []
    return evs, lambda n, d: evs.append((n, dict(d)))


def _err_run(url, dest, errf):
    """Job qui meurt vite avec une ligne sur stderr (comme yt-dlp sur une URL refusée)."""
    return subprocess.Popen([sys.executable, "-c",
        "import sys; print('boom', file=sys.stderr); sys.exit(3)"],
        stdout=subprocess.DEVNULL, stderr=errf)


def _ok_run(url, dest, errf):
    """Crée un fichier dans dest (comme yt-dlp) puis finit vite."""
    return subprocess.Popen([sys.executable, "-c",
        "import sys, time; open(sys.argv[1], 'w').close(); time.sleep(0.05)",
        os.path.join(dest, "Song-x1y2z3.webm")],
        stdout=subprocess.DEVNULL, stderr=errf)


def _slow_run(url, dest, errf):
    """Job long : sert à observer queued/running puis au test watchdog."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL, stderr=errf)


def wait_status(d, jid, *st, t=10.0):
    j, end = {}, time.monotonic() + t
    while time.monotonic() < end:
        jobs = [x for x in d.jobs() if x["id"] == jid]
        j = jobs[0] if jobs else {}
        if j.get("status") in st:
            return j
        time.sleep(0.05)
    pytest.fail(f"status {st} non atteint : {j}")


# 1. ids uniques + jobs() visible en queued/running, champs = contrat
def test_ids_unique_et_status_initial():
    d = Downloader(tempfile.mkdtemp(), lambda *a: None, run_cmd=_slow_run)
    try:
        i1, i2 = d.start("https://youtu.be/aaaa"), d.start("https://youtu.be/bbbb")
        assert i1 != i2
        end = time.monotonic() + 3
        while time.monotonic() < end:
            if any(j["status"] in ("queued", "running") for j in d.jobs()):
                break
            time.sleep(0.05)
        jobs = d.jobs()
        assert len(jobs) == 2
        assert any(j["status"] in ("queued", "running") for j in jobs)
        for j in jobs:
            assert set(j) <= {"id", "url", "title", "status", "file", "err"}
            assert j["file"] is None and j["title"].startswith("https://")
    finally:
        d.shutdown()


# 2. lifecycle error : event download_error, dernière ligne stderr (1 ligne)
def test_erreur_propre():
    evs, cb = reader()
    d = Downloader(tempfile.mkdtemp(), cb, run_cmd=_err_run)
    try:
        jid = d.start("https://youtu.be/xyz")
        j = wait_status(d, jid, "error")
        assert j["file"] is None and j["title"] == "https://youtu.be/xyz"
        assert j["err"] == "boom"
        assert ("download_error", {"id": jid}) in evs
    finally:
        d.shutdown()


# 3. lifecycle done : fichier admis par diff, event download_ready {"file","title"}
def test_done_et_event_ready():
    evs, cb = reader()
    dest = tempfile.mkdtemp()
    d = Downloader(dest, cb, run_cmd=_ok_run)
    try:
        jid = d.start("https://youtu.be/xyz")
        j = wait_status(d, jid, "done")
        f = os.path.join(dest, "Song-x1y2z3.webm")
        assert j["file"] == f and j["title"] == "Song-x1y2z3"
        assert os.path.isfile(f)
        assert ("download_ready", {"file": f, "title": "Song-x1y2z3"}) in evs
    finally:
        d.shutdown()


# 4. URL bizarre : start() ne lève rien, le job échoue proprement
def test_url_bizarre_ne_leve_rien():
    evs, cb = reader()
    d = Downloader(tempfile.mkdtemp(), cb, run_cmd=_err_run)
    try:
        jid = d.start("not a url")
        assert isinstance(jid, str) and jid
        _ = wait_status(d, jid, "error")
        assert ("download_error", {"id": jid}) in evs
    finally:
        d.shutdown()


# 5. watchdog hard timeout (TIMEOUT court injecté, job sleep 30 -> error)
def test_timeout_hard(monkeypatch):
    monkeypatch.setattr(ytdl, "TIMEOUT", 0.2)
    d = Downloader(tempfile.mkdtemp(), lambda *a: None, run_cmd=_slow_run)
    try:
        jid = d.start("u")
        j = wait_status(d, jid, "error", t=8)
        assert "timeout" in j["err"] and j["file"] is None
    finally:
        d.shutdown()


# 6. même URL déjà en cours : start() ne lance pas un 2e téléchargement
def test_meme_url_pas_de_double_telechargement():
    calls = []

    def run(url, dest, errf):
        calls.append(url)
        return _slow_run(url, dest, errf)

    d = Downloader(tempfile.mkdtemp(), lambda *a: None, run_cmd=run)
    try:
        i1 = d.start("https://youtu.be/double")
        i2 = d.start("https://youtu.be/double")
        assert i1 == i2 and len(d.jobs()) == 1
        time.sleep(0.2)
        assert calls == ["https://youtu.be/double"]      # un seul sous-process
    finally:
        d.shutdown()


# 7. la commande contrat exécutée par _real_run reste figée — offline (shape, sans exec)
def test_commande_contrat():
    dest = "/dest/fictive"
    assert ytdl._cmd("https://youtu.be/abc", dest) == [
        sys.executable, "-m", "yt_dlp", "-f", "bestaudio/best",
        "--no-playlist", "--no-part", "-o",
        os.path.join(dest, "%(title)s-%(id)s.%(ext)s"), "https://youtu.be/abc"]
