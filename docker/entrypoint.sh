#!/usr/bin/env bash
# Entrypoint du container Fansly bot.
#
# Strategie :
#  1. Au demarrage du container, on lance le worker en background (detache)
#     si AUTOSTART_WORKER=true (defaut). Le worker tournera independamment.
#  2. On lance Streamlit au foreground (PID 1 via exec). Le container vit
#     tant que Streamlit vit. Si Streamlit plante, Docker relance le
#     container (restart: unless-stopped) -> le worker redemarrera aussi.
#  3. L UI permet Arreter / Forcer / Lancer pendant la vie du container :
#     ces actions tuent ou relancent le worker SEUL, sans toucher Streamlit.

set -euo pipefail

cd /app

# Le dossier data/ est monte en volume — on s assure que ses sous-dossiers
# existent pour eviter une erreur au premier demarrage (volume vide).
mkdir -p \
    /app/data/logs \
    /app/data/logs/jobs \
    /app/data/Medias \
    /app/data/Captions \
    /app/data/browser_profile \
    /app/data/artifacts

# Lancement automatique du worker (configurable via env var)
if [[ "${AUTOSTART_WORKER:-true}" == "true" ]]; then
    # Si un PID file traine d un container precedent et pointe vers un
    # process qui n existe plus dans ce nouveau container, on nettoie pour
    # que le worker puisse demarrer proprement.
    if [[ -f /app/data/worker.pid ]]; then
        stale_pid=$(cat /app/data/worker.pid 2>/dev/null || echo "")
        if [[ -n "$stale_pid" ]] && ! kill -0 "$stale_pid" 2>/dev/null; then
            echo "[entrypoint] PID file residuel ($stale_pid) — nettoyage."
            rm -f /app/data/worker.pid \
                  /app/data/worker.state \
                  /app/data/worker.started_at \
                  /app/data/worker.pid.lock
        fi
    fi

    echo "[entrypoint] Lancement automatique du worker en background..."
    # setsid : detache le worker de l entrypoint pour qu il survive a la
    # fin de l entrypoint et puisse etre arrete/relance via l UI.
    setsid python -m fansly_bot worker \
        >> /app/data/logs/worker.stdout 2>&1 &
    # Petit delai pour laisser le worker ecrire son PID file avant que
    # Streamlit ne demarre et ne le lise.
    sleep 1
else
    echo "[entrypoint] AUTOSTART_WORKER=false — worker non lance au boot."
    echo "[entrypoint] Cliquer 'Lancer' dans l UI pour le demarrer."
fi

# Streamlit en foreground via exec (PID 1 == streamlit pour les signaux Docker)
echo "[entrypoint] Lancement de Streamlit sur 0.0.0.0:8501..."
exec streamlit run /app/src/fansly_dashboard/main.py \
    --server.address=0.0.0.0 \
    --server.port=8501 \
    --server.headless=true \
    --browser.gatherUsageStats=false \
    --server.enableCORS=false \
    --server.enableXsrfProtection=true
