<#
.SYNOPSIS
    Authentifie le bot Fansly en lançant Chromium pour login manuel (Windows).

.DESCRIPTION
    Équivalent PowerShell de scripts/setup-auth.sh. Crée un environnement
    Python jetable, installe Playwright + Chromium, lance le setup-auth
    puis nettoie. Le dossier data/browser_profile/ contient à la fin la
    session authentifiée, utilisable ensuite par Docker.

.EXAMPLE
    .\scripts\setup-auth.ps1
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Header {
    Write-Host ""
    Write-Host "╔══════════════════════════════════════════════════════════╗" -ForegroundColor Blue
    Write-Host "║  Fansly Bot — Authentification initiale (setup-auth)    ║" -ForegroundColor Blue
    Write-Host "╚══════════════════════════════════════════════════════════╝" -ForegroundColor Blue
    Write-Host ""
}

function Write-Step  { param([string]$msg) Write-Host "→ " -ForegroundColor Blue -NoNewline; Write-Host $msg }
function Write-Ok    { param([string]$msg) Write-Host "✓ " -ForegroundColor Green -NoNewline; Write-Host $msg }
function Write-Warn  { param([string]$msg) Write-Host "⚠  " -ForegroundColor Yellow -NoNewline; Write-Host $msg }
function Write-Err   { param([string]$msg) Write-Host "✗ " -ForegroundColor Red -NoNewline; Write-Host $msg }

# ─── Vérifications ───────────────────────────────────────────────────────
function Test-ProjectRoot {
    if (-not (Test-Path "./pyproject.toml") -or -not (Test-Path "./src/fansly_bot")) {
        Write-Err "Ce script doit être lancé depuis la racine du projet."
        exit 1
    }
}

function Test-EnvFile {
    if (-not (Test-Path ".env")) {
        Write-Err "Le fichier .env est manquant."
        Write-Err "Lance d'abord : .\scripts\init-env.ps1"
        exit 1
    }
    $required = 'FANSLY_USERNAME', 'FANSLY_PASSWORD', 'FANSLY_PROFILE_SLUG'
    $content = Get-Content .env -Raw
    foreach ($var in $required) {
        if ($content -notmatch "(?m)^${var}=") {
            Write-Warn ".env n'a pas la variable ${var}."
        }
    }
}

function Find-Python {
    $candidates = 'python', 'python3', 'py'
    foreach ($cmd in $candidates) {
        try {
            $version = & $cmd -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and $version) {
                $major, $minor = $version -split '\.'
                if ([int]$major -ge 3 -and [int]$minor -ge 11) {
                    return $cmd
                }
            }
        } catch {
            continue
        }
    }
    return $null
}

# ─── Venv jetable ────────────────────────────────────────────────────────
$VenvDir = ".venv-setup-auth"

function Remove-Venv {
    if (Test-Path $VenvDir) {
        Remove-Item -Recurse -Force $VenvDir -ErrorAction SilentlyContinue
        Write-Ok "Environnement jetable nettoyé : $VenvDir"
    }
}

function New-Venv {
    param([string]$PythonCmd)
    Write-Step "Création de l'environnement Python jetable"
    $version = & $PythonCmd --version 2>&1
    Write-Host "  Utilisation de : $version" -ForegroundColor DarkGray
    & $PythonCmd -m venv $VenvDir
    Write-Ok "Venv créé : $VenvDir"
}

function Install-Dependencies {
    Write-Step "Installation des dépendances (Playwright + Chromium)"
    Write-Host "  Cela peut prendre 1-2 minutes au premier lancement..." -ForegroundColor DarkGray
    $venvPython = Join-Path $VenvDir "Scripts\python.exe"
    & $venvPython -m pip install --quiet --upgrade pip
    & $venvPython -m pip install --quiet `
        "playwright>=1.47.0" `
        "playwright-stealth>=2.0.0" `
        "structlog>=24.1.0" `
        "pendulum>=3.0.0" `
        "pydantic>=2.7.0" `
        "pydantic-settings>=2.4.0" `
        "pyyaml>=6.0.1" `
        "ruamel.yaml>=0.18.0" `
        "tenacity>=8.2.3"
    Write-Ok "Dépendances Python installées."
    Write-Step "Téléchargement de Chromium pour Playwright"
    & $venvPython -m playwright install chromium --with-deps 2>&1 | Select-Object -Last 3
    Write-Ok "Chromium prêt."
}

# ─── Lancement setup-auth ────────────────────────────────────────────────
function Invoke-SetupAuth {
    Write-Step "Lancement de Chromium pour le login Fansly"
    Write-Host ""
    Write-Host "  Action requise :" -ForegroundColor Yellow
    Write-Host "  → Une fenêtre Chromium va s'ouvrir." -ForegroundColor Yellow
    Write-Host "  → Connecte-toi sur Fansly comme d'habitude." -ForegroundColor Yellow
    Write-Host "  → Une fois sur ta page d'accueil, reviens dans ce terminal." -ForegroundColor Yellow
    Write-Host ""
    Start-Sleep -Seconds 2
    $venvPython = Join-Path $VenvDir "Scripts\python.exe"
    $env:PYTHONPATH = (Resolve-Path "src").Path
    & $venvPython -m fansly_bot setup-auth
}

# ─── Main ────────────────────────────────────────────────────────────────
Write-Header
Test-ProjectRoot
Test-EnvFile

Write-Step "Vérification de Python 3.11+"
$pythonCmd = Find-Python
if (-not $pythonCmd) {
    Write-Err "Aucun Python 3.11+ trouvé sur cette machine."
    Write-Err "Installation : https://python.org/downloads/"
    exit 1
}
Write-Ok "Python détecté : $pythonCmd"

# Nettoyage du venv même en cas d'erreur
try {
    New-Venv -PythonCmd $pythonCmd
    Install-Dependencies
    Invoke-SetupAuth
}
finally {
    Remove-Venv
}

Write-Host ""
Write-Ok "Authentification terminée."
Write-Ok "Le profil de session est dans data/browser_profile/"

Write-Host ""
Write-Step "Prochaine étape"
Write-Host "  Lance le bot avec Docker (recommandé) :"
Write-Host "    docker compose up -d --build" -ForegroundColor White
Write-Host "  Puis ouvre http://localhost:8501 dans ton navigateur." -ForegroundColor White
Write-Host ""
