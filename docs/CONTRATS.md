# CONTRATS D'INTÉGRATION — Auto-DJ-V1

Contrat unique pour tous les modules. Le respecter à la lettre : c'est le filet de sécurité entre sous-agents.

## Identité d'une piste

Une piste = son **chemin absolu (str)**. Les métadonnées (titre/artiste/durée) viennent de ffprobe (`analyze`/`metadata`). Fichiers audio : MP3, WAV, FLAC, AAC, M4A, OGG. La banque scanne en plus `.webm` et `.opus` (conteneurs `bestaudio` de yt-dlp) ; l'import local reste limité aux extensions ci-dessus.

## app/engine.py

```python
class Analysis(TypedDict):
    path: str; title: str; artist: str; duration: float
    cue_in: float; cue_out: float            # bornes du signal réel (seuil -35 dBFS)
    envelope: np.ndarray                      # float32, ~100 Hz (1 valeur / 10ms)

def analyze(path: str, ffmpeg=...) -> Analysis        # cache RAM + persiste cue_in/out/duration dans data/cues.json (atomique, lock)
def waveform(path: str, pixels: int) -> list[float]   # max de |enveloppe| par bucket, normalisée 0..1

class Engine:
    def __init__(self, out_factory=None, data_dir="data"): ...
    # out_factory(rate)->objet .rate / .write(block) / .close() ; défaut : ALSAOut (Linux,
    # repli SDOut si ALSA échoue et que sounddevice est installé) ou SDOut (Windows/macOS,
    # sounddevice) — les tests injectent une fausse sortie

    def set_queue(self, paths: list[str]) -> None          # remplace la file après la piste en cours
    def play_toggle(self) -> None
    def skip(self) -> None                                  # transition immédiate (fade court ~0.3s)
    def set_crossfade(self, seconds: float) -> None         # borné 2..16, défaut 8.0
    def set_crossfade_curve(self, name: str) -> None        # linear | equal_power | smooth | cut
                                                            # (défaut equal_power ; inconnue -> inchangée)
    def set_volume(self, v: float) -> None                  # 0..1
    def set_duck(self, on: bool) -> None                    # niveau ducking fixe 0.25, rampe 100 ms
    def set_position(self, seconds: float) -> None          # restaurer à une position
    def status(self) -> dict, jamais bloquant :
    # {"playing": bool, "path": str|None, "pos": float, "dur": float,
    #  "cue_in": float, "cue_out": float, "fading": bool,
    #  "next": [path, ...], "volume": float, "ducking": bool,
    #  "crossfade": float, "crossfade_curve": str, "preview": bool}
```

Courbes de crossfade (`xf_gains(curve, pc)`, progression `pc ∈ [0,1]`) :

| `crossfade_curve` | Sortant / entrant | Formule |
| --- | --- | --- |
| `linear` | `1-pc` / `pc` | `out + in = 1` |
| `equal_power` | `cos(pc·π/2)` / `sin(pc·π/2)` | `out² + in² = 1` (défaut) |
| `smooth` | `1-s` / `s` | `s = 3pc² − 2pc³` (smoothstep) |
| `cut` | `1-pc` / `pc` | gains linéaires, durée forcée ≤ 0,35 s |

Comportements obligatoires (engine thread unique propriétaire du mixage, *pacing donné par* `out.write()` bloquant) :

1. **Décodage** : un sous-process `ffmpeg -hide_banner -loglevel error [-ss start] -i path -f f32le -ac 2 -ar <out.rate> pipe:1` par piste active + **la piste suivante ouverte ~2 s avant le fade** (jamais toute la soirée). Thread lecteur par pipe → `queue.Queue` de blocs 2048 frames. Sous-dépassement → zéros (pas de crack).
2. **Analyse** : enveloppe via `ffmpeg ... -ar 8000 -ac 1 -f f32le` (stream), RMS par fenêtre de 10 ms → 100 valeurs/s (mémoire O(10ms·durée)). Cue-in = 1er échantillon > seuil (−35 dBFS), cue-out = dernier. Cache RAM + `data/cues.json`.
3. **Zéro silence** : lecture démarre à `cue_in` ; crossfade amorce à `fade_start = max(cue_out - fade_len, première chute sous seuil proche fin)` ; **jamais** lu au-delà de `cue_out`. Fade-len = min(réglage, moitié de la durée utile).
4. **Crossfade** : gains croisés selon `crossfade_curve` sur `fade_len = min(réglage, moitié de la durée utile)` (courbe `cut` → ≤ 0,35 s), tout sur le mix stéréo.
5. **Ducking** : gain master : `volume * (duck_level si ducking)` rampe ~100 ms ; plus de silence brusque jamais.
6. **Corrompu/lire EOF trop tôt** → event `"corrupt"` + skip court fade vers la suivante (jamais de crash).
7. Events poussés via callback `events(event:str, data:dict)` : `"track_started"`, `"corrupt"`, `"ended_fade"`, `"queue_empty"`.
8. Pause = écriture de zéros (device chaud, reprise instantanée). Position = `start + frames_rendus/`rate.

