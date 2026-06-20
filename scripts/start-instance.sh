#!/usr/bin/env bash
# scripts/start-instance.sh NAME
#
# Redemarre un container Fansly bot stoppe (sans rebuild).
set -euo pipefail

if [[ -t 1 ]]; then
    BOLD=$'\033[1m'; DIM=$'\033[2m'; GREEN=$'\033[32m'; RED=$'\033[31m'; RESET=$'\033[0m'
else
    BOLD=""; DIM=""; GREEN=""; RED=""; RESET=""
fi

if [[ $# -lt 1 ]]; then
    echo "${BOLD}Usage:${RESET} $0 NAME"
    exit 1
fi

INSTANCE="$1"
CONTAINER="fansly-bot-${INSTANCE}"

if ! docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
    echo "${RED}✗${RESET} Container ${BOLD}${CONTAINER}${RESET} introuvable."
    echo "  Cree-le avec : ${BOLD}./scripts/new-instance.sh ${INSTANCE}${RESET}"
    exit 1
fi

docker start "$CONTAINER" >/dev/null

# Recupere le port mappe
PORT=$(docker port "$CONTAINER" 8501/tcp 2>/dev/null | grep -oE '[0-9]+$' | head -1)
echo "${GREEN}✓${RESET} Instance ${BOLD}${INSTANCE}${RESET} demarree."
if [[ -n "$PORT" ]]; then
    echo "  ${DIM}Dashboard : http://localhost:${PORT}${RESET}"
fi
