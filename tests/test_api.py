"""Tests API serveur S5 — headless : DummyOut, sans réseau, sans device.

Harnais ASGI minimal (stdlib) pour les appels JSON ; TestClient starlette (httpx,
requirements.txt) pour le multipart de /api/import.
"""
import asyncio
import json
import os
import sys
import threading
import time
import urllib.parse
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from app.server import build_app, Srv
from fastapi.testclient import TestClient


# ---------------- sorties / wav (aucun device) --------------------------------
class DummyOut:
    """write() ne bloque pas (rendu accéléré) ; pause micro pour éviter 100% CPU."""

    rate = 48000

    def __init__(self, pace=0.0005):
        self.total = 0
        self.closed = False
        self.pace = pace

    def write(self, b):
        self.total += b.shape[0]
        time.sleep(self.pace)

    def close(self):
        self.closed = True


def make_wav(path, dur=3.0, sr=48000, f=440.0, amp=0.4):
    n = int(dur * sr)
    s = (amp * np.sin(2 * np.pi * f * np.arange(n) / sr) * 32000).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(s.tobytes())


def app_for(tmp_path, files=1, pace=0.0005, dur=3.0):
    mdir = tmp_path / "m"
    mdir.mkdir(exist_ok=True)
    paths = []
    for name in "abc"[:files]:
        p = mdir / f"{name}.wav"
        make_wav(p, dur=dur)
        paths.append(str(p))
    ddir = tmp_path / "downloads"
    ddir.mkdir(exist_ok=True)
    dfl = ddir / "in_dls.wav"                        # pré-existant : vu par le scan boot
    make_wav(dfl)
    app = build_app([str(mdir)], str(ddir), str(tmp_path / "data"),
                    out_factory=lambda r: DummyOut(pace=pace))
    return app, paths, mdir, ddir, str(dfl)


def wait_for(cond, timeout=6.0, step=0.05):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if cond():
                return True
        except Exception:
            pass
        time.sleep(step)
    return False


# ---------------- harnais ASGI minimal (sans httpx) ----------------------------
def api(app, method, target, body=None):
    """1 requête HTTP brute sur l'app ASGI : body -> http.request ; puis disconnect
    (StreamingResponse s'arrête proprement ; réponse JSON déjà émise)."""

    async def run():
        p, _, qs = target.partition("?")
        headers, braw = [], b""
        if body is not None:
            braw = json.dumps(body).encode()
            headers = [(b"content-type", b"application/json"),
                       (b"content-length", str(len(braw)).encode())]
        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1", "method": method, "scheme": "http",
            "path": p, "raw_path": p.encode(), "query_string": qs.encode(),
            "root_path": "", "headers": headers,
            "client": ("test", 0), "server": ("test", 80), "state": {},
        }
        status = {"v": None}
        buf = bytearray()
        n = {"c": 0}

        async def receive():
            n["c"] += 1
            if n["c"] == 1:
                return {"type": "http.request", "body": braw, "more_body": False}
            await asyncio.sleep(0.05)     # marge : laisse passer le 1er chunk du stream
            return {"type": "http.disconnect"}

        async def send(m):
            if m["type"] == "http.response.start":
                status["v"] = m["status"]
            elif m["type"] == "http.response.body":
                buf.extend(m.get("body", b""))

        await app(scope, receive, send)
        return status["v"], bytes(buf)

    return asyncio.run(run())


def js(app, method, target, body=None):
    status, data = api(app, method, target, body)
    try:
        parsed = json.loads(data) if data else None
    except Exception:
        parsed = None
    return status, parsed


def qpaths(s):
    return [t["path"] for t in s["queue"]]


def settings_of(app):
    return (js(app, "GET", "/api/state")[1] or {}).get("settings", {})


def srv_of(app):
    """Instance Srv derrière build_app (closure de l'endpoint /api/state)."""
    ep = next(r.endpoint for r in app.routes if getattr(r, "path", "") == "/api/state")
    return next(c.cell_contents for c in (ep.__closure__ or ())
                if isinstance(c.cell_contents, Srv))


def boot_app(tmp_path, state_fn, names=("a", "b"), pace=0.0005):
    """App construite APRÈS l'écriture de state.json -> boot réel (aucune course d'autosave).
    `state_fn(paths)` reçoit les chemins créés et rend le dict à sauver."""
    from app import state as stmod
    mdir = tmp_path / "m"
    mdir.mkdir(exist_ok=True)
    paths = []
    for name in names:
        p = mdir / f"{name}.wav"
        make_wav(p)
        paths.append(str(p))
    ddir = tmp_path / "downloads"
    ddir.mkdir(exist_ok=True)
    stmod.save(state_fn(paths), str(tmp_path / "data"))
    app = build_app([str(mdir)], str(ddir), str(tmp_path / "data"),
                    out_factory=lambda r: DummyOut(pace=pace))
    return app, paths


# ---------------- tests ---------------------------------------------------------
def test_state_shape(tmp_path):
    app, (pa,), mdir, ddir, dfl = app_for(tmp_path)
    assert wait_for(lambda: len((js(app, "GET", "/api/state")[1] or {})
                                .get("bank", [])) >= 2), "scan de la banque"
    s = js(app, "GET", "/api/state")[1]
    assert isinstance(s, dict)
    assert set(("playing", "queue", "bank", "downloading", "settings", "restore_offer")) <= set(s)
    assert s["playing"] is None                      # rien de lancé -> tranche null
    assert s["downloading"] == []                    # -> liste vide
    assert set(s["settings"]) == {"crossfade", "crossfade_curve", "volume",
                                  "ducking", "auto_queue"}
    assert s["settings"]["crossfade_curve"] == "equal_power"
    assert s["settings"]["auto_queue"] == {"enabled": False, "min": 3, "fill": 10}
    assert s["history"] == []                        # rien joué -> historique vide
    row = next(r for r in s["bank"] if r["path"] == pa)
    assert set(row) == {"path", "title", "artist", "dur", "source"}
    assert row["dur"] > 2.0 and row["source"] == "music" and row["title"] == "a"
    assert next(r for r in s["bank"] if r["path"] == dfl)["source"] == "downloads"
    st, data = api(app, "GET", "/")                  # cache-bust : assets versionnés
    assert st == 200 and b'style.css?v=' in data and b'app.js?v=' in data