## app/state.py

```python
def path(data_dir="data") -> str           # data/state.json
def load() -> dict                          # {} si absent/corrompu (jamais d'exception en lecture)
def save(state: dict) -> None               # atomique tmp+rename
SCHEMA: {"queue": [path,...], "playing": {"path": str|None, "pos": float},
         "settings": {"crossfade": 8.0, "crossfade_curve": "equal_power",
                      "volume": 1.0, "ducking": False,
                      "auto_queue": {"enabled": False, "min": 3, "fill": 10}},
         "bank": [path,...], "history": [path,...],   # ≤ 50, plus récent en premier
         "saved_at": iso}
# auto_queue : enabled bool, min 0..20, fill 1..50 (normalisés à chaque lecture/écriture)
def offer_restore() -> dict|None            # l'état si playing.path existe encore
```

## app/queue_rules.py

```python
def build_queue(bank, count, *, queue_paths=(), history=(), rng=None) -> list[str]
# règles pures, sans I/O ; chaque règle se relâche si le vivier ne suffit pas :
# 1. jamais de doublon (file existante + lot) ;
# 2. éviter l'historique (relâché du plus ancien d'abord) ;
# 3. même artiste espacé de ≥ 3 positions ;
# 4. pas deux fois le même dossier parent de suite ;
# 5. hasard pondéré sur le reste.
```

## app/ytdl.py

```python
class Downloader:
    def __init__(self, dest_dir: str, events_cb): ...
    def start(self, url: str) -> str        # job_id ; même URL queued/running → même id ;
                                            # ThreadPool max 2 ; jamais le thread serveur
    def jobs(self) -> list[dict]            # {"id","url","title","status","file"} (+ "err" si échec)
                                            # status in queued/running/done/error
    def shutdown(self) -> None              # kill des procs en cours, pool libéré
# subprocess: sys.executable -m yt_dlp -f "bestaudio/best" --no-playlist --no-part -o dest/%(title)s-%(id)s.%(ext)s
# timeout dur : 600 s par job (proc.kill + status error)
# events : "download_ready" {"file", "title"} → bank re-scan côté serveur ; "download_error" {"id"}
```

## app/server.py (FastAPI, port 8092)

