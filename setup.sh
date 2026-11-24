#!/bin/sh
# Seamless — crée .venv et installe les dépendances (à lancer une fois).
cd "$(dirname "$0")" || exit 1
PY="${SEAMLESS_PY:-python3}"
command -v "$PY" >/dev/null 2>&1 || {
  echo "Python 3 introuvable — Linux : sudo apt install python3 python3-venv ; macOS : brew install python"
  exit 1; }
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || {
  echo "Python 3.10+ requis (trouvé : $("$PY" -V 2>&1))"; exit 1; }
[ -x .venv/bin/python ] || "$PY" -m venv .venv || {
  echo "création du venv impossible (Debian/Ubuntu : sudo apt install python3-venv)"; exit 1; }
.venv/bin/python -m pip install -r requirements.txt || { echo "pip install a échoué"; exit 1; }
# ffmpeg/ffprobe : binaires externes (décodage + métadonnées) — jamais installés ici
MISSING=""
for b in ffmpeg ffprobe; do command -v "$b" >/dev/null 2>&1 || MISSING="$MISSING $b"; done
if [ -n "$MISSING" ]; then
  case "$(uname -s)" in
    Darwin) HOW="brew install ffmpeg" ;;
    Linux)  HOW="sudo apt install ffmpeg" ;;
    *)      HOW="installez ffmpeg pour votre OS" ;;
  esac
  echo "manquant :$MISSING — installation : $HOW"
fi
# non-Linux : sounddevice (PortAudio) doit s'importer
if [ "$(uname -s)" != "Linux" ] && ! .venv/bin/python -c "import sounddevice" 2>/dev/null; then
  echo "sounddevice indisponible — macOS : brew install portaudio puis relancez ce script"
fi
echo "ok — lancez ./lancer.sh"
