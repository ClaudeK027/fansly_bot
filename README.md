# Fansly Bot

> Automatisation discrète et résiliente de la publication et de la modération de posts sur Fansly. Worker autonome + dashboard de pilotage, conteneurisé avec Docker pour un déploiement en une commande.

---

## En bref

Le bot enchaîne **deux missions** que tu pilotes depuis un dashboard web :

- **Publier** des médias en rotation sur ton profil, à intervalles variables et humanisés, avec **rotation de légendes** et **preview attachée** pour maximiser la visibilité sur la For You Page.
- **Purger** les anciennes publications selon des **critères croisés** (fenêtre temporelle, mots-clés dans la légende, plafond strict) — sans jamais toucher aux posts manuels.

Tout l'état est local (SQLite + bind mount Docker). Aucune donnée ne sort de ta machine ou de ton VPS.

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                  Container Docker                        │
│                                                          │
│   ┌────────────────┐         ┌────────────────────┐      │
│   │   Streamlit    │ ◄────►  │     Worker         │      │
│   │   dashboard    │  SQLite │   (asyncio +       │      │
│   │  (port 8501)   │         │    Playwright)     │      │
│   └────────────────┘         └────────┬───────────┘      │
│            ▲                          │                  │
│            │                          ▼                  │
│            │                  ┌──────────────────┐       │
│            │                  │ Chromium headless│       │
│            │                  │   + stealth      │       │
│            └────── bind mount ────────┐          │       │
│                                       ▼          │       │
└────────────────────── data/ ──────────┴──────────┴───────┘
```

- **Worker async** : consomme une file de jobs SQLite (`publish` / `purge`), pilote Chromium via Playwright, gère retries, annulation gracieuse, et idempotence (marqueur `publish_in_flight` avant chaque action critique).
- **Dashboard Streamlit** : enfile des jobs, surveille l'état du worker, affiche les logs, gère les lots de médias et de légendes.
- **Tout l'état** vit dans `data/` (BDD, médias, légendes, profil navigateur, logs) — bind-mounté côté host, jamais embarqué dans l'image.

---

## Stack technique

| Couche | Choix |
|---|---|
| Langage | Python 3.11+ |
| Browser automation | Playwright + playwright-stealth |
| UI | Streamlit |
| Persistance | SQLite (mode WAL) |
| Logs | structlog (JSON structuré) |
| Validation | Pydantic v2 + pydantic-settings |
| Conteneurisation | Docker (image officielle Playwright) |

---

## Prérequis

| OS | Outils à installer |
|---|---|
| **macOS** | `brew install git python@3.12` + Docker Desktop (`brew install --cask docker`) |
| **Linux** (Debian/Ubuntu) | `sudo apt install -y git python3 python3-venv` + `curl -fsSL https://get.docker.com \| sudo sh` |
| **Windows** | [Git for Windows](https://git-scm.com), [Python 3.12](https://python.org), [Docker Desktop](https://docker.com/products/docker-desktop) |

Python est nécessaire **uniquement pour l'étape d'authentification initiale** (une fois). Toute la suite ne demande que Docker.

---

## Installation pas à pas

### 1. Cloner le projet

```bash
git clone https://github.com/ClaudeK027/fansly_bot.git fansly-bot
cd fansly-bot
```

> **Repo privé** : ton compte GitHub doit être ajouté comme collaborateur. Avec `gh` CLI déjà authentifié, tu peux aussi faire `gh repo clone ClaudeK027/fansly_bot fansly-bot`.

### 2. Créer le fichier `.env` (interactif)

Un script t'accompagne pour saisir tes identifiants et écrire le `.env` proprement :

```bash
# macOS / Linux
./scripts/init-env.sh

# Windows (PowerShell)
.\scripts\init-env.ps1
```

Le script te demande :

- Ton **email Fansly** (le même que pour te connecter sur le site)
- Ton **mot de passe Fansly** (saisie masquée + confirmation)
- Ton **profil Fansly** (le slug visible dans ton URL : `https://fansly.com/<slug>/posts`)

À la fin, le `.env` est créé avec `chmod 600` (lecture/écriture propriétaire uniquement sur macOS/Linux). Il n'est jamais versionné — le `.gitignore` l'exclut.

### 3. Authentification initiale (login Fansly)

Pour pouvoir piloter ton compte, le bot a besoin d'un navigateur déjà connecté à Fansly. Un second script s'en occupe :

```bash
# macOS / Linux
./scripts/setup-auth.sh

# Windows
.\scripts\setup-auth.ps1
```

Sous le capot, le script :

1. Crée un environnement Python jetable
2. Installe Playwright + télécharge Chromium
3. Lance Chromium en mode visible
4. **Tu te connectes à Fansly à la main** (gestion 2FA inclue si activée)
5. Le bot détecte que tu es connecté et sauve la session dans `data/browser_profile/`
6. L'environnement jetable est supprimé

Le `data/browser_profile/` est ensuite consommé par Docker. À l'issue, ton Python local peut même être désinstallé — il n'est plus nécessaire.

> **Pourquoi un login manuel ?** Les solutions automatisées (Playwright remplit le formulaire) butent fréquemment sur Cloudflare et hCaptcha. Le login manuel reste la méthode la plus fiable à 100 %.

### 4. Lancer le bot — mode Docker (recommandé)

```bash
docker compose up -d --build
```

Au premier lancement, le build télécharge l'image Playwright officielle (~700 Mo) et installe les dépendances. Compte 3 à 5 minutes selon ta connexion. Les lancements suivants utilisent le cache et démarrent en quelques secondes.

Une fois prêt, ouvre le dashboard dans ton navigateur :

```bash
# macOS
open http://localhost:8501

# Linux
xdg-open http://localhost:8501

# Windows
start http://localhost:8501
```

Le worker démarre automatiquement avec le container, le dashboard te permet d'enfiler tes premiers jobs.

---

## Multi-instance (un bot par compte Fansly)

Le bot supporte plusieurs instances sur la même machine, chacune avec son compte Fansly, son dashboard, et ses données isolées. Idéal si tu gères 2-5 comptes.

### Créer une nouvelle instance — commande unique

```bash
./scripts/new-instance.sh NAME
```

Le script orchestre les 3 étapes :
1. Saisie des identifiants Fansly de ce compte (`.env.NAME`)
2. Login Fansly manuel via Chromium (sauvegardé dans `data-NAME/browser_profile/`)
3. Démarrage du container sur un port libre (8501, 8502, …)

À la fin, le dashboard de cette instance est accessible sur `http://localhost:<port>`.

### Gérer les instances

```bash
# Liste les instances et leur statut (running/stopped, port, dashboard)
./scripts/list-instances.sh

# Arrête une instance (préserve data + .env, peut être redémarrée)
./scripts/stop-instance.sh marie

# Redémarre une instance arrêtée
./scripts/start-instance.sh marie

# Supprime totalement une instance (--with-data pour aussi wiper le data)
./scripts/destroy-instance.sh marie               # garde data-marie/ + .env.marie
./scripts/destroy-instance.sh marie --with-data   # wipe tout (irréversible)
```

### Sous le capot

Chaque instance utilise :
- **Container Docker dédié** : `fansly-bot-NAME` (project name `fansly-NAME`)
- **Fichier env dédié** : `.env.NAME` (credentials + slug)
- **Dossier data dédié** : `data-NAME/` (BDD, browser profile, médias, logs)
- **Port host unique** : assigné dynamiquement (8501, 8502, …)

Le `docker-compose.yml` est paramétrable via 4 variables d'env :
```bash
INSTANCE_NAME=marie \
ENV_FILE=.env.marie \
DATA_DIR=./data-marie \
HOST_PORT=8502 \
  docker compose --project-name fansly-marie up -d --build
```

Le single-instance (sans `--instance`) reste pleinement fonctionnel : c'est le comportement par défaut documenté plus haut.

### Limites

- **Anti-détection Fansly** : au-delà de 3-5 comptes depuis la même IP, le risque de pattern bot détectable augmente. Pour scaler plus, prévoir des proxies résidentiels par instance ou plusieurs VPS.
- **RAM** : ~650 Mo par instance (Python + Streamlit + Chromium headless). Un VPS 4 Go tient 5 instances confortablement, un VPS 8 Go en tient 10.

---

## Manager unifié — piloter toutes les instances depuis une seule UI

Plutôt que d'ouvrir 5 onglets de dashboard sur 5 ports différents, le **Manager** te donne une UI unique (port 8500) avec :

- **Sidebar** : sélecteur d'instance (`🟢 marie`, `🔴 camille`, ...) + boutons start/stop/restart par instance
- **Vue d'ensemble** : tableau de toutes les instances (status, port, dashboard URL, KPIs cross-comptes)
- **Vue instance** : iframe vers le dashboard de l'instance sélectionnée, intégrée dans la même page

### Démarrage

```bash
./scripts/start-manager.sh           # démarre (build au 1er run, ~30s)
./scripts/start-manager.sh logs      # suivre les logs en direct
./scripts/start-manager.sh stop      # arrêter
```

Puis ouvre `http://localhost:8500`. Si tu es sur un VPS, tunnel SSH :

```bash
ssh -L 8500:localhost:8500 user@vps
```

### Architecture

```
Container fansly-manager (port 8500)
    │
    ├─ Streamlit UI multi-comptes
    └─ Bind /var/run/docker.sock
           │
           ▼ (pilote via SDK Python docker-py)
    Containers fansly-bot-marie, fansly-bot-camille, ...
        (sur ports 8501, 8502, ...)
```

Le manager n'a **pas** besoin d'être dans le même réseau Docker que les bots — il les pilote uniquement via le socket Docker monté en bind.

### Sécurité

- Le socket Docker monté en bind donne au manager un **équivalent root** sur la machine hôte. **Jamais d'exposition publique** — accès uniquement via tunnel SSH (port `127.0.0.1:8500` seulement).
- Les iframes des dashboards individuels sont résolues par le navigateur de l'utilisateur, donc passent aussi par le tunnel SSH si on est sur VPS (le tunnel doit alors forward 8501-8510 en plus de 8500, ou faire un tunnel `*:*` style `-D 1080` SOCKS).

---

## Premier usage du dashboard

1. **Onglet Médias → onglet Lots** : crée un premier lot (ex: `ete_2026`), uploade des fichiers via l'expander "Ajouter des médias".
2. **Onglet Médias → onglet Légendes** : crée un lot de légendes (texte + hashtags). Le bot piochera au hasard dans le lot à chaque publication.
3. **Onglet Contrôle → Publication** : sélectionne ton lot, choisis un preset de rythme, et clique "Enfiler dans la queue". Le worker prend le job, publie chaque média à intervalles humanisés, attache une preview pour la visibilité FYP, ajoute `#fyp` automatiquement à la légende si absent.
4. **Onglet Contrôle → Purge** : nettoyage temporel (entre deux dates) ou par mots-clés. Mode **dry-run par défaut** pour observer avant d'agir.
5. **Onglet Contrôle → Queue** : suivi en temps réel des jobs (avec auto-refresh).
6. **Onglet Logs** : consultation des logs JSON par job ou globalement.

---

## Maintenance courante

```bash
# Voir le statut
docker compose ps

# Suivre les logs en direct
docker compose logs -f

# Redémarrer
docker compose restart

# Arrêter (sans détruire le container)
docker compose down

# Rebuild après modification du code
docker compose up -d --build

# Entrer dans le container pour debug
docker compose exec fansly-bot bash
```

---

## Mode natif (sans Docker, pour développement)

Pour itérer rapidement sur le code sans rebuild Docker à chaque fois :

```bash
# Crée le venv (Python 3.11+ obligatoire — verifie avec `python3.12 --version`)
python3.12 -m venv .venv
source .venv/bin/activate         # macOS / Linux
# .venv\Scripts\activate          # Windows

# Installe le projet en mode editable (mappe automatiquement src/ via pyproject)
pip install --upgrade pip
pip install -e .
playwright install chromium       # ~300 Mo, 2-3 min

# Lance le worker (dans un terminal)
python -m fansly_bot worker

# Lance Streamlit (dans un autre terminal)
streamlit run src/fansly_dashboard/main.py

# Tests de fumée (optionnel, ~0.5s)
python -m unittest discover -s tests
```

> **macOS Apple Silicon** : sur certains setups Homebrew, `python3.12 -m venv` plante sur `ensurepip` (`Symbol not found: _XML_GetCurrentByteIndex`). C'est un mismatch de libexpat. Solution : prefixer **toutes** les commandes ci-dessus avec `DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib` (les scripts `init-env.sh` / `setup-auth.sh` gèrent ça automatiquement).

---

## Déploiement sur un VPS

Pour faire tourner le bot 24/7 sur un VPS (Hetzner, OVH, etc.) et y accéder via tunnel SSH depuis ton poste local, suis le guide dédié : [`docs/VPS_DEPLOY.md`](docs/VPS_DEPLOY.md).

Résumé : tu copies le repo + le `data/` + le `.env` sur le VPS, tu fais `docker compose up -d --build`, et tu accèdes au dashboard via `ssh -L 8501:localhost:8501 user@vps`.

---

## Sécurité et confidentialité

| Élément | Statut |
|---|---|
| Credentials Fansly (`.env`) | Gitignored, jamais embarqué dans l'image Docker |
| Cookies de session (`data/browser_profile/`) | Gitignored, isolés par installation |
| Base de données SQLite (`data/state.db*`) | Gitignored — contient l'historique en clair, à backup soigneusement |
| Médias et légendes (`data/Medias/`, `data/Captions/`) | Gitignored — restent strictement chez toi |
| Streamlit | Lié à `127.0.0.1:8501`, **jamais exposé publiquement** (accès via tunnel SSH sur VPS) |
| Validation des entrées | Centralisée via `infra/names.validate_batch_name`, défense en profondeur contre path traversal |
| Mode `dry-run` | Activé par défaut pour la purge (sécurité contre les fausses manips) |

---

## Dépannage

### Port 8501 déjà occupé

Au démarrage de Docker, si tu vois `port already allocated` :

```bash
# Identifier ce qui occupe le port
lsof -i :8501          # macOS / Linux
netstat -ano | findstr 8501   # Windows

# Si c'est un Streamlit local oublié :
pkill -f "streamlit run"   # macOS / Linux
```

### Le worker ne démarre pas

```bash
docker compose logs --tail=100 fansly-bot
```

Erreurs fréquentes : `.env` manquant ou champs vides, `config.yaml` introuvable, profil Fansly invalide. Les messages d'erreur Pydantic indiquent précisément le champ fautif.

### Login Fansly refusé après quelques jours

La session Fansly peut expirer. Relance le setup-auth :

```bash
./scripts/setup-auth.sh
```

Et redémarre le container :

```bash
docker compose restart
```

### Chromium crash dans Docker

Vérifie que `shm_size: 1gb` est bien dans le `docker-compose.yml`. Sur certains setups (Docker Desktop avec RAM limitée), augmente la mémoire allouée à Docker dans les préférences.

### Tests de fumée (smoke tests)

Pour valider l'intégrité de la BDD et de la validation après modification du code :

```bash
# Avec un venv actif (mode natif)
python -m unittest tests.test_smoke -v
```

---

## Limitations connues

- **Détection anti-bot Fansly / Cloudflare** : le bot utilise `playwright-stealth` mais Fansly peut, à tout moment, durcir ses protections. Si tu vois apparaître des challenges Cloudflare ou des CAPTCHAs récurrents, l'automatisation peut devenir partiellement bloquée.
- **Un seul environnement actif à la fois** : ne lance pas le bot sur deux machines en parallèle avec le même `browser_profile` — Fansly détectera deux sessions concurrentes et risque de te déconnecter.
- **Aucune garantie ToS** : Fansly a ses propres conditions d'utilisation. Ce projet est fourni à des fins éducatives et de productivité personnelle. C'est à toi de t'assurer que ton usage est conforme.

---

## Structure du projet

```
fansly-bot/
├── src/
│   ├── fansly_bot/             # Runtime du bot (worker, services, infra)
│   │   ├── __main__.py         # Points d'entrée CLI : worker, setup-auth
│   │   ├── worker.py           # Boucle de consommation de la file de jobs
│   │   ├── services/           # Métier : uploader, purger, cycle_cleaner, ...
│   │   ├── browser/            # Wrappers Playwright + humanizer
│   │   ├── infra/              # State (SQLite), retry, validation
│   │   └── config.py           # Pydantic settings
│   └── fansly_dashboard/       # Dashboard Streamlit
│       ├── main.py             # Point d'entrée Streamlit
│       ├── _lib.py             # Helpers partagés entre vues
│       └── views/              # Pages : controle, medias, logs
├── tests/                      # Tests de fumée (unittest)
├── scripts/                    # Scripts d'aide (init-env, setup-auth)
├── docker/
│   └── entrypoint.sh           # Entrypoint du container
├── docs/
│   └── VPS_DEPLOY.md           # Guide de déploiement VPS
├── config.yaml                 # Configuration technique (sélecteurs, timings)
├── .env.example                # Template à copier vers .env
├── Dockerfile                  # Image basée sur Playwright officielle
├── docker-compose.yml          # Service unique, bind mounts, healthcheck
└── pyproject.toml              # Dépendances Python (PEP 517)
```

