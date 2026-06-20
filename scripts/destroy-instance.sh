#!/usr/bin/env bash
# scripts/destroy-instance.sh NAME [--with-data]
#
# Supprime le container d'une instance Fansly bot.
# Par defaut : preserve data-NAME/ et .env.NAME (skip --with-data pour wipe complet).
#
# Usage :
#   ./scripts/destroy-instance.sh marie              # supprime juste le container
#   ./scripts/destroy-instance.sh marie --with-data  # supprime aussi data-NAME/ et .env.NAME
set -euo pipefail

if [[ -t 1 ]]; then
    BOLD=$'\033[1m'; DIM=$'\033[2m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'
    RED=$'\033[31m'; RESET=$'\033[0m'
else
    BOLD=""; DIM=""; GREEN=""; YELLOW=""; RED=""; RESET=""
fi

if [[ $# -lt 1 ]]; then
    echo "${BOLD}Usage:${RESET} $0 NAME [--with-data]"
    echo "  Sans --with-data : preserve data-NAME/ et .env.NAME."
    echo "  Avec --with-data : wipe TOUT (container + data + env). Irreversible."
    exit 1
fi

INSTANCE="$1"
shift
WITH_DATA=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --with-data) WITH_DATA=true; shift ;;
        *) echo "${RED}✗${RESET} Argument inconnu : $1"; exit 1 ;;
    esac
done

CONTAINER="fansly-bot-${INSTANCE}"
PROJECT="fansly-${INSTANCE}"
DATA_DIR="data-${INSTANCE}"
ENV_FILE=".env.${INSTANCE}"

# Confirmation
echo "${YELLOW}⚠${RESET} Tu vas detruire l'instance ${BOLD}${INSTANCE}${RESET} :"
echo "    container : ${CONTAINER}"
echo "    project   : ${PROJECT}"
if [[ "$WITH_DATA" == "true" ]]; then
    echo "    ${RED}+ donnees : ${DATA_DIR}/ et ${ENV_FILE} ${BOLD}(IRREVERSIBLE)${RESET}"
fi
read -r -p "  Confirmer ? Tape '${INSTANCE}' pour valider : " confirm
if [[ "$confirm" != "$INSTANCE" ]]; then
    echo "Annule."
    exit 0
fi

# Stop + remove container
if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
    docker compose --project-name "$PROJECT" down 2>/dev/null || docker rm -f "$CONTAINER" 2>/dev/null || true
    echo "${GREEN}✓${RESET} Container ${CONTAINER} supprime."
else
    echo "${DIM}Container ${CONTAINER} deja absent.${RESET}"
fi

# Wipe data si demande
if [[ "$WITH_DATA" == "true" ]]; then
    if [[ -d "$DATA_DIR" ]]; then
        rm -rf "$DATA_DIR"
        echo "${GREEN}✓${RESET} ${DATA_DIR}/ supprime."
    fi
    if [[ -f "$ENV_FILE" ]]; then
        rm -f "$ENV_FILE"
        echo "${GREEN}✓${RESET} ${ENV_FILE} supprime."
    fi
    if [[ -f "${ENV_FILE}.backup" ]]; then
        rm -f "${ENV_FILE}.backup"
    fi
fi
echo
echo "${GREEN}✓${RESET} Instance ${BOLD}${INSTANCE}${RESET} detruite."
