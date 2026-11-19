# PORTABILITÉ — Auto-DJ-V1

« Vérifié » = exécuté sur cette machine (Linux). Windows et macOS sont **implémentés et
couverts par la CI**, mais pas testés physiquement (aucun device audio Windows/macOS ici).

## Matrice

| | Linux | Windows | macOS |
| --- | --- | --- | --- |
| État | vérifié (cette machine) | implémenté + CI, non testé physiquement | implémenté + CI, non testé physiquement |
| Backend audio | ALSA natif (ctypes, `libasound.so.2`) | sounddevice / PortAudio (MME ou WASAPI) | sounddevice / PortAudio (CoreAudio) |
| Scripts | `setup.sh` / `lancer.sh` / `stop.sh` | `setup.ps1` / `lancer.ps1` / `stop.ps1` (PS 5.1+) | `setup.sh` / `lancer.sh` / `stop.sh` |
| CI | ubuntu-latest | windows-latest | macos-latest |

CI : `.github/workflows/tests.yml` — ffmpeg/ffprobe installés (sous Ubuntu, le paquet
`ffmpeg` tire `libasound2`, requis par les tests ALSA), `pip check`, import de
`app.server`, puis `pytest tests/` sur les 3 OS, headless (la sortie audio est injectée,
aucun device réel). Les 2 tests du backend ALSA (`app/output_alsa.py` charge
`libasound.so.2` dès l'import) sont désélectionnés hors Linux (`-k "not alsa"`) :
90 tests sous Linux, 88 sous Windows/macOS.

## Backends audio

Sélection automatique dans `Engine._default_out` (`app/engine.py`) :

1. **Linux** → `app/output_alsa.py` : ALSA direct via ctypes, `writei` bloquant = pacing
   temps réel. Aucun paquet Python en plus, pas de PortAudio.
2. **Linux, ALSA en échec** (device pris, PipeWire sans device ALSA direct) →
   `app/output_sd.py` (sounddevice) **si** installé à la main : `pip install sounddevice`
   + `libportaudio2`.
3. **Windows / macOS** → `app/output_sd.py` : device de sortie par défaut, `float32`
   stéréo au taux du device.

Sans backend, le démarrage s'arrête sur un message explicite (pas de stack trace) :
`sounddevice requis : pip install sounddevice` sous Windows/macOS, `aucune sortie audio —
ALSA : … ; sounddevice : …` sous Linux quand ALSA et le repli échouent tous les deux.

Dépendances : `libasound2` (Linux, `libasound2t64` ≥ 24.04) ; `sounddevice; sys_platform !=
"linux"` (pip l'installe sur Windows/macOS, ignore Linux, wheel avec PortAudio inclus) ;
`ffmpeg` **et** `ffprobe` dans le `PATH` partout ; Python 3.10+.

## Installation et lancement

**Linux**

```sh
./setup.sh          # venv .venv + dépendances (+ alerte si ffmpeg manque)
./lancer.sh         # arrière-plan, log data/server.log, ouvre l'UI
./lancer.sh 8093    # autre port (après ./stop.sh)
./lancer.sh --fg    # premier plan (debug, Ctrl-C)
./stop.sh           # arrêt propre (pid file, kill ciblé)
```

**Windows (PowerShell 5.1 ou 7)**

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
.\lancer.ps1              # arrière-plan, log data\server.log (stderr : data\server.err.log), ouvre l'UI
.\lancer.ps1 -Port 8093   # autre port (après .\stop.ps1)
.\lancer.ps1 -Setup       # réinstalle les dépendances puis lance
.\stop.ps1
```

Les `.ps1` sont en **UTF-8 avec BOM** : PowerShell 5.1 lit sinon les accents en ANSI
(mojibake). Ne pas réenregistrer sans BOM.

**macOS**

```sh
./setup.sh
./lancer.sh
./stop.sh
```

`lancer.sh` détecte `xdg-open` puis `open` ; aucun `setsid`, aucune dépendance à `/proc`.

**Raccourcis** — `./installer.sh` (Linux) écrit `~/.local/share/applications/auto-dj.desktop`
(menu applications) et le copie sur `~/Bureau` ou `~/Desktop`, icône `app/static/icon.png`.
`installer.ps1` (Windows) crée `Auto-DJ.lnk` sur le Bureau et dans le menu Démarrer, cible
`wscript.exe lancer.vbs` : aucune fenêtre au lancement, icône `icon.ico`. Suppression = effacer
ces raccourcis (aucune désinstallation, rien n'est copié ailleurs).

## Dossier musique

`--music-dir` (répétable) > `AUTODJ_MUSIC_DIR` (séparateur `os.pathsep` : `:` Linux/macOS,
`;` Windows) > `~/Music` et `./music` **s'ils existent** > `downloads/` toujours inclus.
Un chemin absent est ignoré sans crash et sans être créé ; le serveur ne crée que
`downloads/` au démarrage (`data/` est créé par `lancer.sh`/`lancer.ps1`, ou au premier
enregistrement d'état) ; les doublons sont dédupliqués en gardant l'ordre (`_music_dirs`,
`app/server.py`). Sans argument, `data/local.env` (non versionné, non créé automatiquement)
est chargé par `lancer.sh` :

```sh
AUTODJ_MUSIC_DIR="$HOME/Musique"
```

Sous Windows `data\local.env` n'est pas lu : `$env:AUTODJ_MUSIC_DIR = "D:\Ma musique"`
dans la session avant `.\lancer.ps1`.

## Dépannage

- **Pas de son (Linux)** : `aplay -l`, `wpctl status` ; le serveur doit tourner dans la
  même session que la carte. ALSA occupé → `sounddevice` + `libportaudio2`.
- **Pas de son (Windows/macOS)** : device par défaut du système, puis dans le venv
  `python -c "import sounddevice; print(sounddevice.query_devices())"`.
- **ffmpeg introuvable** : `sudo apt install ffmpeg` / `winget install Gyan.FFmpeg` /
  `brew install ffmpeg`, puis `ffmpeg -version` et `ffprobe -version`.
- **Port 8092 occupé** : `ss -ltnp | grep 8092` (`lsof -i :8092` sur macOS), `./stop.sh`,
  puis `./lancer.sh 8093` / `.\lancer.ps1 -Port 8093`. Une instance déjà lancée garde son
  port : les lanceurs le détectent et ouvrent l'URL réelle au lieu d'en démarrer une seconde.
- **« déjà lancé » alors que rien ne répond** : les scripts vérifient la cmdline
  (`-m app.server`, la seule forme utilisée par les lanceurs) et suppriment un pid file
  obsolète. Si le pid vivant est bien notre serveur mais que `/api/state` ne répond pas,
  `lancer` s'arrête et demande `./stop.sh` / `.\stop.ps1`. Inversement, si `/api/state`
  répond sur le port alors que le pid file est absent/obsolète, `lancer.sh` refuse de
  démarrer une 2e instance (elle échouerait sur le bind et le pid écrit serait mort).

## Limites connues

- Audio **non testé physiquement** sous Windows/macOS : seules la sélection de backend et
  le cycle `write`/`close` sont couverts (stream factice) ; latence/dropouts non vérifiés.
- Windows : `stop.ps1` utilise `TerminateProcess` (pas de SIGTERM) ; l'état est écrit à
  chaque changement et toutes les 2 s, donc la position peut reculer d'au plus ~2 s. Fermer
  la fenêtre PowerShell qui a lancé `lancer.ps1` peut tuer le serveur (console héritée).
- macOS : scripts en `sh` POSIX (bash 3.2 en mode POSIX), pas de bashisme.

## À valider sur une vraie machine Windows / macOS

1. `setup` puis `lancer` : venv, installation (dont `sounddevice`), UI ouverte, `/api/state` à 200.
2. Son réel : lecture, crossfade, ducking, absence de clics sur le device par défaut.
3. Import local (noms accentués, caractères interdits Windows `: * ? " < > |`) et yt-dlp
   (téléchargement, arrivée dans la banque, titres accentués dans l'UI).
4. `stop` : process tué, `data/server.pid` supprimé, relance immédiate possible.
