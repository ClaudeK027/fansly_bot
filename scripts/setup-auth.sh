#!/usr/bin/env bash
# scripts/setup-auth.sh
#
# Authentifie le bot auprès de Fansly en lançant Chromium en mode visible
# pour permettre à l'utilisateur de se connecter manuellement.
#
# Crée un venv Python jetable, installe juste ce qu'il faut, lance
# setup-auth, puis nettoie. À l'arrivée, le dossier data/browser_profile/
# contient la session authentifiée, utilisable ensuite par Docker.
#
# Prérequis : Python 3.11+ sur la machine.
#
# Usage :
#   ./scripts/setup-auth.sh

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

print_header() {
    echo
    echo "${BOLD}${BLUE}╔══════════════════════════════════════════════════════════╗${RESET}"
    echo "${BOLD}${BLUE}║  Fansly Bot — Authentification initiale (setup-auth)    ║${RESET}"
    echo "${BOLD}${BLUE}╚══════════════════════════════════════════════════════════╝${RESET}"
    echo
}

print_step()  { echo "${BOLD}${BLUE}→${RESET} ${BOLD}$1${RESET}"; }
print_ok()    { echo "${GREEN}✓${RESET} $1"; }
print_warn()  { echo "${YELLOW}⚠${RESET}  $1"; }
print_error() { echo "${RED}✗${RESET} $1" >&2; }

# ─── Vérifications ───────────────────────────────────────────────────────
check_pwd() {
    if [[ ! -f "./pyproject.toml" ]] || [[ ! -d "./src/fansly_bot" ]]; then
        print_error "Ce script doit être lancé depuis la racine du projet."
        exit 1
    fi
}

# ─── Parse arguments : --instance NAME (multi-instance) ──────────────────
# Sans --instance : comportement legacy (single-instance, lit .env et
# stocke dans data/browser_profile/).
# Avec --instance NAME : lit .env.NAME et stocke dans data-NAME/browser_profile/.
INSTANCE=""
ENV_FILE=".env"
DATA_DIR="data"
parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --instance)
                INSTANCE="$2"
                shift 2
                ;;
            --instance=*)
                INSTANCE="${1#--instance=}"
                shift
                ;;
            -h|--help)
                cat <<EOF
Usage: $0 [--instance NAME]

Sans --instance : utilise .env et data/browser_profile/ (single-instance).
Avec --instance NAME : utilise .env.NAME et data-NAME/browser_profile/.
EOF
                exit 0
                ;;
            *)
                print_error "Argument inconnu : $1"
                exit 1
                ;;
        esac
    done
    if [[ -n "$INSTANCE" ]]; then
        if [[ ! "$INSTANCE" =~ ^[a-zA-Z0-9_]{1,32}$ ]]; then
            print_error "Nom d'instance invalide : '$INSTANCE'"
            exit 1
        fi
        ENV_FILE=".env.$INSTANCE"
        DATA_DIR="data-$INSTANCE"
    fi
}

check_env_file() {
    if [[ ! -f "$ENV_FILE" ]]; then
        print_error "Le fichier $ENV_FILE est manquant."
        if [[ -n "$INSTANCE" ]]; then
            print_error "Lance d'abord : ${BOLD}./scripts/init-env.sh --instance $INSTANCE${RESET}"
        else
            print_error "Lance d'abord : ${BOLD}./scripts/init-env.sh${RESET}"
        fi
        exit 1
    fi
    # Validation rapide : les 3 variables doivent être présentes
    for var in FANSLY_USERNAME FANSLY_PASSWORD FANSLY_PROFILE_SLUG; do
        if ! grep -qE "^${var}=" "$ENV_FILE"; then
            print_warn "$ENV_FILE n'a pas la variable ${BOLD}${var}${RESET}."
            if [[ -n "$INSTANCE" ]]; then
                print_warn "Relance ${BOLD}./scripts/init-env.sh --instance $INSTANCE${RESET}."
            else
                print_warn "Relance ${BOLD}./scripts/init-env.sh${RESET}."
            fi
        fi
    done
}

find_python() {
    # Cherche un Python 3.11+
    for candidate in python3.13 python3.12 python3.11 python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then
            # Vérifie la version
            if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
                echo "$candidate"
                return 0
            fi
        fi
    done
    return 1
}

