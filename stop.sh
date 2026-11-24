#!/bin/sh
# Seamless — arrête proprement le serveur (pid file, kill ciblé).
cd "$(dirname "$0")" || exit 1
[ -f data/server.pid ] || { echo "pas de data/server.pid"; exit 0; }
PID="$(cat data/server.pid)"
if [ -z "$PID" ] || ! kill -0 "$PID" 2>/dev/null; then
  echo "pid $PID déjà mort"
  rm -f data/server.pid
  exit 0
fi
# filet : ne tuer que si c'est bien notre serveur (ps portable Linux + macOS)
ARGS="$(ps -ww -p "$PID" -o args= 2>/dev/null)"
case "$ARGS" in
  *"-m app.server"*)
    kill "$PID" 2>/dev/null
    i=0
    while kill -0 "$PID" 2>/dev/null && [ $i -lt 20 ]; do i=$((i + 1)); sleep 0.25; done
    if kill -0 "$PID" 2>/dev/null; then
      kill -9 "$PID" 2>/dev/null
      sleep 0.25
    fi
    if kill -0 "$PID" 2>/dev/null; then
      echo "pid $PID résiste — pid file conservé (data/server.pid)"
      exit 1
    fi
    echo "arrêté (pid $PID)"
    ;;
  "")
    echo "pid $PID : cmdline illisible — pas tué ; pid file supprimé"
    ;;
  *)
    echo "pid $PID : cmdline sans '-m app.server' — pas tué ; pid file supprimé"
    ;;
esac
rm -f data/server.pid
