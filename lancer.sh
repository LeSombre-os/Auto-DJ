#!/bin/sh
# Seamless — lance le serveur en arrière-plan et ouvre l'UI.
# Usage : ./lancer.sh [port]          (défaut 8092)
#         ./lancer.sh --fg [port]     (premier plan, pour debug)
cd "$(dirname "$0")" || exit 1
# config locale non versionnée (ex. SEAMLESS_MUSIC_DIR=...) : exportée vers le serveur
if [ -f data/local.env ]; then set -a; . ./data/local.env; set +a; fi
mkdir -p data

# interpréteur : SEAMLESS_PY > .venv > ~/.venvs/seamless > ~/.venvs/autodj > python3
PY=""
for c in "${SEAMLESS_PY:-}" ".venv/bin/python" "$HOME/.venvs/seamless/bin/python" \
         "$HOME/.venvs/autodj/bin/python" python3; do
  [ -n "$c" ] || continue
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
[ -n "$PY" ] || { echo "python introuvable — installez Python 3 puis ./setup.sh"; exit 1; }
"$PY" -c "import fastapi, numpy, uvicorn" 2>/dev/null || {
  echo "$PY : fastapi/numpy/uvicorn absents — lancez ./setup.sh"; exit 1; }

open_ui() {
  if command -v xdg-open >/dev/null 2>&1; then xdg-open "$1" >/dev/null 2>&1 &
  elif command -v open >/dev/null 2>&1; then open "$1" >/dev/null 2>&1 &
  fi
}

case "$1" in
  --fg) shift; exec "$PY" -m app.server --port "${1:-8092}" ;;
esac
PORT="${1:-8092}"
command -v curl >/dev/null 2>&1 || {
  echo "curl introuvable — installez curl (contrôle de démarrage)"; exit 1; }
# déjà lancé ? (pid file + cmdline : un pid recyclé n'est pas notre serveur)
if [ -f data/server.pid ]; then
  OLD="$(cat data/server.pid)"
  ARGS=""
  [ -n "$OLD" ] && ARGS="$(ps -ww -p "$OLD" -o args= 2>/dev/null)"
  case "$ARGS" in
    *"-m app.server"*)
      # port réellement utilisé par l'instance en cours
      RUN_PORT="$(printf '%s\n' "$ARGS" | sed -n 's/.*--port[ =]\([0-9][0-9]*\).*/\1/p')"
      RUN_PORT="${RUN_PORT:-8092}"        # sans --port, app.server écoute 8092
      if ! curl -sf -o /dev/null "http://127.0.0.1:$RUN_PORT/api/state"; then
        echo "pid $OLD (app.server) mais /api/state muet sur $RUN_PORT — arrêtez-le : ./stop.sh"
        exit 1
      fi
      echo "déjà lancé (pid $OLD) — http://127.0.0.1:$RUN_PORT/"
      open_ui "http://127.0.0.1:$RUN_PORT/"
      exit 0
      ;;
    "")
      rm -f data/server.pid
      ;;
    *)
      echo "pid $OLD : cmdline sans '-m app.server' — pid file obsolète, ignoré"
      rm -f data/server.pid
      ;;
  esac
fi
# Seamless répond déjà sur ce port alors que le pid file est absent/obsolète :
# lancer une 2e instance échouerait sur le bind, mais /api/state répondrait pour
# l'ancienne -> « en ligne » mensonger et pid file pointant sur un process mort.
if curl -sf -o /dev/null "http://127.0.0.1:$PORT/api/state"; then
  echo "Seamless répond déjà sur $PORT sans pid file valide — arrêtez-le (puis relancez) ou changez de port"
  exit 1
fi
nohup "$PY" -m app.server --port "$PORT" >> data/server.log 2>&1 < /dev/null &
echo $! > data/server.pid
# attente prêt : poll /api/state, max ~15 s
i=0
while [ $i -lt 60 ]; do
  if curl -sf -o /dev/null "http://127.0.0.1:$PORT/api/state"; then break; fi
  i=$((i + 1))
  sleep 0.25
done
if [ $i -ge 60 ]; then
  echo "serveur non joignable après 15 s — journal :"
  tail -n 8 data/server.log 2>/dev/null
  kill "$(cat data/server.pid)" 2>/dev/null
  rm -f data/server.pid
  exit 1
fi
open_ui "http://127.0.0.1:$PORT/"
echo "Seamless en ligne : http://127.0.0.1:$PORT/ (pid $(cat data/server.pid))"