- `GET  /api/state` → tout l'état UI en 1 réponse :
  `{playing, queue, bank, downloading, settings, restore_offer, history, preview}` ;
  `settings = {crossfade, crossfade_curve, volume, ducking, auto_queue}` ;
  `history` = `[path]` (≤ 50, plus récent en premier) ; `preview` = `{"path"} | null`.
  `playing = {path, pos, dur, fading, playing, incoming} | null`. Pendant un crossfade
  **naturel**, `path` reste la piste **sortante** (celle qu'on entend baisser) et
  `incoming = {path, pos, dur, cue_in, cue_out}` décrit l'**entrante** déjà audible ;
  hors transition `incoming` vaut `null` (clé toujours présente). L'UI affiche `incoming`
  tant que `fading` est vrai — sinon la barre paraît finie pendant que le son continue.
  Le tick SSE `pos` ne décrit que le sortant : ne pas s'en servir pour la vue en fade.
- `POST /api/play` | `/api/skip` | `/api/duck` (toggle)
- `POST /api/queue` {"paths"} (set_queue) ; `/api/queue/insert-next` {"path"} ; `/api/queue/remove` {"path"} ;
  `/api/queue/auto` {"count"?: 1..50, "replace"?: bool} → `{"ok", "added", "queue"}` (`queue` = nombre de morceaux à venir ; `replace` remplace la file à venir, sinon ajoute).
- `POST /api/volume` {"value"} ;
  `/api/settings` {"crossfade"?: 2..16, "crossfade_curve"?: "linear"|"equal_power"|"smooth"|"cut",
  "auto_queue"?: {"enabled", "min", "fill"}} → clés normalisées renvoyées ; `crossfade` est
  **borné** à 2..16 (jamais d'erreur pour une valeur numérique hors bornes) ; type non
  numérique ou courbe inconnue → 400.
- `POST /api/preview` {"path"} ; `POST /api/preview/stop` {} → `{"ok", "started", "queue"}`
  (restaure la vraie file ; `started` = elle jouait avant l'audition).
- `POST /api/download` {"url"} ; `POST /api/import` (multipart `files[]`)
- `POST /api/restore` {"confirm": bool} → applique (ou refuse) l'offre de reprise.
  `confirm: true` restaure la piste + sa position + la file ; `confirm: false` ne reprend
  **pas** la piste mais **conserve la file sauvée** (sinon l'autosave qui suit écrivait `[]`).
- `POST /api/bank/clear` {} (ou {"scope": "downloads"}) → `{"ok", "removed", "bank", "queue"}`.
  Supprime uniquement les fichiers de `downloads/` (récursif, `realpath` vérifié), purge
  banque/file/historique/cues et re-scanne. Le dossier musique personnel n'est **jamais** touché.
  409 `{"error": "téléchargement en cours"}` si un job tourne ; 400 pour un autre scope.
- `POST /api/quit` {} → `{"ok": true}` puis arrêt réel ~250 ms après (pid file supprimé,
  `_shutdown()` = pause + save + downloader, puis `os._exit(0)`). Idempotent.
- `GET  /api/waveform?path=...` (→ list[float])
- `GET  /api/events` (SSE : events engine + downloader + tick pos)
- Banque : `--music-dir` (répétable) > `AUTODJ_MUSIC_DIR` (liste `os.pathsep`) > `~/Music` / `./music` ;
  `downloads/` toujours inclus. Scan récursif au boot, ffprobe tags.
- Serve static `app/static` (index réécrit avec `?v=<mtime>`), `Cache-Control: no-store` partout.
  `python -m app.server --port 8092 --music-dir ... --data-dir data --downloads downloads`
- Autosave état (state.py) : positions toutes les 2 s + à chaque mutation + à l'arrêt.

## UI (app/static)

Console sombre : platine en haut (titre, waveform, transport, volume, ducking, réglages crossfade/auto-file), bibliothèque en dessous (file d'attente et banque côte à côte). Clé ESPACE = `/api/duck` (keydown preventDefault, anti-repeat) ; `N` suivant, `P` play/pause, `F` recherche, `A` auto-file, `±` volume, `Échap` quitte l'audition. Waveform canvas sur envelope, drag&drop banque→queue + réordonner, menu `⋮` / clic droit = « Placer en piste suivante », toasts discrets (download prêt, fichier corrompu → sauté), modale « Reprendre la soirée au même endroit ? » si `offer_restore`.

## Addenda U1/U2 (2026-11-11) — audition + import

```python
# app/engine.py (2 ajouts, style existant, cmds thread-safes)
def preview(self, path: str) -> None    # stoppe la lecture (fade 0.3 s) et joue `path` ALONE à cue_in,
                                        # sans queue ; fin naturelle → queue_empty + repos (jamais d'enchaînement)
def requeue(self, paths: list[str], start=False, cut=False) -> None
# set_queue + (si start et moteur au repos) démarre la 1re ; cut=True coupe l'audition audible
# status() gagne "preview": bool
```

- `POST /api/preview {path}` → `{"ok": true}` : suspend la file (real_queue mémorisée côté serveur) et lance l'audition. Fin naturelle → file restaurée **au repos** (le DJ appuie PLAY). Skip ou Play pendant l'audition → file restaurée + démarrage. Reorganisation de la file pendant l'audition → reprise de la main du DJ (flag purgé).
- `POST /api/preview/stop {}` → `{"ok": true, "started": bool, "queue": n}` : file restaurée ; `started` = elle jouait avant l'audition ; `queue` = nombre de morceaux restaurés.
- `POST /api/import` (multipart `files[]`) → `{"ok": true, "added": n, "skipped": m}` : extensions audio du contrat uniquement (mp3/wav/flac/m4a/aac/ogg, 400 sinon), max 200 Mo/fichier, destination `downloads/imports/` (collisions `-1`), banque re-scannée (scan récursif). Dédup par contenu (taille puis SHA-256) : un fichier byte-identique déjà présent dans `downloads/` n'est pas recopié (`skipped`), seul `added` compte les copies. Parseur multipart maison (`python-multipart` absent ; UploadFile natif si le paquet arrive un jour).
- `GET /api/state` : + `"preview": {"path"} | null` ; **`Cache-Control: no-store` sur toutes les réponses** (API + static).
- UI (U1) : platine en haut, file et banque côte à côte en dessous ; clic simple banque = insérer après le titre courant ; boutons `▶`/`⏸` (transport), `＋ Importer` (banque), `Remplir`/`Vider` (file), `Vider imports` (`downloads/`), `Tout couper` (arrêt serveur compris) ; dblclick = audition ; drop-zone OS + `＋ Importer` → `/api/import` ; badge ÉCOUTE sur `state.preview`.