def test_queue_roundtrip(tmp_path):
    app, (pa, pb, pc), mdir, ddir, dfl = app_for(tmp_path, files=3)
    st, r = js(app, "POST", "/api/queue", {"paths": [pa, pb]})
    assert st == 200
    assert wait_for(lambda: qpaths(js(app, "GET", "/api/state")[1]) == [pa, pb])
    st, r = js(app, "POST", "/api/queue/insert-next", {"path": pc})
    assert st == 200
    assert wait_for(lambda: qpaths(js(app, "GET", "/api/state")[1]) == [pc, pa, pb])
    st, r = js(app, "POST", "/api/queue/remove", {"path": pb})
    assert wait_for(lambda: qpaths(js(app, "GET", "/api/state")[1]) == [pc, pa])
    st, r = js(app, "POST", "/api/queue", {"paths": "oops"})
    assert st == 400 and isinstance(r, dict) and "error" in r


def test_volume_settings_clamped(tmp_path):
    app, _, _, _, _ = app_for(tmp_path, files=1)
    st, r = js(app, "POST", "/api/volume", {"value": 5})
    assert st == 400 and "error" in r
    st, r = js(app, "POST", "/api/volume", {"value": 0.35})
    assert st == 200
    assert wait_for(lambda: settings_of(app).get("volume") == 0.35)
    st, r = js(app, "POST", "/api/settings", {"crossfade": "x"})
    assert st == 400 and "error" in r
    js(app, "POST", "/api/settings", {"crossfade": 20})
    assert wait_for(lambda: settings_of(app).get("crossfade") == 16)
    js(app, "POST", "/api/settings", {"crossfade": 1})
    assert wait_for(lambda: settings_of(app).get("crossfade") == 2)
    js(app, "POST", "/api/settings", {"crossfade": 2})
    assert wait_for(lambda: settings_of(app).get("crossfade") == 2)
    js(app, "POST", "/api/settings", {"crossfade": 16})
    assert wait_for(lambda: settings_of(app).get("crossfade") == 16)


def test_play_right_after_queue_not_empty_error(tmp_path):
    """POST /api/queue puis POST /api/play : la commande file n'est pas encore consommée
    par le mixeur -> le garde-fou relit l'état avant de répondre « file vide »."""
    app, paths, *_ = app_for(tmp_path, files=2)
    srv = srv_of(app)
    real = srv.engine.status
    calls = {"n": 0}
    empty = {"playing": False, "path": None, "pos": 0.0, "dur": 0.0, "cue_in": 0.0,
             "cue_out": 0.0, "fading": False, "next": [], "volume": 1.0, "ducking": False,
             "crossfade": 8.0, "crossfade_curve": "equal_power", "preview": False,
             "incoming": None}

    def status():
        calls["n"] += 1
        return dict(empty) if calls["n"] == 1 else real()   # 1er appel : file pas encore appliquée

    js(app, "POST", "/api/queue", {"paths": paths})
    srv.engine.status = status        # installé APRÈS le POST file (qui lit déjà status())
    st, r = js(app, "POST", "/api/play", {})
    assert st == 200 and r == {"ok": True}, (st, r)
    assert calls["n"] >= 2                                   # la relecture a bien eu lieu


def test_play_empty_no_crash(tmp_path):
    app, _, _, _, _ = app_for(tmp_path, files=1)
    st, r = js(app, "POST", "/api/play", {})
    assert st == 200 and isinstance(r, dict) and "error" in r   # pas de crash
    js(app, "POST", "/api/skip", {})
    js(app, "POST", "/api/duck", {})
    js(app, "POST", "/api/queue/remove", {"path": "/nope"})
    st, r = js(app, "GET", "/api/state")
    assert st == 200                                 # serveur toujours debout


def test_download_bad_url(tmp_path):
    app, _paths, _mdir, _ddir, _ = app_for(tmp_path, files=1)
    st, r = js(app, "POST", "/api/download", {"url": ""})
    assert st == 400 and "error" in r
    st, r = js(app, "POST", "/api/download", {"url": "notaurl"})
    assert st == 400 and "error" in r
    st, r = js(app, "POST", "/api/download", {})
    assert st == 400 and "error" in r


def test_waveform(tmp_path):
    app, (pa,), _mdir, _ddir, _dfl = app_for(tmp_path, files=1)
    q = urllib.parse.urlencode({"path": pa})
    st, data = api(app, "GET", "/api/waveform?" + q)
    arr = json.loads(data)
    assert st == 200 and len(arr) == 400             # contrat : list[float], 400 px
    assert all(isinstance(v, float) for v in arr)
    assert 0.0 < max(arr) <= 1.0                     # enveloppe normalisée
    q = urllib.parse.urlencode({"path": str(_mdir) + "/nope.wav"})
    st, data = api(app, "GET", "/api/waveform?" + q)
    assert st == 200 and json.loads(data) == []      # fallback barre plate


def test_restore_no(tmp_path):
    app, (pa, pb), mdir, ddir, _ = app_for(tmp_path, files=2)
    from app import state as stmod
    stmod.save({"queue": [pb], "playing": {"path": pa, "pos": 5.5},
                "settings": {"crossfade": 5.0, "volume": 0.4}},
               str(tmp_path / "data"))
    app2 = build_app([str(mdir)], str(ddir), str(tmp_path / "data"),
                     out_factory=lambda r: DummyOut())
    assert wait_for(lambda: (js(app2, "GET", "/api/state")[1] or {})
                    .get("restore_offer")), "offre de restauration"
    st, r = js(app2, "POST", "/api/restore", {"confirm": False})
    assert st == 200
    s = js(app2, "GET", "/api/state")[1]
    assert s["restore_offer"] is None                # purgée, plus de modale
    assert s["playing"] is None                      # piste non reprise
    # « Nouvelle session » garde la file sauvée : sinon l'autosave écrivait [] (perte réelle).
    assert [t["path"] for t in s["queue"]] == [pb]
    assert s["settings"]["crossfade"] == 5.0
    assert wait_for(lambda: (stmod.load(str(tmp_path / "data")).get("queue") or []) == [pb], 5.0)


