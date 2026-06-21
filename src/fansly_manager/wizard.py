"""Backend du wizard d'authentification noVNC.

Orchestre les containers ``fansly-novnc`` (auth visuelle a distance) et
gere le lifecycle d'une nouvelle instance bot creee depuis le manager :

  1. Build (si besoin) de l'image fansly-novnc (apres modification du
     Dockerfile)
  2. Run d'un container nomme ``fansly-novnc-NAME`` avec :
       - env FANSLY_USERNAME / FANSLY_PASSWORD / FANSLY_PROFILE_SLUG /
         INSTANCE_NAME (transmis par le wizard)
       - port 6080 mappe sur 127.0.0.1:<port libre>
       - volume bind ``<host_data_dir>/data-NAME``:/output (browser_profile
         sera ecrit par le container)
  3. Poll du status du container : ``running`` -> en cours, ``exited 0``
     -> login OK, ``exited >0`` -> echec/timeout.
  4. Une fois le login OK, ecrit le .env.NAME et lance le container bot
     via docker-compose run equivalent (`docker run`).
  5. Nettoie le container noVNC.

Tous les operations encapsulees dans Result(ok, value, error) — la vue
Streamlit n'a JAMAIS a gerer d'exception docker-py.
"""
from __future__ import annotations

import os
import re
import socket
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from fansly_manager.instances import Result, _client, _docker, _docker_exception_msg


# Labels Docker poses sur les containers fansly-novnc-* crees par le wizard.
# Sert au garbage collector au boot du manager (cleanup_stale_novnc_containers).
NOVNC_LABEL_ROLE = "fansly.role"
NOVNC_LABEL_ROLE_VALUE = "novnc-wizard"
NOVNC_LABEL_INSTANCE = "fansly.instance"
NOVNC_LABEL_CREATED_AT = "fansly.created_at"

# Seuil au-dela duquel un container fansly-novnc-* sans wizard actif
# est considere orphelin (fermeture onglet, crash manager).
NOVNC_STALE_THRESHOLD_S = 30 * 60  # 30 minutes


# La regex est alignee avec scripts/init-env.sh --instance (Phase 1)
INSTANCE_NAME_RE = re.compile(r"^[a-zA-Z0-9_]{1,32}$")


@dataclass
class WizardConfig:
    """Etat collecte par le wizard (steps 1+2) puis passe a start_auth()."""

    instance_name: str
    fansly_username: str
    fansly_password: str
    fansly_profile_slug: str


# ─── Host paths (resolus depuis le manager) ──────────────────────────────
#
# Le manager tourne dans un container. Deux paths distincts :
#
# - HOST_REPO_PATH         : path absolu COTE HOST. Utilise pour les
#                             docker run --volume (le daemon Docker resout
#                             les paths cote host, jamais cote container
#                             manager).
# - IN_CONTAINER_REPO_PATH : path du meme repo VU PAR LE MANAGER (via bind
#                             mount). Utilise pour les operations Python
#                             file system : open(), write_text(), etc.
#
# Quand le manager tourne hors docker (dev local Streamlit), les 2 sont
# identiques (= cwd du repo).


def _host_repo_root() -> Optional[Path]:
    """Path du repo COTE HOST (utilise pour les docker run --volume)."""
    p = os.environ.get("HOST_REPO_PATH")
    if p:
        return Path(p)
    # Fallback dev local
    cwd = Path.cwd()
    if (cwd / "pyproject.toml").is_file() and (cwd / "src" / "fansly_bot").is_dir():
        return cwd
    return None


def _container_repo_root() -> Optional[Path]:
    """Path du repo VU PAR LE MANAGER (pour ecrire .env.NAME etc).

    En containerise : ``/host_repo`` (bind mount depuis docker-compose).
    En dev local : meme path que _host_repo_root().
    """
    p = os.environ.get("IN_CONTAINER_REPO_PATH")
    if p and Path(p).is_dir():
        return Path(p)
    return _host_repo_root()


# ─── Port allocation ─────────────────────────────────────────────────────


def _find_free_port(start: int = 6080, end: int = 6180) -> Optional[int]:
    """Trouve un port libre sur localhost pour exposer noVNC du wizard.

    On reste dans la plage 6080-6180 par convention (noVNC default = 6080).
    """
    for port in range(start, end):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return None