# ─── Setup du venv jetable ───────────────────────────────────────────────
VENV_DIR=".venv-setup-auth"

cleanup_venv() {
    if [[ -d "$VENV_DIR" ]]; then
        rm -rf "$VENV_DIR"
        print_ok "Environnement jetable nettoyé : ${BOLD}$VENV_DIR${RESET}"
    fi
}

# Fix bug libexpat sur macOS Apple Silicon : le Python Homebrew lie pyexpat
# dynamiquement contre /usr/lib/libexpat.1.dylib (version systeme) au lieu
# de /opt/homebrew/opt/expat/lib/libexpat.dylib. Symptome : `python -m venv`
# echoue sur `ensurepip` avec un Symbol not found _XML_GetCurrentByteIndex.
# Solution : exporter DYLD_LIBRARY_PATH si on est sur macOS arm64 et que
# expat est present via brew. Autoset = aucune action requise de l'utilisateur.
setup_macos_expat_workaround() {
    if [[ "$(uname -s)" != "Darwin" ]]; then return 0; fi
    if [[ "$(uname -m)" != "arm64" ]]; then return 0; fi
    local expat_lib="/opt/homebrew/opt/expat/lib"
    if [[ ! -d "$expat_lib" ]]; then return 0; fi
    if [[ ":${DYLD_LIBRARY_PATH:-}:" == *":$expat_lib:"* ]]; then return 0; fi
    export DYLD_LIBRARY_PATH="$expat_lib${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"
    print_ok "macOS arm64 : DYLD_LIBRARY_PATH ajuste pour libexpat (auto)"
}

create_venv() {
    local python_cmd="$1"
    print_step "Création de l'environnement Python jetable"
    echo "  ${DIM}Utilisation de : $($python_cmd --version 2>&1)${RESET}"
    setup_macos_expat_workaround
    "$python_cmd" -m venv "$VENV_DIR"
    print_ok "Venv créé : ${BOLD}$VENV_DIR${RESET}"
}

install_deps() {
    print_step "Installation des dépendances (Playwright + Chromium)"
    echo "  ${DIM}Cela peut prendre 1-2 minutes au premier lancement...${RESET}"
    # shellcheck source=/dev/null
    source "$VENV_DIR/bin/activate"
    pip install --quiet --upgrade pip
    pip install --quiet \
        "playwright>=1.47.0" \
        "playwright-stealth>=2.0.0" \
        "structlog>=24.1.0" \
        "pendulum>=3.0.0" \
        "pydantic>=2.7.0" \
        "pydantic-settings>=2.4.0" \
        "pyyaml>=6.0.1" \
        "ruamel.yaml>=0.18.0" \
        "tenacity>=8.2.3"
    print_ok "Dépendances Python installées."
    print_step "Téléchargement de Chromium pour Playwright"
    python -m playwright install chromium --with-deps 2>&1 | tail -3 || true
    print_ok "Chromium prêt."
}

# ─── Génération d'un config.yaml temporaire avec paths overrides ─────────
# Utilisé seulement si --instance NAME : on duplique config.yaml et on
# remplace les paths qui pointent vers data/ par data-NAME/. Le code
# Python ne change pas — il lit le YAML pointé par FANSLY_CONFIG_FILE.
INSTANCE_CONFIG=""
prepare_instance_config() {
    if [[ -z "$INSTANCE" ]]; then return 0; fi
    INSTANCE_CONFIG="config.${INSTANCE}.yaml"
    # Substitution des paths data/ -> data-NAME/ (Python yaml.safe_load
    # accepte les deux formats indifferemment).
    sed -E \
        -e "s|(\"|')?data/Medias(\"|')?|\1${DATA_DIR}/Medias\2|g" \
        -e "s|(\"|')?data/Captions(\"|')?|\1${DATA_DIR}/Captions\2|g" \
        -e "s|(\"|')?data/state.db(\"|')?|\1${DATA_DIR}/state.db\2|g" \
        -e "s|(\"|')?data/logs(\"|')?|\1${DATA_DIR}/logs\2|g" \
        -e "s|(\"|')?data/artifacts(\"|')?|\1${DATA_DIR}/artifacts\2|g" \
        -e "s|(\"|')?data/browser_profile(\"|')?|\1${DATA_DIR}/browser_profile\2|g" \
        config.yaml > "$INSTANCE_CONFIG"
    print_ok "Config instance generee : ${BOLD}${INSTANCE_CONFIG}${RESET}"
}

