#!/bin/sh
# Auto-DJ — installe un raccourci de bureau et une entrée de menu (Linux).
# Usage : ./installer.sh
# Crée ~/.local/share/applications/auto-dj.desktop (+ copie sur le Bureau s'il existe).
cd "$(dirname "$0")" || exit 1
ABS=$(pwd)

APP_DIR="$HOME/.local/share/applications"
DESKTOP_FILE="$APP_DIR/auto-dj.desktop"

chmod +x lancer.sh stop.sh 2>/dev/null

# guillemets seulement si le chemin contient une espace (Exec n'est pas un shell)
q() { case "$1" in *" "*) printf '"%s"' "$1" ;; *) printf '%s' "$1" ;; esac; }

mkdir -p "$APP_DIR" || { echo "impossible de créer $APP_DIR"; exit 1; }
cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=Auto-DJ
Comment=DJ de soirée autonome
Exec=$(q "$ABS/lancer.sh")
Path=$ABS
Icon=$ABS/app/static/icon.png
Terminal=false
Categories=AudioVideo;Audio;Player;
StartupNotify=false
EOF
chmod +x "$DESKTOP_FILE"
echo "entrée de menu : $DESKTOP_FILE"

# copie sur le Bureau (premier dossier existant)
for d in "$HOME/Bureau" "$HOME/Desktop"; do
  if [ -d "$d" ]; then
    cp "$DESKTOP_FILE" "$d/auto-dj.desktop" && chmod +x "$d/auto-dj.desktop" \
      && echo "raccourci bureau : $d/auto-dj.desktop"
    # GNOME : marquer comme fiable (sinon « lanceur non approuvé »)
    command -v gio >/dev/null 2>&1 && \
      gio set "$d/auto-dj.desktop" metadata::trusted true 2>/dev/null
    break
  fi
done

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APP_DIR" 2>/dev/null
fi

echo "terminé — Auto-DJ est dans le menu applications ; désinstallation : rm \"$DESKTOP_FILE\""
exit 0