def test_restore_yes(tmp_path):
    app, (pa, pb), mdir, ddir, _ = app_for(tmp_path, files=2)
    from app import state as stmod
    stmod.save({"queue": [pb], "playing": {"path": pa, "pos": 5.5},
                "settings": {"crossfade": 5.0, "crossfade_curve": "smooth",
                             "volume": 0.4, "ducking": True}},
               str(tmp_path / "data"))
    app2 = build_app([str(mdir)], str(ddir), str(tmp_path / "data"),
                     out_factory=lambda r: DummyOut())
    assert wait_for(lambda: (js(app2, "GET", "/api/state")[1] or {})
                    .get("restore_offer")), "offre"
    off = js(app2, "GET", "/api/state")[1]["restore_offer"]
    assert off["playing"]["path"] == pa and isinstance(off["playing"]["pos"], float)
    st, r = js(app2, "POST", "/api/restore", {"confirm": True})
    assert st == 200 and r.get("ok")
    assert wait_for(lambda: qpaths(js(app2, "GET", "/api/state")[1]) == [pa, pb])
    s = js(app2, "GET", "/api/state")[1]
    assert s["settings"]["crossfade"] == 5.0 and s["settings"]["volume"] == 0.4
    assert s["settings"]["ducking"] is True
    assert s["settings"]["crossfade_curve"] == "smooth"   # courbe réappliquée par /api/restore
    assert s["playing"] is None                      # RESTÉ en pause (pos appliquée à la prochaine promo)
    assert wait_for(lambda: (js(app2, "GET", "/api/state")[1] or {})
                    .get("restore_offer") is None)
    st, r = js(app2, "POST", "/api/restore", {"confirm": True})
    assert r.get("error") == "no_offer"              # purgée définitivement


def test_boot_restores_queue_without_playing(tmp_path):
    """Boot sans piste en cours : la file ET les réglages sauvés sont rechargés (sans
    démarrer la lecture) ; l'autosave réécrit la même file au lieu de la détruire."""
    app, (pa, pb) = boot_app(tmp_path, lambda p: {
        "queue": list(p), "playing": None,
        "settings": {"crossfade": 12.0, "crossfade_curve": "smooth",
                     "volume": 0.4, "ducking": True}})
    s = js(app, "GET", "/api/state")[1]
    assert qpaths(s) == [pa, pb]                      # file sauvée rechargée
    assert s["playing"] is None                       # ... mais rien ne démarre seul
    assert s["settings"]["crossfade"] == 12.0
    assert s["settings"]["crossfade_curve"] == "smooth"
    assert s["settings"]["volume"] == 0.4
    assert s["settings"]["ducking"] is True
    srv = srv_of(app)
    assert srv.engine.status()["next"] == [pa, pb]    # commande déjà prise par le mixeur
    srv.save()                                        # autosave immédiat : même file
    d = json.load(open(tmp_path / "data" / "state.json"))
    assert d["queue"] == [pa, pb]                     # aucune perte
    assert d["settings"]["crossfade"] == 12.0 and d["settings"]["volume"] == 0.4
    time.sleep(0.2)
    assert js(app, "GET", "/api/state")[1]["playing"] is None   # toujours pas de play


def test_boot_with_offer_keeps_queue_until_accept(tmp_path):
    """Avec une piste existante : offre de restauration -> ni file ni réglages appliqués
    avant la décision ; confirm:true les restaure (chemin d'origine intact)."""
    app, (pa, pb) = boot_app(tmp_path, lambda p: {
        "queue": [p[1]], "playing": {"path": p[0], "pos": 1.0},
        "settings": {"crossfade": 5.0, "crossfade_curve": "smooth", "volume": 0.4}})
    assert wait_for(lambda: (js(app, "GET", "/api/state")[1] or {}).get("restore_offer")), "offre"
    s = js(app, "GET", "/api/state")[1]
    assert qpaths(s) == [] and s["playing"] is None   # rien avant la décision
    assert s["settings"]["crossfade"] == 8.0          # réglages par défaut, pas ceux sauvés
    assert s["settings"]["crossfade_curve"] == "equal_power"
    assert s["settings"]["volume"] == 1.0
    st, r = js(app, "POST", "/api/restore", {"confirm": True})
    assert st == 200 and r.get("ok")
    assert wait_for(lambda: qpaths(js(app, "GET", "/api/state")[1]) == [pa, pb]), "file restaurée"
    s2 = js(app, "GET", "/api/state")[1]
    assert s2["settings"]["crossfade"] == 5.0 and s2["settings"]["crossfade_curve"] == "smooth"
    assert s2["playing"] is None                      # RESTÉ en pause


def test_autosave_mutation(tmp_path):
    app, (pa,), mdir, _, dfl_unused = app_for(tmp_path, files=1)
    js(app, "POST", "/api/queue", {"paths": [pa]})
    js(app, "POST", "/api/volume", {"value": 0.2})
    js(app, "POST", "/api/settings", {"crossfade": 6.5})
    stf = tmp_path / "data" / "state.json"
    assert wait_for(lambda: stf.exists()), "state.json écrit"
    data = json.load(open(stf))
    assert data["queue"] == [pa]                     # mutation -> autosave immédiat
    assert data["settings"] == {"crossfade": 6.5, "crossfade_curve": "equal_power",
                               "volume": 0.2, "ducking": False,
                               "auto_queue": {"enabled": False, "min": 3, "fill": 10}}
    assert data["history"] == []                     # rien joué
    assert data["playing"]["path"] is None           # rien en lecture (SCHEMA normalise le dict)
    assert isinstance(data["saved_at"], str)


