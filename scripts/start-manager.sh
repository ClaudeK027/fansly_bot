#!/usr/bin/env bash
# scripts/start-manager.sh
#
# Lance le Fansly Manager (UI Streamlit unifie multi-comptes) sur le
# port 8500. Le manager liste/start/stop les instances bot voisines
# via le socket Docker.
#
# Usage :
#   ./scripts/start-manager.sh         # demarre (build si necessaire)
#   ./scripts/start-manager.sh --stop  # arrete
#   ./scripts/start-manager.sh --logs  # tail logs
set -euo pipefail

if [[ -t 1 ]]; then
    BOLD=$'\033[1m'; DIM=$'\033[2m'; GREEN=$'\033[32m'; RESET=$'\033[0m'
else
    BOLD=""; DIM=""; GREEN=""; RESET=""
fi

COMPOSE_FILE="docker-compose.manager.yml"
PROJECT_NAME="fansly-manager"

action="${1:-up}"

case "$action" in
    up|--up|"")
        docker compose -f "$COMPOSE_FILE" --project-name "$PROJECT_NAME" up -d --build
        echo
        echo "${GREEN}✓${RESET} Manager demarre."
        echo "  ${DIM}Container : fansly-manager${RESET}"
        echo "  ${DIM}Dashboard : http://localhost:8500${RESET}"
        echo
        echo "  Si tu es sur un VPS, ouvre un tunnel SSH depuis ton poste :"
        echo "    ${BOLD}ssh -L 8500:localhost:8500 user@vps${RESET}"
        echo "  puis http://localhost:8500 dans ton navigateur local."
        ;;
    stop|--stop|down|--down)
        docker compose -f "$COMPOSE_FILE" --project-name "$PROJECT_NAME" down
        echo "${GREEN}✓${RESET} Manager arrete."
        ;;
    logs|--logs)
        docker compose -f "$COMPOSE_FILE" --project-name "$PROJECT_NAME" logs -f
        ;;
    *)
        echo "Usage: $0 [up|stop|logs]"
        exit 1
        ;;
esac
