"""Tests purs de app/queue_rules.py — aucune I/O, rng seedé (déterministe)."""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.queue_rules import build_queue


def bank_of(artists, per=2):
    """Un dossier (album) distinct par morceau -> la règle 'même dossier' ne gêne pas."""
    return [{"path": f"/lib/{a}/album-{i}/{a}-{i}.mp3", "artist": a,
             "title": f"{a}-{i}", "dur": 60.0}
            for a in artists for i in range(per)]


def artists_of(paths):
    return [p.split("/")[2] for p in paths]


def test_count_and_no_duplicate():
    bank = bank_of("abcdef", per=2)                       # 12 morceaux
    q = build_queue(bank, 5, rng=random.Random(1))
    assert len(q) == 5                                    # count respecté
    assert len(set(q)) == 5                               # aucun doublon
    q = build_queue(bank, 50, rng=random.Random(2))       # plus que le vivier
    assert len(q) == 12 and len(set(q)) == 12             # tout, une seule fois
    assert build_queue(bank, 0) == []


def test_queue_paths_never_repeated():
    bank = bank_of("abcdef", per=2)
    taken = [bank[0]["path"], bank[3]["path"]]
    q = build_queue(bank, 20, queue_paths=taken, rng=random.Random(3))
    assert len(q) == 10 and not set(q) & set(taken)       # file existante exclue
    assert len(set(q)) == len(q)


def test_never_two_same_artist_in_a_row():
    bank = bank_of("AB", per=3)                           # 2 artistes x 3
    for seed in range(5):
        q = build_queue(bank, 6, rng=random.Random(seed))
        a = artists_of(q)
        assert len(q) == 6
        assert all(x != y for x, y in zip(a, a[1:]))      # jamais consécutif
    # vivier mono-artiste : relâchement total, jamais de crash ni de doublon
    solo = bank_of("S", per=4)
    q = build_queue(solo, 4, rng=random.Random(0))
    assert len(q) == 4 and len(set(q)) == 4


def test_artist_spacing_three_positions():
    bank = bank_of("ABC", per=3)                          # 3 artistes x 3
    for seed in range(5):
        q = build_queue(bank, 6, rng=random.Random(seed))
        assert len(q) == 6
        for a in "ABC":
            pos = [i for i, p in enumerate(q) if p.split("/")[2] == a]
            assert all(y - x >= 3 for x, y in zip(pos, pos[1:])), (seed, a, pos)


def test_history_avoided_then_relaxed():
    bank = bank_of("ABCD", per=1)
    hist = [bank[0]["path"], bank[1]["path"]]             # plus récent en premier
    q = build_queue(bank, 2, history=hist, rng=random.Random(4))
    assert set(q) == {bank[2]["path"], bank[3]["path"]}   # l'historique est évité
    q = build_queue(bank, 4, history=hist, rng=random.Random(4))
    assert len(q) == 4 and set(q) == {r["path"] for r in bank}   # puis relâché


def test_folder_not_twice_in_a_row():
    # 2 albums, 3 morceaux chacun, 2 artistes distincts : le dossier est le critère
    bank = [{"path": f"/lib/art/{alb}/{i}.mp3", "artist": f"art{i}", "dur": 60.0}
            for alb in ("a1", "a2") for i in range(3)]
    q = build_queue(bank, 4, rng=random.Random(5))
    folders = [Path(p).parent.name for p in q]
    assert all(x != y for x, y in zip(folders, folders[1:]))      # pas 2x le même album
    # artiste vide -> clé = nom du dossier parent (même dossier = même "artiste")
    rows = [{"path": f"/z/{d}/{n}.mp3", "artist": "", "dur": 1.0}
            for d in ("d1", "d2", "d3") for n in ("x", "y")]
    q = build_queue(rows, 4, rng=random.Random(6))
    assert len(q) == 4 and len(set(q)) == 4
    assert all(x != y for x, y in zip(q, q[1:]))


def test_empty_and_bad_bank():
    assert build_queue([], 10) == []
    assert build_queue(None, 3) == []
    assert build_queue([{"no_path": 1}, "junk"], 3) == []