def test_sse_first_tick(tmp_path):
    app, _, _, _, _ = app_for(tmp_path, files=1)
    st, data = api(app, "GET", "/api/events")
    assert st == 200
    assert data.startswith(b"data:") and b"tick" in data   # hello immédiat reçu


def test_preview_flow(tmp_path):
    """Audition : flag exposé, file suspendue ; fin naturelle -> restauration au repos ;
    SKIP pendant l'audition -> restauration + moteur relancé (start=True)."""
    app, (pa, pb), mdir, ddir, _dfl = app_for(tmp_path, files=2, pace=0.002, dur=10.0)
    st, r = js(app, "POST", "/api/queue", {"paths": [pa, pb]})
    assert st == 200 and r == {"ok": True}
    assert wait_for(lambda: qpaths(js(app, "GET", "/api/state")[1]) == [pa, pb]), "file visible"
    js(app, "POST", "/api/play", {})
    assert wait_for(lambda: (js(app, "GET", "/api/state")[1] or {}).get("playing")), "pa en lecture"
    assert wait_for(lambda: qpaths(js(app, "GET", "/api/state")[1]) == [pb])   # pa promue, file = [pb]
    # audition de pb : la file moteur est suspendue, flag exposé
    st, r = js(app, "POST", "/api/preview", {"path": pb})
    assert st == 200 and r == {"ok": True}
    assert wait_for(lambda: qpaths(js(app, "GET", "/api/state")[1]) == [], 5.0), "file moteur suspendue"
    s = js(app, "GET", "/api/state")[1]
    assert s["preview"] == {"path": pb} and s["queue"] == []
    # fin naturelle de l'audition (queue_empty) -> vraie file restaurée, moteur AU REPOS
    assert wait_for(lambda: qpaths(js(app, "GET", "/api/state")[1]) == [pb], 8.0), "file restaurée"
    s2 = js(app, "GET", "/api/state")[1]
    assert s2["preview"] is None and s2["playing"] is None
    # re-audition puis SKIP : le DJ tranche -> file restaurée + relancée
    st, r = js(app, "POST", "/api/preview", {"path": pb})
    assert st == 200
    assert wait_for(lambda: (js(app, "GET", "/api/state")[1] or {})
                    .get("playing", {}).get("path") == pb, 5.0), "audition audible"
    st, r = js(app, "POST", "/api/skip", {})
    assert st == 200 and r == {"ok": True}
    assert wait_for(lambda: qpaths(js(app, "GET", "/api/state")[1]) == [pb], 6.0)
    assert wait_for(lambda: (js(app, "GET", "/api/state")[1] or {})
                    .get("playing"), 6.0), "moteur relancé (start=True)"
    s3 = js(app, "GET", "/api/state")[1]
    assert s3["preview"] is None and (s3["playing"] or {}).get("path") == pb   # start=True


def test_settings_crossfade_curve(tmp_path):
    """POST /api/settings {crossfade_curve} : persistée, valeur inconnue -> 400 sans
    effet ; les deux clés cohabitent dans /api/state et state.json."""
    app, _p, _mdir, _ddir, _dfl = app_for(tmp_path, files=1)
    assert settings_of(app)["crossfade_curve"] == "equal_power"      # défaut
    st, r = js(app, "POST", "/api/settings", {"crossfade_curve": "smooth"})
    assert st == 200 and r == {"crossfade_curve": "smooth"}
    assert wait_for(lambda: settings_of(app).get("crossfade_curve") == "smooth")
    st, r = js(app, "POST", "/api/settings", {"crossfade_curve": "wobble"})
    assert st == 400 and "error" in r
    assert settings_of(app)["crossfade_curve"] == "smooth"           # inchangée
    st, r = js(app, "POST", "/api/settings", {"crossfade": 12, "crossfade_curve": "cut"})
    assert st == 200 and r == {"crossfade": 12.0, "crossfade_curve": "cut"}
    assert wait_for(lambda: settings_of(app).get("crossfade") == 12.0
                    and settings_of(app).get("crossfade_curve") == "cut")
    stf = tmp_path / "data" / "state.json"
    assert wait_for(lambda: stf.exists() and
                    json.load(open(stf))["settings"]["crossfade_curve"] == "cut"), "persisté"


def test_preview_stop(tmp_path):
    """POST /api/preview/stop : file restaurée + `started` = état de lecture d'AVANT
    l'audition (False au repos, True si le DJ jouait) ; sans audition : 200 neutre."""
    app, (pa, pb), _mdir, _ddir, _dfl = app_for(tmp_path, files=2, pace=0.002, dur=10.0)
    js(app, "POST", "/api/queue", {"paths": [pa, pb]})
    assert wait_for(lambda: qpaths(js(app, "GET", "/api/state")[1]) == [pa, pb]), "file"
    js(app, "POST", "/api/preview", {"path": pb})            # moteur au repos
    assert wait_for(lambda: qpaths(js(app, "GET", "/api/state")[1]) == [], 5.0)
    st, r = js(app, "POST", "/api/preview/stop", {})
    assert st == 200 and r == {"ok": True, "started": False, "queue": 2}
    assert wait_for(lambda: qpaths(js(app, "GET", "/api/state")[1]) == [pa, pb], 6.0)
    assert wait_for(lambda: (js(app, "GET", "/api/state")[1] or {}).get("playing") is None, 6.0)
    s = js(app, "GET", "/api/state")[1]
    assert s["preview"] is None                               # plus d'audition en cours
    # --- audition lancée alors que le DJ jouait : started=True
    js(app, "POST", "/api/play", {})
    assert wait_for(lambda: (js(app, "GET", "/api/state")[1] or {}).get("playing"), 6.0)
    assert wait_for(lambda: qpaths(js(app, "GET", "/api/state")[1]) == [pb], 6.0)
    js(app, "POST", "/api/preview", {"path": pb})
    assert wait_for(lambda: qpaths(js(app, "GET", "/api/state")[1]) == [], 5.0)
    st, r = js(app, "POST", "/api/preview/stop", {})
    assert st == 200 and r == {"ok": True, "started": True, "queue": 1}
    assert wait_for(lambda: ((js(app, "GET", "/api/state")[1] or {}).get("playing") or {})
                    .get("path") == pb, 6.0), "la file reprend (pb promue)"
    assert qpaths(js(app, "GET", "/api/state")[1]) == []      # pb consommée par la lecture


