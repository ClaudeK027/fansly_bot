#!/usr/bin/env bash
# Entrypoint du container fansly-novnc.
#
# Orchestre :
#   1. xvfb (display virtuel :99)
#   2. x11vnc (expose :99 sur localhost:5900 sans password — accessible
#      uniquement depuis le container, pas l'exterieur)
#   3. websockify (bridge 5900 -> 6080 WebSocket pour noVNC, ecoute sur
#      0.0.0.0:6080 mais seul l'iframe du manager y accede via tunnel)
#   4. setup_auth_visual.py (lance Chromium visible et attend le login)
#
# Le script Python sort en exit 0 quand le login est detecte cote serveur,
# ce qui termine tous les processus xvfb/x11vnc/websockify (puisqu'on
# est le PID 1 et qu'on les a lances en background).

set -e

cleanup() {
    echo "[novnc] cleanup..."
    for pid in $XVFB_PID $X11VNC_PID $WEBSOCKIFY_PID; do
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            kill -TERM "$pid" 2>/dev/null || true
        fi
    done
    wait 2>/dev/null
}
trap cleanup EXIT INT TERM

echo "[novnc] starting Xvfb on :99 (1280x800x24)..."
Xvfb :99 -screen 0 1280x800x24 -nolisten tcp &
XVFB_PID=$!
sleep 1

if ! kill -0 "$XVFB_PID" 2>/dev/null; then
    echo "[novnc] FATAL: Xvfb failed to start"
    exit 1
fi

echo "[novnc] starting x11vnc on :99 -> localhost:5900..."
# -nopw       : pas de password (acces deja restreint au container)
# -listen     : ecoute seulement sur localhost (websockify y accede)
# -forever    : reste en vie meme apres deconnexion du client
# -shared     : autorise plusieurs clients simultanes
# -quiet      : moins de bruit dans les logs
# -bg ne marche pas dans un container, on lance en background bash
x11vnc -display :99 -nopw -listen localhost -forever -shared -quiet \
       -rfbport 5900 &
X11VNC_PID=$!
sleep 1

echo "[novnc] starting websockify 6080 -> 5900 (with noVNC web root)..."
# --web sert les fichiers statiques noVNC (HTML + JS) sur la racine.
# Acces final : http://localhost:6080/vnc.html?autoconnect=1
websockify --web=/usr/share/novnc 6080 localhost:5900 &
WEBSOCKIFY_PID=$!
sleep 1

echo "[novnc] all services up. Launching Chromium auth flow..."
exec python /app/setup_auth_visual.py