# ─── Validation ──────────────────────────────────────────────────────────


def validate_instance_name(name: str) -> Optional[str]:
    """Retourne un message d'erreur lisible ou None si valide."""
    if not name:
        return "Le nom est obligatoire."
    if not INSTANCE_NAME_RE.match(name):
        return (
            "Format invalide : utilise uniquement des lettres, chiffres et "
            "underscore (1-32 caracteres)."
        )
    return None


def instance_already_exists(name: str) -> bool:
    """Verifie qu'aucun container fansly-bot-NAME ou fansly-novnc-NAME existe.

    Retourne True meme pour un container noVNC en statut ``exited`` —
    utiliser ``instance_already_exists_or_cleanup`` pour la logique de
    retry apres crash (qui nettoie automatiquement les orphelins exited).
    """
    try:
        client = _client()
        for candidate in (f"fansly-bot-{name}", f"fansly-novnc-{name}"):
            try:
                client.containers.get(candidate)
                return True
            except _docker().errors.NotFound:
                continue
        return False
    except Exception:  # noqa: BLE001
        return False


def instance_already_exists_or_cleanup(name: str) -> Result[None]:
    """Variante intelligente : si un container ``fansly-novnc-NAME`` exited
    est present (crash precedent), on le nettoie automatiquement et on
    laisse l'user retenter le wizard.

    Bloque uniquement si :
      - un container ``fansly-bot-NAME`` existe (instance bot reelle) ;
      - OU un container ``fansly-novnc-NAME`` est ``running``/``restarting``/
        ``paused`` (un autre wizard est en cours pour ce meme nom).

    Retourne ``Result(ok=True)`` quand l'user peut continuer, ``Result(
    ok=False, error=...)`` avec un message actionnable sinon.
    """
    try:
        client = _client()
        # Bot existe -> instance reelle, on bloque
        try:
            client.containers.get(f"fansly-bot-{name}")
            return Result(
                ok=False,
                error=f"L'instance {name} existe deja. Choisis un autre nom.",
            )
        except _docker().errors.NotFound:
            pass
        # noVNC orphelin ?
        try:
            old = client.containers.get(f"fansly-novnc-{name}")
            old.reload()
            if old.status in ("running", "restarting", "paused"):
                return Result(
                    ok=False,
                    error=(
                        f"Un wizard est deja en cours pour l'instance {name}. "
                        f"Attends sa fin (≤10 min) ou choisis un autre nom."
                    ),
                )
            # exited / created / dead : orphelin -> on nettoie
            try:
                old.remove(force=True)
            except Exception:  # noqa: BLE001
                # Si on n'arrive pas a nettoyer, on ne peut pas continuer
                return Result(
                    ok=False,
                    error=(
                        f"Session precedente bloquee (container {old.name}). "
                        f"Supprime-le manuellement : docker rm -f {old.name}"
                    ),
                )
        except _docker().errors.NotFound:
            pass
        return Result(ok=True)
    except Exception as e:  # noqa: BLE001
        # Daemon down ou autre : on ne bloque pas l'user — l'erreur ressortira
        # plus tard lors du docker run.
        return Result(ok=True)


# ─── Image build ─────────────────────────────────────────────────────────


def _ensure_image_built(
    tag: str, dockerfile: str, what_for: str
) -> Result[None]:
    """Helper generique : verifie qu'une image existe, build sinon.

    ``what_for`` est juste un label humain pour les messages d'erreur.
    """
    try:
        client = _client()
        try:
            client.images.get(tag)
            return Result(ok=True)
        except _docker().errors.ImageNotFound:
            pass

        repo_in_container = _container_repo_root()
        if repo_in_container is None or not repo_in_container.is_dir():
            return Result(
                ok=False,
                error=(
                    f"Repo non accessible depuis le manager (build {what_for} "
                    f"impossible). Ajoute le bind mount HOST_REPO_PATH:/host_repo "
                    f"dans docker-compose.manager.yml."
                ),
            )

        client.images.build(
            path=str(repo_in_container),
            dockerfile=dockerfile,
            tag=tag,
            rm=True,
        )
        return Result(ok=True)
    except Exception as e:  # noqa: BLE001
        return Result(ok=False, error=_docker_exception_msg(e))