def test_preview_stop_idle(tmp_path):
    """Stop sans audition : 200 neutre, aucune mutation de la file."""
    app, (pa,), _mdir, _ddir, _dfl = app_for(tmp_path, files=1)
    js(app, "POST", "/api/queue", {"paths": [pa]})
    assert wait_for(lambda: qpaths(js(app, "GET", "/api/state")[1]) == [pa])
    st, r = js(app, "POST", "/api/preview/stop", {})
    assert st == 200 and r == {"ok": True, "started": False, "queue": 1}
    assert qpaths(js(app, "GET", "/api/state")[1]) == [pa]


def test_import_multipart(tmp_path):
    """Import de 2 faux WAV -> fichiers dans downloads/imports/, banque re-scannée (fond) ;
    ré-import du MÊME contenu -> skipped, aucune copie byte-identique ni suffixe -1."""
    app, _p, _mdir, ddir, _dfl = app_for(tmp_path, files=1)
    imports = Path(ddir) / "imports"
    f1 = tmp_path / "Nouveau morceau.wav"; make_wav(f1, f=660)   # contenu unique (pas celui de la banque)
    f2 = tmp_path / "Deuxième.wav"; make_wav(f2, f=880)
    c = TestClient(app)
    r = c.post("/api/import", files=[("files", (f1.name, f1.read_bytes(), "audio/wav")),
                                     ("files", (f2.name, f2.read_bytes(), "audio/wav"))])
    assert r.status_code == 200 and r.json() == {"ok": True, "added": 2, "skipped": 0}
    assert (imports / "Nouveau morceau.wav").exists() and (imports / "Deuxième.wav").exists()
    assert wait_for(lambda: any(Path(x["path"]) == imports / "Nouveau morceau.wav"
                                for x in (js(app, "GET", "/api/state")[1] or {}).get("bank", [])))
    r = c.post("/api/import", files=[("files", (f1.name, f1.read_bytes(), "audio/wav"))])
    assert r.status_code == 200 and r.json() == {"ok": True, "added": 0, "skipped": 1}
    assert not (imports / "Nouveau morceau-1.wav").exists()   # pas de doublon byte-identique
    assert sorted(p.name for p in imports.iterdir()) == ["Deuxième.wav", "Nouveau morceau.wav"]
    s = js(app, "GET", "/api/state")[1]
    assert sum(1 for row in s["bank"] if row["path"] == str(imports / "Nouveau morceau.wav")) == 1


def test_import_meme_nom_contenu_different(tmp_path):
    """Même nom mais contenu différent : collision -> suffixe -1 (aucune perte)."""
    app, _p, _mdir, ddir, _dfl = app_for(tmp_path, files=1)
    imports = Path(ddir) / "imports"
    f = tmp_path / "Titre.wav"; make_wav(f, f=660)
    c = TestClient(app)
    r = c.post("/api/import", files=[("files", (f.name, f.read_bytes(), "audio/wav"))])
    assert r.json() == {"ok": True, "added": 1, "skipped": 0}
    make_wav(f, f=990)                                        # autre contenu, même nom
    r = c.post("/api/import", files=[("files", (f.name, f.read_bytes(), "audio/wav"))])
    assert r.json() == {"ok": True, "added": 1, "skipped": 0}
    assert (imports / "Titre-1.wav").exists()
    assert (imports / "Titre.wav").read_bytes() != (imports / "Titre-1.wav").read_bytes()


# ---------------- event loop / scans / chemins ---------------------------------
def test_queue_does_not_block_event_loop(tmp_path, monkeypatch):
    """POST /api/queue (ffprobe des rows inconnues) ne gèle pas la boucle : /api/state
    répond pendant l'appel (1,6 s de boucle bloquée mesurés sur 40 chemins hors banque)."""
    app, (pa,), *_ = app_for(tmp_path, files=1)
    srv = srv_of(app)
    real = srv._track
    calls = {"n": 0}

    def slow(p):
        calls["n"] += 1
        time.sleep(0.6)
        return real(p)

    monkeypatch.setattr(srv, "_track", slow)
    with TestClient(app) as c:                  # portail partagé : une seule event loop
        out = {}
        th = threading.Thread(target=lambda: out.update(
            c.post("/api/queue", json={"paths": [pa]}).json()))
        th.start()
        time.sleep(0.1)
        t0 = time.time()
        c.get("/api/state")
        lat = time.time() - t0
        th.join()
    assert out.get("ok") is True and calls["n"] == 1
    assert lat < 0.3, f"event loop bloqué {lat:.2f}s"


def test_restore_wait_does_not_block_event_loop(tmp_path, monkeypatch):
    """Le busy-wait de /api/restore (file prise par le mixeur) tourne en threadpool."""
    app, (pa, pb) = boot_app(tmp_path, lambda p: {
        "queue": [p[1]], "playing": {"path": p[0], "pos": 1.0}})
    assert wait_for(lambda: (js(app, "GET", "/api/state")[1] or {}).get("restore_offer")), "offre"
    srv = srv_of(app)
    monkeypatch.setattr(srv.engine, "set_queue", lambda paths: None)   # jamais prise
    with TestClient(app) as c:
        th = threading.Thread(target=lambda: c.post("/api/restore", json={"confirm": False}))
        th.start()
        time.sleep(0.1)
        t0 = time.time()
        c.get("/api/state")
        lat = time.time() - t0
        th.join()
    assert lat < 0.3, f"event loop bloqué {lat:.2f}s"


