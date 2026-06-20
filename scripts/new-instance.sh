#!/usr/bin/env bash
# scripts/new-instance.sh
#
# Orchestrateur multi-instance : cree une nouvelle instance bot complete
# (compte Fansly distinct) en une seule commande.
#
# Sous le capot :
#   1. valide le nom de l'instance
#   2. trouve un port host libre (8501, 8502, ...)
#   3. lance init-env.sh --instance NAME pour saisir les identifiants
#   4. lance setup-auth.sh --instance NAME pour le login Fansly
#   5. demarre le container avec project-name dedie via docker compose
#
# Usage :
#   ./scripts/new-instance.sh NAME
#   ./scripts/new-instance.sh marie
#
# Apres : http://localhost:<port> pour le dashboard de cette instance.

set -euo pipefail

# ─── Couleurs ────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
    BOLD=$'\033[1m'
    DIM=$'\033[2m'
    GREEN=$'\033[32m'
    YELLOW=$'\033[33m'
    RED=$'\033[31m'
    BLUE=$'\033[34m'
    RESET=$'\033[0m'
else
    BOLD=""; DIM=""; GREEN=""; YELLOW=""; RED=""; BLUE=""; RESET=""
fi

print_step()  { echo "${BOLD}${BLUE}→${RESET} ${BOLD}$1${RESET}"; }
print_ok()    { echo "${GREEN}✓${RESET} $1"; }
print_warn()  { echo "${YELLOW}⚠${RESET}  $1"; }
print_error() { echo "${RED}✗${RESET} $1" >&2; }

# ─── Arguments ───────────────────────────────────────────────────────────
if [[ $# -lt 1 ]]; then
    cat <<EOF
${BOLD}Usage:${RESET} $0 NAME [--start-only]

NAME : identifiant de l'instance (lettres, chiffres, _, max 32 chars).

Options :
  --start-only  : skip init-env et setup-auth (instance deja configuree),
                  demarre juste le container.

Exemple :
  $0 marie         # creation complete (env + auth + start)
  $0 marie --start-only   # juste demarrer (apres maintenance)
EOF
    exit 1
fi

INSTANCE="$1"
shift
START_ONLY=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --start-only) START_ONLY=true; shift ;;
        *) print_error "Argument inconnu : $1"; exit 1 ;;
    esac
done

# ─── Validation du nom ───────────────────────────────────────────────────
if [[ ! "$INSTANCE" =~ ^[a-zA-Z0-9_]{1,32}$ ]]; then
    print_error "Nom d'instance invalide : '$INSTANCE'"
    print_error "Format requis : 1-32 chars, lettres/chiffres/_ uniquement."
    exit 1
fi

# ─── Verification PWD ────────────────────────────────────────────────────
if [[ ! -f "./pyproject.toml" ]] || [[ ! -d "./src/fansly_bot" ]]; then
    print_error "Ce script doit etre lance depuis la racine du projet."
    exit 1
fi

ENV_FILE=".env.${INSTANCE}"
DATA_DIR="data-${INSTANCE}"
PROJECT_NAME="fansly-${INSTANCE}"

# ─── Trouve un port host libre, en partant de 8501 ───────────────────────
find_free_port() {
    local port=8501
    while [[ $port -lt 8600 ]]; do
        # Verifie si le port est deja utilise par un container fansly-bot-*
        if ! docker ps --format '{{.Ports}}' 2>/dev/null | grep -q "127.0.0.1:${port}->"; then
            # Verifie qu'il n'est pas utilise par autre chose
            if ! lsof -nP -iTCP:"${port}" -sTCP:LISTEN 2>/dev/null | grep -q LISTEN; then
                echo "$port"
                return 0
            fi
        fi
        port=$((port + 1))
    done
    print_error "Aucun port libre trouve entre 8501 et 8599."
    return 1
}

# ─── Header ──────────────────────────────────────────────────────────────
echo
echo "${BOLD}${BLUE}╔══════════════════════════════════════════════════════════╗${RESET}"
echo "${BOLD}${BLUE}║  Fansly Bot — Nouvelle instance : ${INSTANCE}${RESET}"
echo "${BOLD}${BLUE}╚══════════════════════════════════════════════════════════╝${RESET}"
echo

# ─── Si START_ONLY : skip init+auth ──────────────────────────────────────
if [[ "$START_ONLY" == "true" ]]; then
    if [[ ! -f "$ENV_FILE" ]]; then
        print_error "$ENV_FILE introuvable. Lance d'abord sans --start-only."
        exit 1
    fi
    if [[ ! -d "$DATA_DIR/browser_profile" ]]; then
        print_error "$DATA_DIR/browser_profile/ introuvable. Lance d'abord sans --start-only."
        exit 1
    fi
    print_step "Mode --start-only : skip init-env et setup-auth"
else
    # ─── 1) init-env ─────────────────────────────────────────────────────
    print_step "Etape 1/3 : Saisie des identifiants Fansly (${ENV_FILE})"
    ./scripts/init-env.sh --instance "$INSTANCE" < /dev/tty
    # Note : init-env va potentiellement exec setup-auth direct. On detecte
    # cela : si setup-auth a deja tourne (data-NAME/browser_profile/ existe),
    # on skip l'etape 2.
    if [[ -d "$DATA_DIR/browser_profile" ]]; then
        print_ok "Setup-auth deja effectue par init-env."
    else
        # ─── 2) setup-auth ───────────────────────────────────────────────
        print_step "Etape 2/3 : Authentification Fansly (Chromium visible)"
        ./scripts/setup-auth.sh --instance "$INSTANCE" < /dev/tty
    fi
fi

# ─── 3) demarrage du container ───────────────────────────────────────────
print_step "Etape 3/3 : Demarrage du container Docker"
HOST_PORT=$(find_free_port)
print_ok "Port host libre detecte : ${BOLD}${HOST_PORT}${RESET}"

# Cree le dossier data-NAME/ s'il n'existe pas (les sous-dossiers seront
# crees par entrypoint.sh au demarrage du container).
mkdir -p "$DATA_DIR"

INSTANCE_NAME="$INSTANCE" \
ENV_FILE="$ENV_FILE" \
DATA_DIR="./$DATA_DIR" \
HOST_PORT="$HOST_PORT" \
    docker compose --project-name "$PROJECT_NAME" up -d --build

echo
print_ok "${BOLD}Instance ${INSTANCE} demarree.${RESET}"
echo "  ${DIM}Container : fansly-bot-${INSTANCE}${RESET}"
echo "  ${DIM}Project    : ${PROJECT_NAME}${RESET}"
echo "  ${DIM}Dashboard  : http://localhost:${HOST_PORT}${RESET}"
echo "  ${DIM}env file   : ${ENV_FILE}${RESET}"
echo "  ${DIM}data dir   : ./${DATA_DIR}/${RESET}"
echo
echo "  Pour stopper : ${BOLD}./scripts/stop-instance.sh ${INSTANCE}${RESET}"
echo "  Pour lister  : ${BOLD}./scripts/list-instances.sh${RESET}"
echo
