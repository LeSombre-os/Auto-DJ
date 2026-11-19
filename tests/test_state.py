"""Tests headless de app/state.py — tmp_path uniquement, offline, threads inclus."""
import json, os, sys, threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import state

KEYS = {"queue", "playing", "settings", "bank", "history", "saved_at"}
AUTO = {"enabled": False, "min": 3, "fill": 10}


def test_roundtrip(tmp_path):
    d = str(tmp_path / "data")
    src = {
        "queue": ["/z/a.mp3", "/z/b.flac", "/z/c.wav"],
        "playing": {"path": "/z/b.flac", "pos": 123.4567891},
        "settings": {"crossfade": 7.5, "crossfade_curve": "smooth", "volume": 0.83,
                     "ducking": True,
                     "auto_queue": {"enabled": True, "min": 5, "fill": 20}},
        "bank": ["/z/a.mp3", "/z/b.flac"],
        "history": ["/z/c.wav", "/z/a.mp3"],
    }
    state.save(src, d)
    out = state.load(d)
    assert out["queue"] == src["queue"]                     # ordre préservé
    assert out["playing"]["pos"] == src["playing"]["pos"]   # float précis
    assert out["playing"]["path"] == "/z/b.flac"
    assert out["settings"] == src["settings"]
    assert out["bank"] == src["bank"]
    assert out["history"] == src["history"]                 # plus récent en premier
    assert isinstance(out["saved_at"], str)                 # iso8601 écrit par save()
    assert state.path(d).endswith("state.json")
    assert state.path() == os.path.join("data", "state.json")


def test_corrupt_then_empty(tmp_path):
    d = str(tmp_path / "data")
    (tmp_path / "data").mkdir()
    p = Path(state.path(d))
    p.write_text("garbage")                                 # JSON invalide
    assert state.load(d) == {}
    p.write_text('{"queue": [')                             # fichier partiel (crash ancien format)
    assert state.load(d) == {}
    p.write_text("[1,2]")                                   # JSON valide mais pas un objet
    assert state.load(d) == {}
    p.unlink()
    assert state.load(d) == {}                              # absent


def test_merge_defaults(tmp_path):
    d = str(tmp_path / "data")
    (tmp_path / "data").mkdir()
    Path(state.path(d)).write_text(json.dumps({"playing": {"path": "x"}}))
    r = state.load(d)
    assert set(r) == KEYS
    assert r["queue"] == [] and r["bank"] == [] and r["history"] == []
    assert r["settings"] == {"crossfade": 8.0, "crossfade_curve": "equal_power",
                             "volume": 1.0, "ducking": False, "auto_queue": AUTO}
    assert r["playing"]["path"] == "x" and r["playing"]["pos"] == 0.0


def test_bounds(tmp_path):
    d = str(tmp_path / "data")
    (tmp_path / "data").mkdir()
    Path(state.path(d)).write_text(json.dumps(
        {"settings": {"crossfade": 99, "crossfade_curve": "wobble", "volume": -5, "bad": 1},
         "playing": {"pos": "nope", "path": 12}}))
    r = state.load(d)                                       # borné EN LECTURE
    assert r["settings"] == {"crossfade": 16.0, "crossfade_curve": "equal_power",
                             "volume": 0.0, "ducking": False, "auto_queue": AUTO}
    assert r["playing"] == {"path": None, "pos": 0.0}
    assert r["saved_at"] is None                            # défaut faute de timestamp
    Path(state.path(d)).write_text(json.dumps(
        {"settings": {"crossfade": 1, "crossfade_curve": "cut"}}))
    r2 = state.load(d)                                      # borne basse + courbe valide
    assert r2["settings"]["crossfade"] == 2.0
    assert r2["settings"]["crossfade_curve"] == "cut"


def test_offer_restore(tmp_path):
    d = str(tmp_path / "data")
    real = tmp_path / "track.mp3"
    real.write_bytes(b"x")
    state.save({"playing": {"path": str(real), "pos": 4.2}}, d)   # makedirs via save()
    r = state.offer_restore(d)
    assert r is not None and r["playing"]["pos"] == 4.2
    state.save({"playing": {"path": str(tmp_path / "manquant.mp3"), "pos": 1.0}}, d)
    assert state.offer_restore(d) is None                   # path absent du disque
    assert state.offer_restore(str(tmp_path / "pas-la")) is None  # pas de fichier


def test_atomic_concurrent(tmp_path):
    """200 saves en 4 threads : jamais de state.json vide/partiel lisible."""
    d = str(tmp_path / "data")
    errs = []

    def worker(w):
        for i in range(50):
            q = [f"/z/{w}-{i}.mp3", "/z/next.flac"]
            state.save({"queue": q, "playing": {"path": q[0], "pos": i + w * 50}}, d)
            r = state.load(d)                               # lecture pendant la tempête
            if r != {} and not KEYS.issubset(r):            # {} permis (premier save pas fait)
                errs.append(r)

    ths = [threading.Thread(target=worker, args=(w,)) for w in range(4)]
    for t in ths: t.start()
    for t in ths: t.join()
    assert not errs                                         # aucune lecture agrippée
    r = state.load(d)
    assert KEYS.issubset(r)                                 # état final lisible et complet
    assert r["queue"][-1] == "/z/next.flac"
    assert isinstance(r["saved_at"], str)
    assert not list((tmp_path / "data").glob("state.json.tmp"))   # aucun tmp orphelin


def test_save_identique_sans_reecriture(tmp_path, monkeypatch):
    """Autosave 2 s : contenu inchangé -> aucune réécriture ; un changement écrit."""
    d = str(tmp_path / "data")
    state.save({"queue": ["/z/a.mp3"]}, d)
    n = {"c": 0}
    real = os.replace
    monkeypatch.setattr(state.os, "replace",
                        lambda *a, **k: (n.__setitem__("c", n["c"] + 1), real(*a, **k)))
    state.save({"queue": ["/z/a.mp3"]}, d)          # identique -> skip
    assert n["c"] == 0
    state.save({"queue": ["/z/b.mp3"]}, d)          # changé -> écrit
    assert n["c"] == 1
    assert state.load(d)["queue"] == ["/z/b.mp3"]