def test_scan_concurrent_un_seul_ffprobe(tmp_path, monkeypatch):
    """4 scans simultanés (boot/download/import) : verrou + rows connues réutilisées ->
    chaque fichier n'est ffprobe qu'une fois, banque correcte."""
    app, (pa,), mdir, ddir, _dfl = app_for(tmp_path, files=1)
    srv = srv_of(app)
    assert wait_for(lambda: any(r["path"] == pa for r in srv.bank_rows())), "scan boot"
    new = mdir / "nouveau.wav"; make_wav(new, f=660)
    from app import server as srvmod
    calls = []
    real = srvmod._meta

    def slow(p):
        calls.append(p)
        time.sleep(0.05)
        return real(p)

    monkeypatch.setattr(srvmod, "_meta", slow)
    ths = [threading.Thread(target=srv._scan, args=([str(mdir)],)) for _ in range(4)]
    for t in ths: t.start()
    for t in ths: t.join()
    assert calls.count(str(new)) == 1, calls          # pas 4 ffprobe du même fichier
    assert any(r["path"] == str(new) for r in srv.bank_rows())


def test_scan_ne_ressuscite_pas_un_fichier_supprime(tmp_path, monkeypatch):
    """Fichier supprimé pendant un scan (bank/clear concurrent) : pas de retour en banque."""
    app, (pa,), mdir, ddir, _dfl = app_for(tmp_path, files=1)
    srv = srv_of(app)
    assert wait_for(lambda: any(r["path"] == pa for r in srv.bank_rows())), "scan boot"
    doomed = mdir / "partant.wav"; make_wav(doomed, f=660)
    from app import server as srvmod
    real = srvmod._meta

    def meta_then_delete(p):
        r = real(p)
        if p == str(doomed):
            os.unlink(p)                              # supprimé entre le walk et le merge
        return r

    monkeypatch.setattr(srvmod, "_meta", meta_then_delete)
    srv._scan([str(mdir)])
    assert str(doomed) not in srv._bank


def test_waveform_et_preview_hors_bibliotheque(tmp_path):
    """Chemin hors dossiers musique/downloads : aucun décodage ffmpeg arbitraire."""
    app, (pa,), mdir, ddir, _dfl = app_for(tmp_path, files=1)
    outside = tmp_path / "ailleurs.wav"; make_wav(outside)
    st, data = api(app, "GET", "/api/waveform?" + urllib.parse.urlencode({"path": str(outside)}))
    assert st == 200 and json.loads(data) == []
    st, r = js(app, "POST", "/api/preview", {"path": str(outside)})
    assert st == 400 and "error" in r
    st, data = api(app, "GET", "/api/waveform?" + urllib.parse.urlencode({"path": pa}))
    assert st == 200 and len(json.loads(data)) == 400      # la bibliothèque marche toujours


def test_restore_no_offer_keeps_settings(tmp_path):
    """POST /api/restore sans offre (déjà consommée) : réglages live intacts."""
    app, (pa,), *_ = app_for(tmp_path, files=1)
    js(app, "POST", "/api/settings", {"crossfade": 6, "auto_queue": {"enabled": True, "min": 2}})
    js(app, "POST", "/api/volume", {"value": 0.35})
    st, r = js(app, "POST", "/api/restore", {})
    assert st == 200 and r.get("error") == "no_offer"
    s = js(app, "GET", "/api/state")[1]
    assert s["settings"]["crossfade"] == 6.0 and s["settings"]["volume"] == 0.35
    assert s["settings"]["auto_queue"] == {"enabled": True, "min": 2, "fill": 10}
    stf = tmp_path / "data" / "state.json"
    assert wait_for(lambda: stf.exists() and
                    json.load(open(stf))["settings"]["volume"] == 0.35), "persisté"


def test_import_bad_ext_nostore(tmp_path):
    """Mauvaise extension -> 400, rien écrit ; /api/state porte Cache-Control: no-store."""
    app, _p, _mdir, ddir, _dfl = app_for(tmp_path, files=1)
    bad = tmp_path / "malware.txt"; bad.write_bytes(b"x" * 64)
    c = TestClient(app)
    r = c.post("/api/import", files=[("files", (bad.name, bad.read_bytes(), "text/plain"))])
    assert r.status_code == 400 and "error" in r.json()
    assert not (Path(ddir) / "imports" / "malware.txt").exists()
    r2 = c.get("/api/state")
    assert r2.status_code == 200 and r2.headers.get("cache-control") == "no-store"


def test_queue_auto_fill(tmp_path):
    """POST /api/queue/auto : remplit la file avec des chemins de la banque, sans
    doublon ; banque épuisée -> erreur ; replace=True remplace la file à venir."""
    app, _p, _mdir, _ddir, _dfl = app_for(tmp_path, files=3)   # banque = 4 fichiers
    assert wait_for(lambda: len((js(app, "GET", "/api/state")[1] or {})
                                .get("bank", [])) >= 4), "scan de la banque"
    st, r = js(app, "POST", "/api/queue/auto", {"count": 4})
    assert st == 200 and r == {"ok": True, "added": 4, "queue": 4}
    assert wait_for(lambda: len(qpaths(js(app, "GET", "/api/state")[1])) == 4)
    s = js(app, "GET", "/api/state")[1]
    q = qpaths(s)
    assert len(set(q)) == 4                                  # aucun doublon
    assert set(q) <= {row["path"] for row in s["bank"]}      # chemins de la banque
    st, r = js(app, "POST", "/api/queue/auto", {"count": 4})  # tout est déjà en file
    assert st == 400 and "error" in r
    st, r = js(app, "POST", "/api/queue/auto", {"count": 2, "replace": True})
    assert st == 200 and r["added"] == 2 and r["queue"] == 2
    assert wait_for(lambda: len(qpaths(js(app, "GET", "/api/state")[1])) == 2)
    q2 = qpaths(js(app, "GET", "/api/state")[1])
    assert len(set(q2)) == 2
    st, r = js(app, "POST", "/api/queue/auto", {"count": "x"})
    assert st == 400 and "error" in r                        # count invalide


