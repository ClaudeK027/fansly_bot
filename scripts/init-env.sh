#!/usr/bin/env bash
# scripts/init-env.sh
#
# Crée interactivement le fichier .env nécessaire au bot Fansly.
# Compatible macOS, Linux, WSL. Aucun prérequis (ni Python, ni Docker).
#
# Usage :
#   ./scripts/init-env.sh
#
# À lancer depuis la racine du projet.

set -euo pipefail

# ─── Couleurs (désactivées si pas de terminal interactif) ────────────────
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

# ─── Helpers d'affichage ─────────────────────────────────────────────────
print_header() {
    echo
    echo "${BOLD}${BLUE}╔══════════════════════════════════════════════════════════╗${RESET}"
    echo "${BOLD}${BLUE}║  Fansly Bot — Configuration initiale du fichier .env    ║${RESET}"
    echo "${BOLD}${BLUE}╚══════════════════════════════════════════════════════════╝${RESET}"
    echo
    echo "Ce script crée le fichier ${BOLD}.env${RESET} avec tes identifiants Fansly."
    echo "${DIM}Le .env n'est jamais versionné ni inclus dans l'image Docker.${RESET}"
    echo
}

print_step()    { echo "${BOLD}${BLUE}→${RESET} ${BOLD}$1${RESET}"; }
print_ok()      { echo "${GREEN}✓${RESET} $1"; }
print_warn()    { echo "${YELLOW}⚠${RESET}  $1"; }
print_error()   { echo "${RED}✗${RESET} $1" >&2; }

# ─── Vérifications préalables ────────────────────────────────────────────
check_pwd() {
    if [[ ! -f "./pyproject.toml" ]] || [[ ! -d "./src/fansly_bot" ]]; then
        print_error "Ce script doit être lancé depuis la racine du projet."
        print_error "Lance plutôt : ${BOLD}./scripts/init-env.sh${RESET} depuis le dossier du repo."
        exit 1
    fi
}

# ─── Sauvegarde d'un éventuel .env existant ──────────────────────────────
handle_existing_env() {
    if [[ -f ".env" ]]; then
        print_warn "Un fichier ${BOLD}.env${RESET} existe déjà."
        echo "  Que faire ?"
        echo "    ${BOLD}[1]${RESET} Le sauvegarder en .env.backup et en créer un nouveau (défaut)"
        echo "    ${BOLD}[2]${RESET} Annuler"
        echo
        read -r -p "  Ton choix [1] : " choice
        choice="${choice:-1}"
        case "$choice" in
            1)
                mv .env .env.backup
                print_ok ".env existant sauvegardé en ${BOLD}.env.backup${RESET}"
                ;;
            2)
                print_warn "Annulé. Aucune modification."
                exit 0
                ;;
            *)
                print_error "Choix invalide."
                exit 1
                ;;
        esac
        echo
    fi
}

# ─── Saisie des credentials ──────────────────────────────────────────────
ask_username() {
    while true; do
        read -r -p "  Email de connexion Fansly : " FANSLY_USERNAME
        if [[ -z "$FANSLY_USERNAME" ]]; then
            print_error "L'email ne peut pas être vide."
            continue
        fi
        if [[ ! "$FANSLY_USERNAME" =~ @ ]]; then
            print_warn "L'email ne contient pas '@'. Es-tu sûr ? [O/n]"
            read -r confirm
            case "${confirm:-O}" in
                [OoYy]*) break ;;
                *) continue ;;
            esac
        else
            break
        fi
    done
}

