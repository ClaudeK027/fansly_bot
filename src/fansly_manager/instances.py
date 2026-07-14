"""Gestion des instances Fansly bot via le socket Docker.

Le manager tourne dans son propre container avec /var/run/docker.sock monte
en bind (mode read-write pour pouvoir start/stop). Toutes les operations
passent par le SDK docker-py.

Convention de nommage des containers : ``fansly-bot-NAME`` (cf.
docker-compose.yml). On reconnait aussi ``fansly-bot`` (sans suffixe)
comme l'instance ``default`` pour la retro-compat single-instance.

Toutes les operations exposees aux vues sont enveloppees dans des
``safe_*`` qui catchent DockerException/APIError et retournent un
``Result(ok, value, error)``. La vue n'a JAMAIS a gerer les exceptions
docker-py, elle affiche juste error si ok=False.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Generic, Optional, TypeVar

if TYPE_CHECKING:
    from docker.models.containers import Container  # type: ignore[import-untyped]


# Convention : "fansly-bot-NAME" ou NAME = [A-Za-z0-9_]{1,64}. Borne haute
# de 64 chars cap volontaire : empeche un attaquant local qui peut creer
# des containers de flood l'UI avec un nom geant (defense-in-depth).
_INSTANCE_NAME_RE = re.compile(r"^fansly-bot-(?P<name>[A-Za-z0-9_]{1,64})$")
# Format legacy single-instance pre-multi-instance.
_LEGACY_NAME_RE = re.compile(r"^fansly-bot$")


T = TypeVar("T")


@dataclass
class Result(Generic[T]):
    """Resultat d'une operation Docker, jamais une exception cote vue.

    Pattern : ``ok, value, error = result`` (ou usage direct des fields).
    Si ``ok`` est True, ``value`` contient le resultat ; sinon ``error``
    contient un message lisible (pas une stack trace).
    """

    ok: bool
    value: Optional[T] = None
    error: Optional[str] = None


@dataclass
class Instance:
    """Vue serialisable d'une instance bot."""

    name: str           # ex: "marie"
    container: str      # ex: "fansly-bot-marie"
    status: str         # 'running' | 'exited' | 'paused' | 'restarting' | ...
    host_port: Optional[int]  # ex: 8501 (None si pas de mapping)
    image: str
    # ISO 8601 timestamp du dernier StartedAt Docker (None si jamais demarre).
    started_at: Optional[str] = None

    @property
    def is_running(self) -> bool:
        return self.status == "running"

    @property
    def dashboard_url(self) -> Optional[str]:
        # Validation defensive : port doit etre dans la plage non-privilegiee.
        if self.host_port is None or not (1024 <= self.host_port <= 65535):
            return None
        if not self.is_running:
            return None
        return f"http://localhost:{self.host_port}"


# ─── Helpers internes ────────────────────────────────────────────────────


def _docker():
    """Import lazy de docker-py."""
    import docker  # type: ignore[import-untyped]
    return docker


def _client() -> Any:
    """Retourne un client Docker base sur l'env."""
    return _docker().from_env()


def _extract_host_port(container_attrs: dict) -> Optional[int]:
    """Recupere le port host mappe sur 8501/tcp (port Streamlit interne).

    Filtre par HostIp='127.0.0.1' en priorite (le binding attendu) pour
    eviter de servir un IPv6 ou un 0.0.0.0 imprevu.
    """
    try:
        ports = container_attrs.get("NetworkSettings", {}).get("Ports", {}) or {}
        mappings = ports.get("8501/tcp") or []
        if not mappings:
            return None
        # Priorite : HostIp='127.0.0.1', sinon premier match
        for m in mappings:
            if (m.get("HostIp") or "").startswith("127.0.0.1"):
                return int(m["HostPort"])
        return int(mappings[0]["HostPort"])
    except (KeyError, ValueError, TypeError):
        return None


def _container_to_instance(c: "Container") -> Optional[Instance]:
    """Convertit un Container docker-py en Instance, ou None si le nom ne
    matche pas le pattern attendu (incluant le legacy ``fansly-bot``).

    Le ``c.attrs`` est passe une seule fois (docker-py inspect deja inclus
    dans le listing) — pas de round-trip supplementaire.
    """
    name: Optional[str] = None
    if (m := _INSTANCE_NAME_RE.match(c.name)) is not None:
        name = m.group("name")
    elif _LEGACY_NAME_RE.match(c.name):
        name = "default"
    else:
        return None
    started_at = c.attrs.get("State", {}).get("StartedAt") or None
    # NE PAS utiliser c.image[.tags] : docker-py fait un lazy GET
    # /images/<id>/json qui leve ImageNotFound (404) si l'image a ete
    # supprimee/remplacee (typiquement apres un rebuild du tag qui orpheline
    # l'ancienne image encore referencee par un container arrete). Ce 404
    # remontait jusqu'au except global de safe_list_instances et faisait
    # planter TOUTE la liste (un seul container pourri aveuglait le manager).
    # On lit la reference d'image depuis c.attrs (deja charge par le listing,
    # aucun round-trip, jamais 404) : Config.Image = le tag au moment de la
    # creation (ex "fansly-bot:latest"), fallback sur l'ID court tronque.
    img_id = c.attrs.get("Image", "") or ""
    img_short = img_id.split(":")[-1][:12] if img_id else "?"
    image_ref = (c.attrs.get("Config", {}) or {}).get("Image") or img_short
    return Instance(
        name=name,
        container=c.name,
        status=c.status,
        host_port=_extract_host_port(c.attrs),
        image=image_ref,
        started_at=started_at,
    )