def test_settings_auto_queue(tmp_path):
    """POST /api/settings {auto_queue} : fusion partielle, bornes, /api/state le
    reflète, persistance dans state.json ; crossfade toujours accepté seul."""
    app, _p, _mdir, _ddir, _dfl = app_for(tmp_path, files=1)
    st, r = js(app, "POST", "/api/settings",
               {"auto_queue": {"enabled": True, "min": 2, "fill": 5}})
    assert st == 200 and r["auto_queue"] == {"enabled": True, "min": 2, "fill": 5}
    assert wait_for(lambda: settings_of(app).get("auto_queue")
                    == {"enabled": True, "min": 2, "fill": 5})
    js(app, "POST", "/api/settings", {"auto_queue": {"min": -5, "fill": 999}})
    a = settings_of(app)["auto_queue"]                       # borné + fusion partielle
    assert a == {"enabled": True, "min": 0, "fill": 50}
    st, r = js(app, "POST", "/api/settings", {"crossfade": 7})
    assert st == 200 and r == {"crossfade": 7.0}
    st, r = js(app, "POST", "/api/settings", {})
    assert st == 400 and "error" in r
    stf = tmp_path / "data" / "state.json"
    assert wait_for(lambda: stf.exists() and
                    json.load(open(stf))["settings"]["auto_queue"]["fill"] == 50), "persisté"


def test_auto_queue_refill_on_track_started(tmp_path):
    """auto_queue actif : au démarrage d'un morceau, la file à venir repasse au-dessus
    de `min` et le morceau joué entre dans l'historique."""
    app, paths, _mdir, _ddir, _dfl = app_for(tmp_path, files=3, pace=0.005, dur=10.0)
    pa = paths[0]
    assert wait_for(lambda: len((js(app, "GET", "/api/state")[1] or {})
                                .get("bank", [])) >= 4), "banque"
    js(app, "POST", "/api/settings", {"auto_queue": {"enabled": True, "min": 2, "fill": 3}})
    js(app, "POST", "/api/queue", {"paths": [pa]})            # file à venir = 1 morceau
    assert wait_for(lambda: qpaths(js(app, "GET", "/api/state")[1]) == [pa]), "file prête"
    js(app, "POST", "/api/play", {})
    assert wait_for(lambda: ((js(app, "GET", "/api/state")[1] or {}).get("playing") or {})
                    .get("path") == pa, 8.0), "pa en lecture"
    assert wait_for(lambda: (js(app, "GET", "/api/state")[1] or {})
                    .get("history", [])[:1] == [pa], 8.0), "track_started -> historique"
    assert wait_for(lambda: len(qpaths(js(app, "GET", "/api/state")[1])) >= 2, 8.0), \
        "re-remplissage automatique"
    s = js(app, "GET", "/api/state")[1]
    assert s["history"][0] == pa                              # plus récent en premier


def test_state_playing_incoming(tmp_path):
    """`playing.incoming` : clé toujours présente dans la tranche playing (None au repos
    et en lecture simple), remplie pendant un crossfade naturel -> l'UI suit la piste
    réellement audible au lieu de la barre finie du sortant."""
    app, (pa, pb), _mdir, _ddir, _dfl = app_for(tmp_path, files=2, pace=0.01, dur=6.0)
    js(app, "POST", "/api/settings", {"crossfade": 2})
    js(app, "POST", "/api/queue", {"paths": [pa, pb]})
    assert wait_for(lambda: qpaths(js(app, "GET", "/api/state")[1]) == [pa, pb]), "file"
    assert (js(app, "GET", "/api/state")[1] or {})["playing"] is None    # repos : tranche null
    js(app, "POST", "/api/play", {})
    assert wait_for(lambda: ((js(app, "GET", "/api/state")[1] or {}).get("playing") or {})
                    .get("path") == pa, 8.0), "pa en lecture"
    p0 = (js(app, "GET", "/api/state")[1] or {})["playing"]
    assert "incoming" in p0 and p0["incoming"] is None       # lecture simple : pas d'entrante

    seen = []

    def mid_fade():
        p = (js(app, "GET", "/api/state")[1] or {}).get("playing") or {}
        if p.get("incoming"):
            seen.append(p)
        return bool(seen)

    assert wait_for(mid_fade, 12.0), "incoming exposé pendant le crossfade"
    p = seen[0]
    assert p["path"] == pa and p["fading"] is True, f"sortant={p['path']} attendu={pa}"
    inc = p["incoming"]
    assert set(inc) == {"path", "pos", "dur", "cue_in", "cue_out"}, inc
    assert inc["path"] == pb, f"entrant={inc['path']} attendu={pb} (queue={p})"
    # pos = start + consommé : au tout 1er bloc du fade, pos == cue_in (0.0 ici, WAV sans silence)
    assert inc["cue_in"] <= inc["pos"] < inc["dur"], inc
    assert wait_for(lambda: ((js(app, "GET", "/api/state")[1] or {}).get("playing") or {})
                    .get("path") == pb, 10.0), "pb promue"
    assert ((js(app, "GET", "/api/state")[1] or {}).get("playing") or {})["incoming"] is None


# ---------------- vider la banque / quitter ------------------------------------
def test_bank_clear_removes_downloads_only(tmp_path):
    """POST /api/bank/clear : downloads/ vidé (récursif), musique perso intacte,
    banque/file/historique purgés des chemins supprimés."""
    app, (pa,), mdir, ddir, dfl = app_for(tmp_path, files=1)
    srv = srv_of(app)
    os.unlink(dfl)                                    # removed attendu = 3 exactement
    d1 = Path(ddir) / "dl1.wav"; make_wav(d1)
    d2 = Path(ddir) / "dl2.wav"; make_wav(d2)
    sub = Path(ddir) / "imports"; sub.mkdir(exist_ok=True)
    d3 = sub / "dl3.wav"; make_wav(d3)
    srv._scan([str(ddir)])                            # scan synchrone (comme la fin d'un job)
    assert {str(d1), str(d2), str(d3)} <= {r["path"] for r in srv.bank_rows()}
    assert wait_for(lambda: pa in {r["path"] for r in srv.bank_rows()}), "scan musique"
    js(app, "POST", "/api/queue", {"paths": [str(d1), str(d2)]})
    srv.push_history(str(d3))
    assert wait_for(lambda: qpaths(js(app, "GET", "/api/state")[1]) == [str(d1), str(d2)])

    st, r = js(app, "POST", "/api/bank/clear", {})
    assert st == 200 and r["ok"] is True and r["removed"] == 3, (st, r)
    assert not d1.exists() and not d2.exists() and not d3.exists()
    assert sub.is_dir()                               # dossiers conservés
    assert Path(pa).exists()                          # musique perso JAMAIS touchée
    assert wait_for(lambda: qpaths(js(app, "GET", "/api/state")[1]) == []), "file purgée"
    s = js(app, "GET", "/api/state")[1]
    assert {row["path"] for row in s["bank"]} == {pa}
    assert s["history"] == []                         # historique purgé
    assert r["bank"] == len(s["bank"]) and r["queue"] == len(qpaths(s))


