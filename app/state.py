"""État de session Auto-DJ : data/state.json — écriture atomique, lecture tolérante.

Fonctions simples, pas de classe : le serveur décide QUAND sauvegarder (timer 2 s +
chaque mutation + à l'arrêt). save()/load() normalisent toujours vers le SCHEMA
(clés manquantes -> défauts, volume/crossfade bornés) : l'UI ne voit jamais un dict agrippé.
"""
import datetime, json, os, threading

_LOCK = threading.Lock()            # sérialise les écritures concurrentes (serveur)
_LAST: dict = {}                    # path state.json -> dernier contenu écrit (sans saved_at)

AUTO_QUEUE = {"enabled": False, "min": 3, "fill": 10}     # remplissage auto de la file
HIST_MAX = 50                                            # paths conservés dans l'historique
XF_CURVES = ("linear", "equal_power", "smooth", "cut")   # courbes de crossfade (source unique)
XF_DEFAULT = "equal_power"

SCHEMA = {
    "queue": [],                                          # [path, ...] en attente
    "playing": {"path": None, "pos": 0.0},                # str|None, float
    "settings": {"crossfade": 8.0, "crossfade_curve": XF_DEFAULT,
                 "volume": 1.0, "ducking": False,
                 "auto_queue": dict(AUTO_QUEUE)},
    "bank": [],
    "history": [],                                        # [path, ...] plus récent en premier
    "saved_at": None,                                     # iso8601 UTC après save()
}


def norm_auto_queue(value=None, base=None) -> dict:
    """Fusion partielle + bornes : enabled bool, min 0..20, fill 1..50."""
    out = dict(AUTO_QUEUE)
    if isinstance(base, dict):
        out.update({k: base[k] for k in AUTO_QUEUE if k in base})
    if isinstance(value, dict):
        if isinstance(value.get("enabled"), bool):
            out["enabled"] = value["enabled"]
        for k, lo, hi in (("min", 0, 20), ("fill", 1, 50)):
            x = value.get(k)
            if isinstance(x, (int, float)) and not isinstance(x, bool):
                out[k] = int(min(max(float(x), lo), hi))
    return out


def path(data_dir="data") -> str:
    return os.path.join(data_dir, "state.json")


def _norm(state) -> dict:
    """Complète un dict partiel vers le SCHEMA ; borne volume 0..1, crossfade 2..16,
    courbe validée dans XF_CURVES (défaut sinon)."""
    s = {"queue": list(SCHEMA["queue"]), "bank": list(SCHEMA["bank"]),
         "history": list(SCHEMA["history"]),
         "playing": dict(SCHEMA["playing"]),
         "settings": {**SCHEMA["settings"], "auto_queue": dict(AUTO_QUEUE)},
         "saved_at": None}
    if not isinstance(state, dict):
        return s
    if isinstance(state.get("queue"), list):
        s["queue"] = [str(p) for p in state["queue"]]
    if isinstance(state.get("bank"), list):
        s["bank"] = [str(p) for p in state["bank"]]
    if isinstance(state.get("history"), list):
        s["history"] = [str(p) for p in state["history"]][:HIST_MAX]
    p = state.get("playing")
    if isinstance(p, dict):
        s["playing"]["path"] = p.get("path") if isinstance(p.get("path"), str) else None
        if isinstance(p.get("pos"), (int, float)):
            s["playing"]["pos"] = float(p["pos"])
    st = state.get("settings")
    if isinstance(st, dict):
        try:
            s["settings"]["crossfade"] = min(max(float(st.get("crossfade", 8.0)), 2.0), 16.0)
        except (TypeError, ValueError):
            pass
        if st.get("crossfade_curve") in XF_CURVES:
            s["settings"]["crossfade_curve"] = st["crossfade_curve"]
        try:
            s["settings"]["volume"] = min(max(float(st.get("volume", 1.0)), 0.0), 1.0)
        except (TypeError, ValueError):
            pass
        if isinstance(st.get("ducking"), bool):
            s["settings"]["ducking"] = st["ducking"]
        s["settings"]["auto_queue"] = norm_auto_queue(st.get("auto_queue"),
                                                      s["settings"]["auto_queue"])
    if isinstance(state.get("saved_at"), str):
        s["saved_at"] = state["saved_at"]
    return s


def load(data_dir="data") -> dict:
    """{} si le fichier est absent, corrompu, partiel ou pas un objet JSON — jamais d'exception."""
    try:
        with open(path(data_dir)) as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return {}
    except Exception:
        return {}
    return _norm(raw)


def save(state: dict, data_dir="data") -> None:
    """Normalise puis écrit data/state.json en atomique (tmp + os.replace, portable Windows).

    Contenu identique au dernier écrit -> aucune réécriture : l'autosave 2 s d'une
    soirée au repos ne réécrit plus ~55 Ko (banque 950) en boucle.
    """
    s = _norm(state)
    s["saved_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    body = {k: v for k, v in s.items() if k != "saved_at"}
    with _LOCK:
        try:
            full = path(data_dir)
            if _LAST.get(full) == body and os.path.exists(full):
                return              # rien n'a changé : state.json est déjà à jour
            os.makedirs(data_dir, exist_ok=True)
            tmp = full + ".tmp"
            with open(tmp, "w") as f:
                json.dump(s, f)
            os.replace(tmp, full)
            _LAST[full] = body
        except Exception:
            pass                    # jamais de crash pour une I/O disque (règle projet)


def offer_restore(data_dir="data") -> dict | None:
    """L'état complet si la piste en cours existe encore sur disque, sinon None."""
    st = load(data_dir)
    p = st.get("playing") or {}
    if p.get("path") and os.path.exists(p["path"]):
        return st
    return None
