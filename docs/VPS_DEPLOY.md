# Déploiement VPS du bot Fansly

Procédure complète pour déployer le bot sur un VPS Linux, accéder au dashboard via tunnel SSH, et maintenir le service dans la durée.

---

## Choix du VPS

Recommandation : **Hetzner CX22** ou équivalent.

| Critère | Valeur |
|---|---|
| Spec recommandée | 2 vCPU, 4 Go RAM, 40 Go SSD |
| OS | Debian 12 (Bookworm) ou Ubuntu 24.04 (Noble) |
| Coût indicatif | 4-6 € / mois |
| Localisation | Allemagne ou Finlande (Hetzner), France (OVH) |

**Pourquoi 4 Go de RAM** : Chromium consomme ~800 Mo à 1.5 Go en plein cycle, Streamlit ~300 Mo, Python worker ~200 Mo. Avec 4 Go on a une marge confortable et on évite les kills OOM du noyau.

Alternatives : OVH VPS Starter, Scaleway DEV1-S, DigitalOcean Basic Droplet 4 GB.

---

## Étape 1 — Préparation du VPS

### Connexion initiale et utilisateur dédié

Connecte-toi en root via la clé SSH que tu as fournie lors de la commande :

```bash
ssh root@TON_IP
```

Crée un utilisateur non-root pour faire tourner le bot (ne jamais faire tourner Docker en root direct) :

```bash
adduser fansly --gecos "" --disabled-password
usermod -aG sudo fansly
mkdir -p /home/fansly/.ssh
cp ~/.ssh/authorized_keys /home/fansly/.ssh/
chown -R fansly:fansly /home/fansly/.ssh
chmod 700 /home/fansly/.ssh
chmod 600 /home/fansly/.ssh/authorized_keys
```

Désactive le login root (sécurité) :

```bash
sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
systemctl restart sshd
```

Quitte la session root et reconnecte-toi avec le nouvel utilisateur :

```bash
exit
ssh fansly@TON_IP
```

### Installation de Docker

Méthode officielle (script Docker) :

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker fansly
```

Déconnecte-toi et reconnecte-toi pour appliquer l'ajout au groupe `docker`. Vérifie ensuite :

```bash
docker --version
docker compose version
```

### Firewall minimal

UFW (Uncomplicated Firewall) est dans Ubuntu/Debian. On bloque tout sauf SSH :

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp
sudo ufw enable
```

Streamlit n'est **pas** ouvert en externe : il sera accessible uniquement via tunnel SSH (voir étape 4).

---

## Étape 2 — Récupération du code et configuration

### Cloner le repo

```bash
cd /home/fansly
git clone <URL_DU_REPO> fansly-bot
cd fansly-bot
```

Si tu n'as pas encore mis le code sur un repo Git, tu peux transférer le dossier depuis ton local :

```bash
# Depuis ton Mac :
rsync -avz --exclude '.venv' --exclude 'data' --exclude '.env' \
    /Users/claudemenye/Documents/Project./fansly/ \
    fansly@TON_IP:/home/fansly/fansly-bot/
```

### Fichier `.env` sur le VPS

Le `.env` contient les credentials Fansly. Il n'est jamais versionné ni inclus dans l'image Docker. Crée-le directement sur le VPS :

```bash
cd /home/fansly/fansly-bot
nano .env
```

Contenu attendu :

```
FANSLY_USERNAME=ton.email@example.com
FANSLY_PASSWORD=ton_mot_de_passe_fansly
```

Permissions strictes :

```bash
chmod 600 .env
```

---

## Étape 3 — Authentification Fansly (première fois uniquement)