cleanup_instance_config() {
    if [[ -n "$INSTANCE_CONFIG" ]] && [[ -f "$INSTANCE_CONFIG" ]]; then
        rm -f "$INSTANCE_CONFIG"
    fi
}

# ─── Lancement du setup-auth ─────────────────────────────────────────────
run_setup_auth() {
    print_step "Lancement de Chromium pour le login Fansly"
    if [[ -n "$INSTANCE" ]]; then
        echo "  ${DIM}Instance: $INSTANCE | env: $ENV_FILE | data: $DATA_DIR/${RESET}"
    fi
    echo
    echo "  ${BOLD}${YELLOW}Action requise :${RESET}"
    echo "  ${YELLOW}→ Une fenêtre Chromium va s'ouvrir.${RESET}"
    echo "  ${YELLOW}→ Connecte-toi sur Fansly comme d'habitude (email + mdp + éventuelle 2FA).${RESET}"
    echo "  ${YELLOW}→ Une fois sur ta page d'accueil Fansly, reviens dans ce terminal.${RESET}"
    echo "  ${YELLOW}→ Appuie sur Entrée ici pour terminer le setup.${RESET}"
    echo
    sleep 2
    # Charge le .env (instance-specifique ou legacy) comme variables d'env
    # exportees, pour que Pydantic les lise en priorite.
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
    # PYTHONPATH=src pour que le code source soit trouve sans `pip install -e .`
    if [[ -n "$INSTANCE_CONFIG" ]]; then
        FANSLY_CONFIG_FILE="$INSTANCE_CONFIG" PYTHONPATH=src python -m fansly_bot setup-auth
    else
        PYTHONPATH=src python -m fansly_bot setup-auth
    fi
}

cleanup_all() {
    cleanup_venv
    cleanup_instance_config
}

# ─── Main ────────────────────────────────────────────────────────────────
main() {
    parse_args "$@"
    print_header
    if [[ -n "$INSTANCE" ]]; then
        print_step "Instance : ${BOLD}${INSTANCE}${RESET}"
        echo "  ${DIM}env: $ENV_FILE | data: $DATA_DIR/browser_profile/${RESET}"
    fi
    check_pwd
    check_env_file

    print_step "Vérification de Python 3.11+"
    if ! python_cmd=$(find_python); then
        print_error "Aucun Python 3.11+ trouvé sur cette machine."
        print_error "Installation :"
        print_error "  macOS  : brew install python@3.12"
        print_error "  Linux  : sudo apt install python3"
        print_error "  Windows : https://python.org/downloads/"
        exit 1
    fi
    print_ok "Python détecté : ${BOLD}$python_cmd${RESET}"

    # Setup propre : on nettoie même en cas d'interruption (Ctrl+C)
    trap cleanup_all EXIT

    prepare_instance_config
    create_venv "$python_cmd"
    install_deps
    run_setup_auth

    echo
    print_ok "${BOLD}Authentification terminée.${RESET}"
    print_ok "Le profil de session est sauvegardé dans ${BOLD}${DATA_DIR}/browser_profile/${RESET}"

    echo
    print_step "Prochaine étape"
    if [[ -n "$INSTANCE" ]]; then
        echo "  Demarre l'instance ${BOLD}${INSTANCE}${RESET} :"
        echo "    ${BOLD}./scripts/new-instance.sh ${INSTANCE} --start-only${RESET}"
        echo "  Ou via docker-compose direct :"
        echo "    ${BOLD}INSTANCE_NAME=${INSTANCE} ENV_FILE=${ENV_FILE} DATA_DIR=./${DATA_DIR} HOST_PORT=<port_libre> docker compose --project-name fansly-${INSTANCE} up -d --build${RESET}"
    else
        echo "  Lance le bot avec Docker (recommandé) :"
        echo "    ${BOLD}docker compose up -d --build${RESET}"
        echo "  Puis ouvre ${BOLD}http://localhost:8501${RESET} dans ton navigateur."
    fi
    echo
}

main "$@"