def ensure_novnc_image_built() -> Result[None]:
    """Build fansly-novnc:latest si absente (~3-5 min au 1er run)."""
    return _ensure_image_built(
        tag="fansly-novnc:latest",
        dockerfile="docker/Dockerfile.novnc",
        what_for="image noVNC",
    )


def ensure_bot_image_built() -> Result[None]:
    """Build fansly-bot:latest si absente.

    Le wizard cree un container ``fansly-bot:latest`` au step 4. Si
    l'utilisateur n'a JAMAIS lance le bot single-instance via
    ``docker compose up`` au prealable, cette image n'existe pas et le
    docker run echoue avec ImageNotFound APRES que l'user a fait son
    login noVNC (5-15 min perdus). On force le build en amont, en
    step 2, avant que l'user investisse du temps dans le login.

    Build typique ~2-4 min (image Playwright deja en cache si le
    wizard noVNC a deja ete build avant).
    """
    return _ensure_image_built(
        tag="fansly-bot:latest",
        dockerfile="Dockerfile",
        what_for="image bot",
    )


# ─── Lancement container noVNC ───────────────────────────────────────────


@dataclass
class NovncSession:
    """Vue d'une session noVNC active (container en cours de login)."""

    container_id: str       # full ID Docker
    container_name: str     # fansly-novnc-NAME
    host_port: int          # port host expose pour le browser de l'user
    instance_name: str
    data_dir_host: str      # path absolu HOST de data-NAME/


def start_auth_container(cfg: WizardConfig) -> Result[NovncSession]:
    """Lance le container fansly-novnc en arriere-plan, retourne la session."""
    try:
        repo = _host_repo_root()
        if repo is None:
            return Result(
                ok=False,
                error="HOST_REPO_PATH non defini cote manager.",
            )

        data_dir_host = repo / f"data-{cfg.instance_name}"
        # data_dir_host n'est pas accessible depuis le manager (sauf si
        # bind-mount supplementaire). Le mkdir se fait cote container
        # fansly-novnc lors du --volume = bind direct par Docker daemon
        # sur le host. On le cree NOTIONELLEMENT ici en passant la valeur
        # path string au docker run — Docker daemon cree le dossier si
        # absent quand il monte le volume.

        port = _find_free_port()
        if port is None:
            return Result(ok=False, error="Aucun port libre entre 6080-6180.")

        container_name = f"fansly-novnc-{cfg.instance_name}"

        client = _client()
        # Cleanup d'un eventuel container precedent de meme nom — SAUF si
        # un autre wizard est en cours de login (status running) : on ne
        # vole pas une session active. instance_already_exists au step 1
        # devrait normalement bloquer ce cas mais la race TOCTOU existe.
        try:
            old = client.containers.get(container_name)
            try:
                old.reload()
                if old.status in ("running", "restarting", "paused"):
                    return Result(
                        ok=False,
                        error=(
                            f"Un wizard est deja en cours pour l'instance "
                            f"{cfg.instance_name}. Attends sa fin ou supprime "
                            f"manuellement le container {container_name}."
                        ),
                    )
            except _docker().errors.NotFound:
                # Race : disparu entre get() et reload()
                pass
            else:
                old.remove(force=True)
        except _docker().errors.NotFound:
            pass

        container = client.containers.run(
            "fansly-novnc:latest",
            name=container_name,
            detach=True,
            remove=False,  # on garde le container pour pouvoir lire logs+exitcode
            environment={
                "FANSLY_USERNAME": cfg.fansly_username,
                "FANSLY_PASSWORD": cfg.fansly_password,
                "FANSLY_PROFILE_SLUG": cfg.fansly_profile_slug,
                "INSTANCE_NAME": cfg.instance_name,
                # LOGIN_TIMEOUT_S abaisse de 900 a 600s : limite la fenetre
                # pendant laquelle un container orphelin contient les creds
                # en clair lisibles via docker inspect.
                "LOGIN_TIMEOUT_S": "600",
            },
            ports={"6080/tcp": ("127.0.0.1", port)},
            volumes={
                str(data_dir_host): {"bind": "/output", "mode": "rw"},
            },
            shm_size="1g",
            # Labels qui permettent au cleanup au boot du manager de
            # detecter ce container et le supprimer si orphelin.
            labels={
                NOVNC_LABEL_ROLE: NOVNC_LABEL_ROLE_VALUE,
                NOVNC_LABEL_INSTANCE: cfg.instance_name,
                NOVNC_LABEL_CREATED_AT: str(int(time.time())),
            },
        )
        return Result(
            ok=True,
            value=NovncSession(
                container_id=container.id,
                container_name=container_name,
                host_port=port,
                instance_name=cfg.instance_name,
                data_dir_host=str(data_dir_host),
            ),
        )
    except Exception as e:  # noqa: BLE001
        return Result(ok=False, error=_docker_exception_msg(e))


