"""Gestion des instances Fansly bot via le socket Docker.

Le manager tourne dans son propre container avec /var/run/docker.sock monte
en bind (mode read-write pour pouvoir start/stop). Toutes les operations
passent par le SDK docker-py.

Convention de nommage des containers : ``fansly-bot-NAME`` (cf. docker-compose.yml).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from docker.models.containers import Container  # type: ignore[import-untyped]


# Le manager lui-meme s'appelle "fansly-manager" -> on l'exclut des listings.
# Le format attendu d'un container instance est "fansly-bot-NAME" ou NAME
# matche la regex de --instance de init-env.sh (alphanumerique + underscore).
# On reconnait aussi l'ancien format single-instance "fansly-bot" (sans
# suffixe) comme l'instance "default" pour la retro-compat des deployments
# qui n'ont pas encore migre vers le nommage multi-instance.
_INSTANCE_NAME_RE = re.compile(r"^fansly-bot-(?P<name>[A-Za-z0-9_]+)$")
_LEGACY_NAME_RE = re.compile(r"^fansly-bot$")


def _docker():
    """Import lazy de docker-py pour ne pas casser les tests dans un
    environnement sans la lib installee (les tests qui ont besoin du SDK
    sont skip ou utilisent un mock)."""
    import docker  # type: ignore[import-untyped]
    return docker


@dataclass
class Instance:
    """Vue serialisable d'une instance bot."""

    name: str           # ex: "marie"
    container: str      # ex: "fansly-bot-marie"
    status: str         # 'running' | 'exited' | 'paused' | 'restarting' | ...
    host_port: Optional[int]  # ex: 8501 (None si pas de mapping)
    image: str

    @property
    def is_running(self) -> bool:
        return self.status == "running"

    @property
    def dashboard_url(self) -> Optional[str]:
        if self.host_port is None or not self.is_running:
            return None
        return f"http://localhost:{self.host_port}"


def _client() -> Any:
    """Retourne un client Docker base sur l'env (fonctionne avec le socket
    monte en bind via /var/run/docker.sock)."""
    return _docker().from_env()


def _extract_host_port(container: "Container") -> Optional[int]:
    """Recupere le port host mappe sur 8501/tcp (le port Streamlit interne)."""
    try:
        ports = container.attrs.get("NetworkSettings", {}).get("Ports", {}) or {}
        mappings = ports.get("8501/tcp") or []
        if mappings:
            # Format Docker : [{"HostIp": "127.0.0.1", "HostPort": "8501"}, ...]
            return int(mappings[0].get("HostPort"))
    except (KeyError, ValueError, TypeError):
        pass
    return None


def list_instances() -> list[Instance]:
    """Liste toutes les instances bot (running + stopped), triees par nom."""
    client = _client()
    instances: list[Instance] = []
    for c in client.containers.list(all=True):
        m = _INSTANCE_NAME_RE.match(c.name)
        if m is not None:
            name = m.group("name")
        elif _LEGACY_NAME_RE.match(c.name):
            # Container legacy single-instance pre-multi-instance.
            name = "default"
        else:
            continue
        instances.append(
            Instance(
                name=name,
                container=c.name,
                status=c.status,
                host_port=_extract_host_port(c),
                image=(c.image.tags[0] if c.image.tags else c.image.short_id),
            )
        )
    instances.sort(key=lambda i: i.name)
    return instances


def get_instance(name: str) -> Optional[Instance]:
    """Recupere une instance par son nom (sans le prefixe ``fansly-bot-``)."""
    for inst in list_instances():
        if inst.name == name:
            return inst
    return None


def _resolve_container_name(name: str) -> str:
    """Resout le nom de container Docker depuis le nom logique d'instance.

    Pour "default", essaye d'abord le format moderne ``fansly-bot-default``
    et fallback sur l'ancien ``fansly-bot`` si le moderne n'existe pas (cas
    des deployments legacy non encore migres).
    """
    client = _client()
    nf_exc = _docker().errors.NotFound
    modern = f"fansly-bot-{name}"
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
    return modern  # par defaut on retourne le format moderne (NotFound a l'usage)


def start_instance(name: str) -> bool:
    """Demarre un container stoppe. Retourne True si OK, False si introuvable."""
    client = _client()
    try:
        c = client.containers.get(_resolve_container_name(name))
        c.start()
        return True
    except _docker().errors.NotFound:
        return False


def stop_instance(name: str, timeout: int = 30) -> bool:
    """Stoppe gracieusement un container (SIGTERM puis SIGKILL apres timeout)."""
    client = _client()
    try:
        c = client.containers.get(_resolve_container_name(name))
        c.stop(timeout=timeout)
        return True
    except _docker().errors.NotFound:
        return False


def restart_instance(name: str, timeout: int = 30) -> bool:
    """Redemarre un container (preserve les volumes et la config)."""
    client = _client()
    try:
        c = client.containers.get(_resolve_container_name(name))
        c.restart(timeout=timeout)
        return True
    except _docker().errors.NotFound:
        return False
