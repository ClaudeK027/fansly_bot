<#
.SYNOPSIS
    Crée interactivement le fichier .env nécessaire au bot Fansly (Windows).

.DESCRIPTION
    Équivalent PowerShell de scripts/init-env.sh. Compatible Windows 10/11
    et Windows Server. Demande à l'utilisateur ses identifiants Fansly et
    écrit le fichier .env à la racine du projet.

.EXAMPLE
    .\scripts\init-env.ps1
    À lancer depuis la racine du projet.
#>

# Strict mode + arrêt à la première erreur
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ─── Helpers d'affichage ─────────────────────────────────────────────────
function Write-Header {
    Write-Host ""
    Write-Host "╔══════════════════════════════════════════════════════════╗" -ForegroundColor Blue
    Write-Host "║  Fansly Bot — Configuration initiale du fichier .env    ║" -ForegroundColor Blue
    Write-Host "╚══════════════════════════════════════════════════════════╝" -ForegroundColor Blue
    Write-Host ""
    Write-Host "Ce script crée le fichier " -NoNewline
    Write-Host ".env" -ForegroundColor White -NoNewline
    Write-Host " avec tes identifiants Fansly."
    Write-Host "Le .env n'est jamais versionné ni inclus dans l'image Docker." -ForegroundColor DarkGray
    Write-Host ""
}

function Write-Step  { param([string]$msg) Write-Host "→ " -ForegroundColor Blue -NoNewline; Write-Host $msg }
function Write-Ok    { param([string]$msg) Write-Host "✓ " -ForegroundColor Green -NoNewline; Write-Host $msg }
function Write-Warn  { param([string]$msg) Write-Host "⚠  " -ForegroundColor Yellow -NoNewline; Write-Host $msg }
function Write-Err   { param([string]$msg) Write-Host "✗ " -ForegroundColor Red -NoNewline; Write-Host $msg }

# ─── Vérification du dossier ─────────────────────────────────────────────
function Test-ProjectRoot {
    if (-not (Test-Path "./pyproject.toml") -or -not (Test-Path "./src/fansly_bot")) {
        Write-Err "Ce script doit être lancé depuis la racine du projet."
        Write-Err "Lance plutôt : .\scripts\init-env.ps1"
        exit 1
    }
}

# ─── Sauvegarde d'un .env existant ───────────────────────────────────────
function Test-ExistingEnv {
    if (Test-Path ".env") {
        Write-Warn "Un fichier .env existe déjà."
        Write-Host "  Que faire ?"
        Write-Host "    [1] Le sauvegarder en .env.backup et en créer un nouveau (défaut)"
        Write-Host "    [2] Annuler"
        Write-Host ""
        $choice = Read-Host "  Ton choix [1]"
        if ([string]::IsNullOrWhiteSpace($choice)) { $choice = "1" }
        switch ($choice) {
            "1" {
                Move-Item -Path .env -Destination .env.backup -Force
                Write-Ok ".env existant sauvegardé en .env.backup"
            }
            "2" {
                Write-Warn "Annulé. Aucune modification."
                exit 0
            }
            default {
                Write-Err "Choix invalide."
                exit 1
            }
        }
        Write-Host ""
    }
}

# ─── Saisie des credentials ──────────────────────────────────────────────
function Read-Username {
    while ($true) {
        $script:FanslyUsername = Read-Host "  Email de connexion Fansly"
        if ([string]::IsNullOrWhiteSpace($script:FanslyUsername)) {
            Write-Err "L'email ne peut pas être vide."
            continue
        }
        if ($script:FanslyUsername -notmatch '@') {
            Write-Warn "L'email ne contient pas '@'. Es-tu sûr ? [O/n]"
            $confirm = Read-Host
            if ([string]::IsNullOrWhiteSpace($confirm)) { $confirm = "O" }
            if ($confirm -match '^[OoYy]') { break }
            continue
        }
        break
    }
}

function Read-PasswordSecure {
    while ($true) {
        $secure1 = Read-Host "  Mot de passe Fansly" -AsSecureString
        $pw1 = [System.Net.NetworkCredential]::new("", $secure1).Password
        if ([string]::IsNullOrWhiteSpace($pw1)) {
            Write-Err "Le mot de passe ne peut pas être vide."
            continue
        }
        if ($pw1.Length -lt 6) {
            Write-Warn "Le mot de passe semble très court (< 6 caractères)."
        }
        $secure2 = Read-Host "  Confirmer le mot de passe" -AsSecureString
        $pw2 = [System.Net.NetworkCredential]::new("", $secure2).Password
        if ($pw1 -ne $pw2) {
            Write-Err "Les deux mots de passe ne correspondent pas. Recommence."
            continue
        }
        $script:FanslyPassword = $pw1
        break
    }
}

