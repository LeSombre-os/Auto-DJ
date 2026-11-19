"""File d'attente automatique — règles pures, sans I/O, rng injectable.

Priorités (la première gagne) : (1) jamais de doublon (file + lot), (2) éviter
l'historique, (3) espacement artiste (>= 3 positions), (4) pas 2 fois le même
dossier parent de suite, (5) hasard pondéré. Chaque règle se relâche quand le
vivier ne permet plus de la tenir — on remplit au mieux, jamais de crash.
"""
import os
import random

ARTIST_GAP = 3          # positions d'écart mini entre 2 morceaux du même artiste


def _folder(p: str) -> str:
    return os.path.dirname(str(p)) or "."


def _artist_key(row: dict) -> str:
    """Artiste normalisé ; vide -> nom du dossier parent (approximation album)."""
    a = str(row.get("artist") or "").strip().lower()
    return a or os.path.basename(_folder(row.get("path"))).strip().lower()


def _weight(p: str, hpos: dict) -> float:
    """Hors historique : poids neutre. Historique : le plus ancien pèse plus lourd
    (index élevé = joué il y a longtemps) -> relâche du plus ancien d'abord."""
    i = hpos.get(p)
    return 1.0 if i is None else float(i + 1)


def build_queue(bank, count, *, queue_paths=(), history=(), rng=None) -> list[str]:
    """Au plus `count` chemins de `bank` (liste de rows {path, artist, ...}), dans
    l'ordre de lecture, en respectant les règles du module. Jamais de doublon."""
    rng = rng or random.Random()
    try:
        count = int(count)
    except (TypeError, ValueError):
        return []
    if count <= 0:
        return []
    rows = [r for r in (bank or [])
            if isinstance(r, dict) and isinstance(r.get("path"), str) and r["path"]]
    amap = {r["path"]: _artist_key(r) for r in rows}
    hpos = {str(p): i for i, p in enumerate(history or [])}
    tail = [str(p) for p in (queue_paths or [])]     # contexte : file existante
    used = set(tail)
    alive = [r["path"] for r in rows if r["path"] not in used]
    out = []

    def artist_of(p):
        return amap.get(p) or os.path.basename(_folder(p)).strip().lower()

    def artist_ok(p, span):                          # span = nb de prédécesseurs vus
        a = artist_of(p)
        return all(artist_of(q) != a for q in tail[-span:])

    def folder_ok(p):
        return not tail or _folder(p) != _folder(tail[-1])

    while alive and len(out) < count:
        pick = None
        for fresh_only in (True, False):             # historique d'abord, puis relâché
            pool = [p for p in alive if not fresh_only or p not in hpos]
            if not pool:
                continue
            for cand in ([p for p in pool if artist_ok(p, ARTIST_GAP - 1) and folder_ok(p)],
                         [p for p in pool if artist_ok(p, ARTIST_GAP - 1)],
                         [p for p in pool if artist_ok(p, 1)]):
                if cand:
                    pick = rng.choices(cand, weights=[_weight(p, hpos) for p in cand])[0]
                    break
            if pick is not None:
                break
            # vivier historique non vide mais rien de valide -> tente le pool complet
        if pick is None:                             # même artiste imposé : on prend quand même
            pick = rng.choices(alive, weights=[_weight(p, hpos) for p in alive])[0]
        out.append(pick)
        used.add(pick)
        tail.append(pick)
        alive = [p for p in alive if p != pick]
    return out
