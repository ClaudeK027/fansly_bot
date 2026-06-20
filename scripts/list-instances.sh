#!/usr/bin/env bash
# scripts/list-instances.sh
#
# Liste les containers Fansly bot actuellement crees (running ou stopped).
# Format : nom | status | port host | data dir | env file
#
# Usage : ./scripts/list-instances.sh

set -euo pipefail

if [[ -t 1 ]]; then
    BOLD=$'\033[1m'; DIM=$'\033[2m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'
    RED=$'\033[31m'; RESET=$'\033[0m'
else
    BOLD=""; DIM=""; GREEN=""; YELLOW=""; RED=""; RESET=""
fi

# Recupere tous les containers fansly-bot-* (bash 3.x compatible : pas de mapfile)
container_list=$(
    docker ps -a --filter "name=fansly-bot-" \
        --format '{{.Names}}|{{.Status}}|{{.Ports}}' 2>/dev/null \
        | sort
)

if [[ -z "$container_list" ]]; then
    echo "${DIM}Aucune instance Fansly bot trouvee.${RESET}"
    echo "  Cree-en une avec : ${BOLD}./scripts/new-instance.sh NAME${RESET}"
    exit 0
fi

printf "${BOLD}%-30s %-25s %-15s${RESET}\n" "INSTANCE" "STATUS" "DASHBOARD"
echo "────────────────────────────────────────────────────────────────────────"

while IFS='|' read -r name status ports; do
    [[ -z "$name" ]] && continue
    instance="${name#fansly-bot-}"
    # Extrait le port host de la ligne ports : "127.0.0.1:8501->8501/tcp, ..."
    host_port=$(echo "$ports" | grep -oE '127\.0\.0\.1:[0-9]+' | head -1 | cut -d: -f2)
    if [[ -z "$host_port" ]]; then host_port="-"; fi
    # Status court + couleur
    if [[ "$status" =~ ^Up ]]; then
        status_color="${GREEN}● up${RESET}    ${DIM}${status#Up }${RESET}"
        dashboard="http://localhost:${host_port}"
    elif [[ "$status" =~ ^Exited ]]; then
        status_color="${RED}● down${RESET}  ${DIM}${status#Exited }${RESET}"
        dashboard="-"
    else
        status_color="${YELLOW}● ?${RESET}     ${DIM}${status}${RESET}"
        dashboard="-"
    fi
    printf "%-30s %-25s %s\n" "$instance" "$(printf '%b' "$status_color")" "$dashboard"
done <<< "$container_list"

echo
echo "  Demarrer une instance arretee : ${BOLD}./scripts/start-instance.sh NAME${RESET}"
echo "  Stopper une instance running : ${BOLD}./scripts/stop-instance.sh NAME${RESET}"
echo "  Detruire une instance        : ${BOLD}./scripts/destroy-instance.sh NAME${RESET}"
echo "  Nouvelle instance            : ${BOLD}./scripts/new-instance.sh NAME${RESET}"