function Read-ProfileSlug {
    Write-Host ""
    Write-Host "  Ton slug est la partie publique de l'URL de ton profil." -ForegroundColor DarkGray
    Write-Host "  Exemple : https://fansly.com/MonPseudo/posts → slug = MonPseudo" -ForegroundColor DarkGray
    while ($true) {
        $slug = Read-Host "  Slug de ton profil Fansly"
        $slug = $slug.Trim('/').Trim()
        if ([string]::IsNullOrWhiteSpace($slug)) {
            Write-Err "Le slug ne peut pas être vide."
            continue
        }
        if ($slug -match '\s') {
            Write-Err "Le slug ne peut pas contenir d'espaces."
            continue
        }
        if ($slug -match '/') {
            Write-Err "Le slug ne peut pas contenir de '/'."
            continue
        }
        if ($slug -notmatch '^[A-Za-z0-9._-]+$') {
            Write-Warn "Le slug contient des caractères inhabituels."
            Write-Warn "Les slugs Fansly utilisent généralement : lettres, chiffres, '_', '-', '.'"
            $confirm = Read-Host "  Continuer quand même ? [o/N]"
            if ($confirm -match '^[OoYy]') { $script:FanslyProfileSlug = $slug; break }
            continue
        }
        $script:FanslyProfileSlug = $slug
        break
    }
}

# ─── Récapitulatif et écriture ───────────────────────────────────────────
function Confirm-AndWrite {
    $masked = '*' * $script:FanslyPassword.Length
    Write-Host ""
    Write-Step "Récapitulatif"
    Write-Host "    FANSLY_USERNAME      = $($script:FanslyUsername)"
    Write-Host "    FANSLY_PASSWORD      = $masked"
    Write-Host "    FANSLY_PROFILE_SLUG  = $($script:FanslyProfileSlug)"
    Write-Host ""
    $confirm = Read-Host "  Confirmer et écrire le .env ? [O/n]"
    if ([string]::IsNullOrWhiteSpace($confirm)) { $confirm = "O" }
    if ($confirm -notmatch '^[OoYy]') {
        Write-Warn "Annulé. Le .env n'a pas été créé."
        if ((Test-Path ".env.backup") -and -not (Test-Path ".env")) {
            Move-Item -Path .env.backup -Destination .env
            Write-Ok ".env précédent restauré."
        }
        exit 0
    }

    $content = @"
# .env — généré par scripts/init-env.ps1
# NE JAMAIS commit ce fichier — il contient des secrets.

FANSLY_USERNAME=$($script:FanslyUsername)
FANSLY_PASSWORD=$($script:FanslyPassword)
FANSLY_PROFILE_SLUG=$($script:FanslyProfileSlug)
"@
    # Encodage UTF-8 sans BOM pour compatibilité Docker / Linux
    [System.IO.File]::WriteAllText("$PWD\.env", $content, [System.Text.UTF8Encoding]::new($false))

    # Sur Windows, les permissions Linux n'existent pas. On indique tout de
    # même un message rassurant. L'utilisateur peut éventuellement restreindre
    # via les ACL Windows mais c'est rarement nécessaire en usage perso.
    Write-Ok ".env créé : $PWD\.env"
    Write-Host "  Note : sur Windows, les permissions de fichier ne sont pas restreintes par chmod." -ForegroundColor DarkGray
    Write-Host "  Veille à ne pas partager ce fichier ni le pousser sur Git." -ForegroundColor DarkGray
}

# ─── Proposition de la suite ─────────────────────────────────────────────
function Show-NextStep {
    Write-Host ""
    Write-Step "Prochaine étape"
    Write-Host "  Tu dois maintenant authentifier le bot auprès de Fansly."
    Write-Host "  Cela ouvrira Chromium pour te laisser te connecter à la main."
    Write-Host ""
    if (Test-Path ".\scripts\setup-auth.ps1") {
        $confirm = Read-Host "  Lancer .\scripts\setup-auth.ps1 maintenant ? [O/n]"
        if ([string]::IsNullOrWhiteSpace($confirm)) { $confirm = "O" }
        if ($confirm -match '^[OoYy]') {
            Write-Host ""
            & ".\scripts\setup-auth.ps1"
        } else {
            Write-Host "  Quand tu seras prêt, lance : .\scripts\setup-auth.ps1"
        }
    } else {
        Write-Host "  Lance : .\scripts\setup-auth.ps1"
    }
}

# ─── Main ────────────────────────────────────────────────────────────────
Write-Header
Test-ProjectRoot
Test-ExistingEnv
Write-Step "Identifiants Fansly"
Read-Username
Read-PasswordSecure
Write-Step "Profil Fansly"
Read-ProfileSlug
Confirm-AndWrite
Show-NextStep
Write-Host ""