def poll_auth_status(container_id: str) -> Result[dict]:
    """Recupere l'etat actuel du container noVNC.

    Retour : Result(ok=True, value={"status": "running|exited", "exit_code": int|None})
    """
    try:
        client = _client()
        c = client.containers.get(container_id)
        c.reload()
        attrs_state = c.attrs.get("State", {})
        return Result(
            ok=True,
            value={
                "status": c.status,  # 'running' | 'exited' | ...
                "exit_code": attrs_state.get("ExitCode"),
                "started_at": attrs_state.get("StartedAt"),
                "finished_at": attrs_state.get("FinishedAt"),
            },
        )
    except Exception as e:  # noqa: BLE001
        return Result(ok=False, error=_docker_exception_msg(e))


def cleanup_auth_container(container_id: str) -> Result[None]:
    """Supprime le container noVNC une fois la session terminee."""
    try:
        client = _client()
        c = client.containers.get(container_id)
        try:
            c.stop(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        c.remove(force=True)
        return Result(ok=True)
    except _docker().errors.NotFound:
        # Deja supprime (race avec un autre cleanup, retry double-clic)
        return Result(ok=True)
    except Exception as e:  # noqa: BLE001
        return Result(ok=False, error=_docker_exception_msg(e))


def cleanup_stale_novnc_containers(
    max_age_s: int = NOVNC_STALE_THRESHOLD_S,
) -> Result[list[str]]:
    """Garbage collector des containers noVNC orphelins.

    Scenarios cibles :
      - Utilisateur ferme l'onglet pendant l'etape 3 -> session_state perdue,
        le container fansly-novnc-NAME continue de tourner avec
        FANSLY_USERNAME/PASSWORD en clair dans son env (lisible via
        ``docker inspect``).
      - Container manager redemarre (OOM, mise a jour) entre step 2 et 4 ->
        idem.
      - LOGIN_TIMEOUT_S atteint -> exit 1 mais ``remove=False`` garde le
        container avec son env en lecture.

    A appeler au boot du manager. Filtre sur le label ``fansly.role=
    novnc-wizard`` et supprime tous les containers (running OU exited)
    crees il y a plus de ``max_age_s`` secondes.

    Retourne la liste des noms de containers supprimes pour les logs.
    """
    removed: list[str] = []
    try:
        client = _client()
        now = int(time.time())
        for c in client.containers.list(
            all=True,
            filters={"label": f"{NOVNC_LABEL_ROLE}={NOVNC_LABEL_ROLE_VALUE}"},
        ):
            # CRITIQUE : on ne tue JAMAIS un container running. Le user est
            # peut-etre en plein login Cloudflare/2FA (legitimement long).
            # Le container lui-meme exit apres LOGIN_TIMEOUT_S=600s, apres
            # quoi il devient eligible au GC.
            try:
                c.reload()
                if c.status in ("running", "restarting", "paused"):
                    continue
            except Exception:  # noqa: BLE001
                pass
            try:
                created_at_raw = c.labels.get(NOVNC_LABEL_CREATED_AT, "0")
                created_at = int(created_at_raw)
            except (ValueError, TypeError):
                # Label absent / corrompu : on considere stale par defaut
                created_at = 0
            age = now - created_at
            if age < max_age_s:
                continue
            try:
                c.remove(force=True)
                removed.append(c.name)
            except Exception:  # noqa: BLE001
                # Best-effort : on ignore les erreurs sur containers individuels
                continue
        return Result(ok=True, value=removed)
    except Exception as e:  # noqa: BLE001
        return Result(ok=False, error=_docker_exception_msg(e))


# ─── Provisioning de l'instance bot ──────────────────────────────────────


def write_env_file(cfg: WizardConfig) -> Result[None]:
    """Ecrit .env.NAME a la racine du repo (via le bind mount manager)."""
    try:
        repo = _container_repo_root()
        if repo is None:
            return Result(
                ok=False,
                error="Repo non accessible depuis le manager (bind mount manquant).",
            )

        env_path = repo / f".env.{cfg.instance_name}"
        content = textwrap.dedent(
            f"""\
            # {env_path.name} — genere par le wizard manager
            # NE JAMAIS commit ce fichier — il contient des secrets.

            FANSLY_USERNAME={cfg.fansly_username}
            FANSLY_PASSWORD={cfg.fansly_password}
            FANSLY_PROFILE_SLUG={cfg.fansly_profile_slug}
            """
        )
        env_path.write_text(content, encoding="utf-8")
        try:
            env_path.chmod(0o600)
        except Exception:  # noqa: BLE001
            pass
        return Result(ok=True)
    except Exception as e:  # noqa: BLE001
        return Result(ok=False, error=str(e))


def delete_env_file(instance_name: str) -> Result[None]:
    """Supprime .env.NAME en cas de rollback (start_bot_instance KO).

    Sans ca, on laisse sur disque un fichier contenant FANSLY_PASSWORD
    en clair pour une instance qui ne demarrera jamais.
    """
    try:
        repo = _container_repo_root()
        if repo is None:
            return Result(ok=False, error="Repo non accessible depuis le manager.")
        env_path = repo / f".env.{instance_name}"
        env_path.unlink(missing_ok=True)
        return Result(ok=True)
    except Exception as e:  # noqa: BLE001
        return Result(ok=False, error=str(e))


def start_bot_instance(cfg: WizardConfig) -> Result[dict]:
    """Lance le container fansly-bot-NAME avec le bon env/volumes/port.

    Equivalent de :
      INSTANCE_NAME=NAME ENV_FILE=.env.NAME DATA_DIR=./data-NAME HOST_PORT=PORT \
        docker compose --project-name fansly-NAME up -d --build

    Mais via docker-py (pas de docker compose dispo dans le container manager).
    On replique les memes options que docker-compose.yml du repo.
    """
    try:
        repo = _host_repo_root()
        if repo is None:
            return Result(ok=False, error="HOST_REPO_PATH non defini.")

        # Trouve un port host libre pour le dashboard Streamlit (8501-8599)
        port: Optional[int] = None
        for p in range(8501, 8600):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind(("127.0.0.1", p))
                    port = p
                    break
                except OSError:
                    continue
        if port is None:
            return Result(ok=False, error="Aucun port libre pour le dashboard (8501-8599).")

        # env_file : on LIT le contenu cote manager (bind mount) mais on
        # passe le path HOST au daemon Docker. Idem pour data_dir / config.
        repo_in = _container_repo_root()
        if repo_in is None:
            return Result(
                ok=False,
                error="Repo non accessible depuis le manager (bind mount manquant).",
            )
        env_vars = _parse_env_file(repo_in / f".env.{cfg.instance_name}")
        data_dir = repo / f"data-{cfg.instance_name}"
        config_yaml = repo / "config.yaml"

        client = _client()
        # Defense en profondeur : si on arrive ici sans que ensure_bot_image_built
        # ait ete appelee (chemin programmatique, test, etc.), on build maintenant
        # plutot que de laisser docker run lever ImageNotFound apres l'investissement
        # de temps dans le login noVNC.
        try:
            client.images.get("fansly-bot:latest")
        except _docker().errors.ImageNotFound:
            build_result = ensure_bot_image_built()
            if not build_result.ok:
                return Result(
                    ok=False,
                    error=f"Image fansly-bot:latest absente et build echoue : {build_result.error}",
                )

        container = client.containers.run(
            "fansly-bot:latest",
            name=f"fansly-bot-{cfg.instance_name}",
            detach=True,
            restart_policy={"Name": "unless-stopped"},
            environment=env_vars,
            ports={"8501/tcp": ("127.0.0.1", port)},
            volumes={
                str(data_dir): {"bind": "/app/data", "mode": "rw"},
                str(config_yaml): {"bind": "/app/config.yaml", "mode": "ro"},
            },
            shm_size="1g",
            mem_limit="3g",
            init=True,
        )
        return Result(ok=True, value={"container_id": container.id, "host_port": port})
    except Exception as e:  # noqa: BLE001
        return Result(ok=False, error=_docker_exception_msg(e))


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse un fichier .env (KEY=VALUE par ligne, ignore # et lignes vides)."""
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env