ask_password() {
    while true; do
        read -r -s -p "  Mot de passe Fansly : " FANSLY_PASSWORD
        echo
        if [[ -z "$FANSLY_PASSWORD" ]]; then
            print_error "Le mot de passe ne peut pas être vide."
            continue
        fi
        if [[ ${#FANSLY_PASSWORD} -lt 6 ]]; then
            print_warn "Le mot de passe semble très court (< 6 caractères)."
        fi
        read -r -s -p "  Confirmer le mot de passe : " PASSWORD_CONFIRM
        echo
        if [[ "$FANSLY_PASSWORD" != "$PASSWORD_CONFIRM" ]]; then
            print_error "Les deux mots de passe ne correspondent pas. Recommence."
            continue
        fi
        break
    done
}

ask_profile_slug() {
    echo
    echo "  ${DIM}Ton slug est la partie publique de l'URL de ton profil.${RESET}"
    echo "  ${DIM}Exemple : https://fansly.com/${BOLD}MonPseudo${RESET}${DIM}/posts → slug = MonPseudo${RESET}"
    while true; do
        read -r -p "  Slug de ton profil Fansly : " FANSLY_PROFILE_SLUG
        # Normalise : retire les / et espaces en début/fin
        FANSLY_PROFILE_SLUG="${FANSLY_PROFILE_SLUG#/}"
        FANSLY_PROFILE_SLUG="${FANSLY_PROFILE_SLUG%/}"
        if [[ -z "$FANSLY_PROFILE_SLUG" ]]; then
            print_error "Le slug ne peut pas être vide."
            continue
        fi
        if [[ "$FANSLY_PROFILE_SLUG" =~ [[:space:]] ]]; then
            print_error "Le slug ne peut pas contenir d'espaces."
            continue
        fi
        if [[ "$FANSLY_PROFILE_SLUG" =~ / ]]; then
            print_error "Le slug ne peut pas contenir de '/'."
            continue
        fi
        # Regex permissive — accepte lettres, chiffres, _, -, .
        if [[ ! "$FANSLY_PROFILE_SLUG" =~ ^[A-Za-z0-9._-]+$ ]]; then
            print_warn "Le slug contient des caractères inhabituels."
            print_warn "Les slugs Fansly utilisent généralement : lettres, chiffres, '_', '-', '.'"
            read -r -p "  Continuer quand même ? [o/N] : " confirm
            case "${confirm:-N}" in
                [OoYy]*) break ;;
                *) continue ;;
            esac
        else
            break
        fi
    done
}

# ─── Récapitulatif et écriture ───────────────────────────────────────────
confirm_and_write() {
    local masked
    masked=$(printf '%*s' "${#FANSLY_PASSWORD}" '' | tr ' ' '*')
    echo
    print_step "Récapitulatif"
    echo "    FANSLY_USERNAME      = $FANSLY_USERNAME"
    echo "    FANSLY_PASSWORD      = $masked"
    echo "    FANSLY_PROFILE_SLUG  = $FANSLY_PROFILE_SLUG"
    echo
    read -r -p "  Confirmer et écrire le .env ? [O/n] : " confirm
    case "${confirm:-O}" in
        [OoYy]*) ;;
        *)
            print_warn "Annulé. Le .env n'a pas été créé."
            # Restaure le .env.backup si applicable
            if [[ -f ".env.backup" ]] && [[ ! -f ".env" ]]; then
                mv .env.backup .env
                print_ok ".env précédent restauré."
            fi
            exit 0
            ;;
    esac

    # Écriture du .env
    cat > .env <<EOF
# .env — généré par scripts/init-env.sh
# NE JAMAIS commit ce fichier — il contient des secrets.

FANSLY_USERNAME=$FANSLY_USERNAME
FANSLY_PASSWORD=$FANSLY_PASSWORD
FANSLY_PROFILE_SLUG=$FANSLY_PROFILE_SLUG
EOF
    chmod 600 .env
    print_ok ".env créé : ${BOLD}$(pwd)/.env${RESET}"
    print_ok "Permissions : ${BOLD}chmod 600${RESET} (lecture/écriture propriétaire uniquement)"
}

# ─── Proposition de la suite ─────────────────────────────────────────────
propose_next_step() {
    echo
    print_step "Prochaine étape"
    echo "  Tu dois maintenant authentifier le bot auprès de Fansly."
    echo "  Cela ouvrira Chromium pour te laisser te connecter à la main."
    echo
    if [[ -x "./scripts/setup-auth.sh" ]]; then
        read -r -p "  Lancer ${BOLD}./scripts/setup-auth.sh${RESET} maintenant ? [O/n] : " run_setup
        case "${run_setup:-O}" in
            [OoYy]*)
                echo
                exec ./scripts/setup-auth.sh
                ;;
            *)
                echo "  Quand tu seras prêt, lance : ${BOLD}./scripts/setup-auth.sh${RESET}"
                ;;
        esac
    else
        echo "  Lance : ${BOLD}./scripts/setup-auth.sh${RESET}"
    fi
}

# ─── Main ────────────────────────────────────────────────────────────────
main() {
    print_header
    check_pwd
    handle_existing_env
    print_step "Identifiants Fansly"
    ask_username
    ask_password
    print_step "Profil Fansly"
    ask_profile_slug
    confirm_and_write
    propose_next_step
    echo
}

main "$@"
