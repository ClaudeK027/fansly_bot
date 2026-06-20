#!/usr/bin/env bash
# scripts/stop-instance.sh NAME
#
# Stoppe le container d'une instance Fansly bot sans la detruire (data
# preservee, peut etre relancee avec start-instance.sh).
set -euo pipefail

if [[ -t 1 ]]; then
    BOLD=$'\033[1m'; GREEN=$'\033[32m'; RED=$'\033[31m'; RESET=$'\033[0m'
else
    BOLD=""; GREEN=""; RED=""; RESET=""
fi

if [[ $# -lt 1 ]]; then
    echo "${BOLD}Usage:${RESET} $0 NAME"
    echo "  NAME : nom de l'instance (default si single-instance)"
    exit 1
fi

INSTANCE="$1"
CONTAINER="fansly-bot-${INSTANCE}"

if ! docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
    echo "${RED}✗${RESET} Container ${BOLD}${CONTAINER}${RESET} introuvable."
    echo "  Liste : ${BOLD}./scripts/list-instances.sh${RESET}"
    exit 1
fi

docker stop "$CONTAINER" >/dev/null
echo "${GREEN}✓${RESET} Instance ${BOLD}${INSTANCE}${RESET} arretee."
echo "  Pour redemarrer : ${BOLD}./scripts/start-instance.sh ${INSTANCE}${RESET}"