def test_bank_clear_current_track_skips(tmp_path):
    """Piste en cours dans downloads/ supprimée -> le moteur saute proprement."""
    app, (pa,), mdir, ddir, dfl = app_for(tmp_path, files=1, pace=0.002, dur=30.0)
    js(app, "POST", "/api/queue", {"paths": [dfl, pa]})
    assert wait_for(lambda: qpaths(js(app, "GET", "/api/state")[1]) == [dfl, pa]), "file"
    js(app, "POST", "/api/play", {})
    assert wait_for(lambda: ((js(app, "GET", "/api/state")[1] or {}).get("playing") or {})
                    .get("path") == dfl, 8.0), "piste downloads en cours"
    st, r = js(app, "POST", "/api/bank/clear", {})
    assert st == 200 and r["removed"] == 1, (st, r)
    assert not Path(dfl).exists()
    assert wait_for(lambda: ((js(app, "GET", "/api/state")[1] or {}).get("playing") or {})
                    .get("path") != dfl, 8.0), "piste supprimée sautée"
    assert ((js(app, "GET", "/api/state")[1] or {}).get("playing") or {}).get("path") != dfl
    assert wait_for(lambda: qpaths(js(app, "GET", "/api/state")[1]) == [], 8.0)


def test_bank_remove_un_import(tmp_path):
    """POST /api/bank/remove : supprime UN fichier de downloads/ et purge ses références."""
    app, (pa,), mdir, ddir, dfl = app_for(tmp_path, files=1)
    srv = srv_of(app)
    d1 = Path(ddir) / "dl1.wav"; make_wav(d1)
    srv._scan([str(ddir)])
    assert wait_for(lambda: str(d1) in {r["path"] for r in srv.bank_rows()}), "scan downloads"
    js(app, "POST", "/api/queue", {"paths": [str(d1), pa]})
    srv.push_history(str(d1))
    assert wait_for(lambda: qpaths(js(app, "GET", "/api/state")[1]) == [str(d1), pa])

    st, r = js(app, "POST", "/api/bank/remove", {"path": str(d1)})
    assert st == 200 and r["ok"] is True and r["removed"] == 1, (st, r)
    assert not d1.exists() and Path(pa).exists()          # musique perso intacte
    s = js(app, "GET", "/api/state")[1]
    assert str(d1) not in {row["path"] for row in s["bank"]}
    assert str(d1) not in s["history"]
    assert wait_for(lambda: qpaths(js(app, "GET", "/api/state")[1]) == [pa]), "file purgée"


def test_bank_remove_hors_downloads_refuse(tmp_path):
    """Musique perso (lecture seule) et chemins invalides : refus, fichier intact."""
    app, (pa,), mdir, ddir, dfl = app_for(tmp_path, files=1)
    st, r = js(app, "POST", "/api/bank/remove", {"path": pa})
    assert st == 403 and "error" in r and Path(pa).exists()
    st, r = js(app, "POST", "/api/bank/remove", {"path": "/etc/passwd"})
    assert st == 403
    st, r = js(app, "POST", "/api/bank/remove", {})
    assert st == 400
    st, r = js(app, "POST", "/api/bank/remove", {"path": str(Path(ddir) / "absent.wav")})
    assert st == 200 and r["removed"] == 0                # déjà absent : purge tolérante


def test_bank_clear_scope_invalid(tmp_path):
    """scope inconnu -> 400 et rien n'est supprimé."""
    app, _p, _mdir, ddir, dfl = app_for(tmp_path, files=1)
    st, r = js(app, "POST", "/api/bank/clear", {"scope": "all"})
    assert st == 400 and "error" in r
    assert Path(dfl).exists()


def test_bank_clear_refuses_active_download(tmp_path):
    """Téléchargement en cours -> 409, aucun fichier supprimé."""
    app, _p, _mdir, ddir, dfl = app_for(tmp_path, files=1)
    srv_of(app).dl._jobs["j1"] = {"id": "j1", "url": "u", "title": "t",
                                  "status": "running", "file": None}
    st, r = js(app, "POST", "/api/bank/clear", {})
    assert st == 409 and r == {"error": "téléchargement en cours"}
    assert Path(dfl).exists()


def test_quit_saves_and_exits(tmp_path, monkeypatch):
    """POST /api/quit : ok immédiat, save avant sortie, _hard_exit une seule fois
    même avec deux appels rapprochés."""
    from app import server as srvmod
    calls = []
    monkeypatch.setattr(srvmod, "_hard_exit", lambda code=0: calls.append(code))
    app, (pa,), *_ = app_for(tmp_path)
    js(app, "POST", "/api/queue", {"paths": [pa]})
    stf = tmp_path / "data" / "state.json"
    assert wait_for(lambda: stf.exists() and json.load(open(stf))["queue"] == [pa]), "state"
    pid = tmp_path / "data" / "server.pid"
    pid.write_text("123")
    st, r = js(app, "POST", "/api/quit", {})
    assert st == 200 and r == {"ok": True}
    js(app, "POST", "/api/quit", {})                  # idempotent : pas de 2e sortie
    assert wait_for(lambda: len(calls) == 1, 2.0), "sortie planifiée"
    time.sleep(0.3)                                   # laisse le 2e timer passer
    assert calls == [0]
    assert json.load(open(stf))["queue"] == [pa]      # save immédiat avant la sortie
    assert not pid.exists()
