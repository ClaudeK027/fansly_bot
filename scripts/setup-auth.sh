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

check_env_file() {
    if [[ ! -f ".env" ]]; then
        print_error "Le fichier .env est manquant."
        print_error "Lance d'abord : ${BOLD}./scripts/init-env.sh${RESET}"
        exit 1
    fi
    # Validation rapide : les 3 variables doivent être présentes
    for var in FANSLY_USERNAME FANSLY_PASSWORD FANSLY_PROFILE_SLUG; do
        if ! grep -qE "^${var}=" .env; then
            print_warn ".env n'a pas la variable ${BOLD}${var}${RESET}."
            print_warn "Relance ${BOLD}./scripts/init-env.sh${RESET} pour le régénérer."
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

create_venv() {
    local python_cmd="$1"
    print_step "Création de l'environnement Python jetable"
    echo "  ${DIM}Utilisation de : $($python_cmd --version 2>&1)${RESET}"
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

# ─── Lancement du setup-auth ─────────────────────────────────────────────
run_setup_auth() {
    print_step "Lancement de Chromium pour le login Fansly"
    echo
    echo "  ${BOLD}${YELLOW}Action requise :${RESET}"
    echo "  ${YELLOW}→ Une fenêtre Chromium va s'ouvrir.${RESET}"
    echo "  ${YELLOW}→ Connecte-toi sur Fansly comme d'habitude (email + mdp + éventuelle 2FA).${RESET}"
    echo "  ${YELLOW}→ Une fois sur ta page d'accueil Fansly, reviens dans ce terminal.${RESET}"
    echo "  ${YELLOW}→ Appuie sur Entrée ici pour terminer le setup.${RESET}"
    echo
    sleep 2
    # PYTHONPATH=src pour que le code source soit trouvé sans `pip install -e .`
    PYTHONPATH=src python -m fansly_bot setup-auth
}

# ─── Main ────────────────────────────────────────────────────────────────
main() {
    print_header
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
    trap cleanup_venv EXIT

    create_venv "$python_cmd"
    install_deps
    run_setup_auth

    echo
    print_ok "${BOLD}Authentification terminée.${RESET}"
    print_ok "Le profil de session est sauvegardé dans ${BOLD}data/browser_profile/${RESET}"

    echo
    print_step "Prochaine étape"
    echo "  Lance le bot avec Docker (recommandé) :"
    echo "    ${BOLD}docker compose up -d --build${RESET}"
    echo "  Puis ouvre ${BOLD}http://localhost:8501${RESET} dans ton navigateur."
    echo
}

main "$@"