Le bot a besoin que Chromium soit connecté à ton compte Fansly. Les cookies de session sont stockés dans `data/browser_profile/`. Comme le VPS est headless (pas d'écran), on ne peut pas faire le login interactif directement dessus.

**Approche recommandée** : faire le login en local sur ton Mac, puis copier le `browser_profile` vers le VPS.

### A. Sur ton Mac

Lance le bot en mode `setup-auth` (qui ouvre Chromium en mode visible pour que tu te connectes à la main) :

```bash
cd /Users/claudemenye/Documents/Project./fansly
DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib .venv/bin/python -m fansly_bot setup-auth
```

Chromium s'ouvre. Connecte-toi sur Fansly comme d'habitude. Le bot détecte le login OK et sauvegarde les cookies dans `data/browser_profile/`.

### B. Copier le profil vers le VPS

```bash
rsync -avz /Users/claudemenye/Documents/Project./fansly/data/browser_profile/ \
    fansly@TON_IP:/home/fansly/fansly-bot/data/browser_profile/
```

Le profil Chromium est cross-platform (les cookies sont dans des SQLite portables). Une fois copié, le bot du VPS reprend la session sans demander de re-login.

---

## Étape 4 — Premier démarrage du container

### Build et lancement

```bash
cd /home/fansly/fansly-bot
docker compose up -d --build
```

Le premier build télécharge l'image Playwright (~700 Mo) et installe les dépendances Python. Compte 3-5 minutes selon la connexion du VPS.

### Vérifier que tout tourne

```bash
docker compose ps
docker compose logs -f
```

Tu dois voir l'entrypoint lancer le worker, puis Streamlit démarrer. Le container doit passer en état `healthy` au bout de ~30s (le healthcheck attend que Streamlit réponde sur `/_stcore/health`).

---

## Étape 5 — Accès au dashboard via tunnel SSH

Sur ton poste local (Mac), ouvre un terminal et lance le tunnel :

```bash
ssh -L 8501:localhost:8501 fansly@TON_IP
```

Tant que cette session SSH reste ouverte, le port `8501` de ton Mac est forwardé vers le port `8501` du VPS (qui n'est accessible qu'en local sur le VPS). Ouvre dans ton navigateur :

```
http://localhost:8501
```

C'est le dashboard du VPS, transitant par SSH chiffré, sans aucune exposition publique.

### Tunnel en background

Pour ne pas avoir à garder un terminal ouvert :

```bash
ssh -fN -L 8501:localhost:8501 fansly@TON_IP
```

Le tunnel tourne en arrière-plan. Pour le couper :

```bash
pkill -f "ssh -fN -L 8501"
```

### Alias pratique dans `~/.zshrc` ou `~/.bashrc`

```bash
alias fansly-tunnel="ssh -fN -L 8501:localhost:8501 fansly@TON_IP && open http://localhost:8501"
alias fansly-tunnel-stop="pkill -f 'ssh -fN -L 8501'"
```

---

## Étape 6 — Maintenance courante

### Voir les logs

```bash
ssh fansly@TON_IP
cd ~/fansly-bot
docker compose logs -f                  # tous les logs
docker compose logs -f --tail 100       # 100 dernieres lignes
tail -f data/logs/fansly-bot.jsonl      # logs structlog du bot directement
```

### Redémarrer le container

```bash
docker compose restart
```

### Mise à jour du code

Si le code est sur Git :

```bash
ssh fansly@TON_IP
cd ~/fansly-bot
git pull
docker compose up -d --build
```

Si tu modifies depuis ton Mac et push via rsync :

```bash
# Depuis ton Mac :
rsync -avz --delete --exclude '.venv' --exclude 'data' --exclude '.env' \
    /Users/claudemenye/Documents/Project./fansly/ \
    fansly@TON_IP:/home/fansly/fansly-bot/

# Sur le VPS :
ssh fansly@TON_IP
cd ~/fansly-bot
docker compose up -d --build
```

### Arrêt complet

```bash
docker compose down
```

Le container s'arrête, mais `data/` reste sur le VPS (volume bind mount). Au prochain `docker compose up -d`, tout reprend où ça en était.

---

## Étape 7 — Backups

L'état complet du bot vit dans `data/` sur le VPS :

- `data/state.db*` — base de données SQLite (historique, file de jobs, lots actifs)
- `data/Medias/` — médias à publier
- `data/Captions/` — lots de légendes
- `data/browser_profile/` — session Fansly authentifiée
- `data/logs/` — logs (peut être exclu des backups)
- `data/artifacts/` — dumps de diagnostic (peut être exclu)

### Backup vers ton Mac

```bash
# Depuis ton Mac, sauvegarde quotidienne dans ~/Backups/fansly/
rsync -avz --exclude 'logs' --exclude 'artifacts' \
    fansly@TON_IP:/home/fansly/fansly-bot/data/ \
    ~/Backups/fansly/$(date +%Y-%m-%d)/
```

### Cron de backup automatique sur le VPS

Pour une copie locale sur le VPS (utile entre deux backups distants) :

```bash
crontab -e
```

Ajoute :

```
0 3 * * * cd /home/fansly/fansly-bot && tar czf /home/fansly/backups/data-$(date +\%Y\%m\%d).tar.gz --exclude data/logs --exclude data/artifacts data/ && find /home/fansly/backups -name 'data-*.tar.gz' -mtime +7 -delete
```

Crée le dossier d'abord :

```bash
mkdir -p /home/fansly/backups
```

---

## Dépannage

### Le container ne démarre pas

```bash
docker compose logs
```

Causes fréquentes :
- `.env` manquant ou mal formaté → erreur Pydantic au démarrage du worker.
- `config.yaml` manquant à la racine du dossier projet.
- Port 8501 déjà utilisé sur le VPS (autre Streamlit en cours) → `netstat -tlnp | grep 8501`.

### Streamlit répond pas au tunnel

Vérifie que le container est `healthy` :

```bash
docker compose ps
```

Si `unhealthy`, c'est que Streamlit a planté. Logs :

```bash
docker compose logs fansly-bot | tail -100
```

### Le worker ne démarre pas au boot du container

Vérifie `AUTOSTART_WORKER` dans le `docker-compose.yml` ou via env. Sinon, lance-le manuellement via le dashboard (bouton "Lancer").

### Login Fansly refuse au démarrage

Le `browser_profile` est peut-être trop vieux et la session a expiré. Refaire l'étape 3 (login local + rsync).

### Le VPS reboot et le bot ne redémarre pas

`restart: unless-stopped` couvre les reboots. Vérifie que Docker démarre au boot :

```bash
sudo systemctl enable docker
```

---

## Sécurité — récap

| Mesure | Statut |
|---|---|
| SSH par clé uniquement (root désactivé) | À configurer (étape 1) |
| UFW : seul le port 22 ouvert | À configurer (étape 1) |
| Streamlit lié à `127.0.0.1` du VPS, jamais public | OK (compose.yml) |
| `.env` en `chmod 600`, jamais versionné | À configurer (étape 2) |
| `data/state.db*` exclu du gitignore | OK |
| Docker tourne sous utilisateur dédié `fansly` | À configurer (étape 1) |
| Backups réguliers de `data/` | À configurer (étape 7) |

---

## Annexe — récap des commandes utiles

```bash
# Logs en temps réel
docker compose logs -f

# Redémarrer
docker compose restart

# Rebuild après modif code
docker compose up -d --build

# Arrêter
docker compose down

# État du container
docker compose ps

# Entrer dans le container (debug)
docker compose exec fansly-bot bash

# Espace disque utilisé par Docker
docker system df

# Nettoyer les images obsolètes après un rebuild
docker image prune -f
```