def _resolve_container_name(name: str) -> str:
    """Resout le nom de container Docker depuis le nom logique d'instance.

    Pour ``default``, tente le format moderne ``fansly-bot-default`` puis
    fallback sur l'ancien ``fansly-bot`` (retro-compat). Pour les autres
    noms, retourne directement ``fansly-bot-NAME``.

    Note : peut lever DockerException si le daemon est injoignable —
    appele uniquement depuis les helpers safe_* qui catchent.
    """
    nf_exc = _docker().errors.NotFound
    modern = f"fansly-bot-{name}"
    client = _client()
    try:
        client.containers.get(modern)
        return modern
    except nf_exc:
        pass
    if name == "default":
        try:
            client.containers.get("fansly-bot")
            return "fansly-bot"
        except nf_exc:
            pass
    return modern  # NotFound a l'usage


# ─── API publique (safe : aucune exception ne remonte) ───────────────────


def _docker_exception_msg(e: Exception) -> str:
    """Message lisible pour l'utilisateur a partir d'une DockerException."""
    msg = str(e).strip() or e.__class__.__name__
    # Truncate stack traces / json blobs
    return msg.split("\n")[0][:200]


def safe_list_instances() -> Result[list[Instance]]:
    """Liste toutes les instances bot. Catch toute DockerException.

    Returns:
        Result(ok=True, value=[Instance, ...]) en cas de succes.
        Result(ok=False, error="message lisible") si le daemon est
        injoignable, permission refusee, etc.
    """
    try:
        client = _client()
        instances: list[Instance] = []
        for c in client.containers.list(all=True):
            # Isolation par container : un container corrompu (image morte,
            # attrs partiels, etc.) ne doit JAMAIS faire echouer toute la
            # liste. On le skip et on continue — le manager reste utilisable.
            try:
                inst = _container_to_instance(c)
            except Exception:  # noqa: BLE001 — resilience per-container
                continue
            if inst is not None:
                instances.append(inst)
        instances.sort(key=lambda i: i.name)
        return Result(ok=True, value=instances)
    except Exception as e:  # noqa: BLE001 — surface large voulue
        return Result(ok=False, error=_docker_exception_msg(e))


def safe_get_instance(name: str) -> Result[Optional[Instance]]:
    """Recupere une instance par son nom logique. Single round-trip Docker."""
    try:
        client = _client()
        try:
            c = client.containers.get(_resolve_container_name(name))
        except _docker().errors.NotFound:
            return Result(ok=True, value=None)
        return Result(ok=True, value=_container_to_instance(c))
    except Exception as e:  # noqa: BLE001
        return Result(ok=False, error=_docker_exception_msg(e))


def safe_start_instance(name: str) -> Result[None]:
    try:
        client = _client()
        c = client.containers.get(_resolve_container_name(name))
        c.start()
        return Result(ok=True)
    except _docker().errors.NotFound:
        return Result(ok=False, error=f"Container introuvable : {name}")
    except Exception as e:  # noqa: BLE001
        return Result(ok=False, error=_docker_exception_msg(e))


def safe_stop_instance(name: str, timeout: int = 30) -> Result[None]:
    """SIGTERM puis SIGKILL apres timeout seconds."""
    try:
        client = _client()
        c = client.containers.get(_resolve_container_name(name))
        c.stop(timeout=timeout)
        return Result(ok=True)
    except _docker().errors.NotFound:
        return Result(ok=False, error=f"Container introuvable : {name}")
    except Exception as e:  # noqa: BLE001
        return Result(ok=False, error=_docker_exception_msg(e))


def safe_restart_instance(name: str, timeout: int = 30) -> Result[None]:
    try:
        client = _client()
        c = client.containers.get(_resolve_container_name(name))
        c.restart(timeout=timeout)
        return Result(ok=True)
    except _docker().errors.NotFound:
        return Result(ok=False, error=f"Container introuvable : {name}")
    except Exception as e:  # noqa: BLE001
        return Result(ok=False, error=_docker_exception_msg(e))


# ─── Compat (anciens noms, gardent les meme contracts) ───────────────────
# Les helpers safe_* sont la voie recommandee ; ces alias existent pour
# les tests et toute API externe qui voudrait ignorer les erreurs.

def list_instances() -> list[Instance]:
    """Liste les instances (silencieux sur erreur, retourne [] si daemon KO)."""
    return safe_list_instances().value or []


def get_instance(name: str) -> Optional[Instance]:
    return safe_get_instance(name).value


def start_instance(name: str) -> bool:
    return safe_start_instance(name).ok


def stop_instance(name: str, timeout: int = 30) -> bool:
    return safe_stop_instance(name, timeout).ok


def restart_instance(name: str, timeout: int = 30) -> bool:
    return safe_restart_instance(name, timeout).ok
