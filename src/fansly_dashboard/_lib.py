# src/fansly_dashboard/_lib.py
"""Fonctions partagees entre les pages Streamlit.

Pattern : charger une seule fois Settings + StateStore par session Streamlit
via st.cache_resource pour ne pas recreer les connexions a chaque rerun.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import streamlit as st

from fansly_bot.config import Settings, load_settings
from fansly_bot.infra.names import (
    InvalidBatchNameError,
    is_valid_batch_name,
    validate_batch_name,
)
from fansly_bot.infra.state import StateStore


# ---------- chargement settings + state ----------

@st.cache_resource
def get_settings() -> Settings:
    settings = load_settings()
    settings.ensure_runtime_dirs()
    return settings


@st.cache_resource
def get_state() -> StateStore:
    return StateStore(get_settings())


# ---------- helpers chemins ----------

def project_root() -> Path:
    """Racine du projet (4 niveaux au-dessus de ce fichier).
    src/fansly_dashboard/_lib.py  →  remonter a project root."""
    return Path(__file__).resolve().parents[2]


def config_path() -> Path:
    env_path = os.environ.get("FANSLY_CONFIG_FILE")
    if env_path:
        return Path(env_path).expanduser().resolve()
    return project_root() / "config.yaml"


# ---------- gestion lots ----------

def list_batches() -> list[tuple[str, int]]:
    """Renvoie [(nom_lot, nb_medias)] des sous-dossiers de Medias/."""
    settings = get_settings()
    folder = settings.paths.media_folder
    if not folder.is_dir():
        return []
    exts = set(settings.publishing.media_extensions)
    out = []
    for p in sorted(folder.iterdir()):
        if not p.is_dir() or p.name.startswith("_"):
            continue
        n = sum(1 for f in p.iterdir() if f.is_file() and f.suffix.lower() in exts)
        out.append((p.name, n))
    return out


def list_media_in_batch(batch_name: str) -> list[Path]:
    validate_batch_name(batch_name)
    settings = get_settings()
    folder = settings.paths.media_folder / batch_name
    if not folder.is_dir():
        return []
    exts = set(settings.publishing.media_extensions)
    return sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in exts
    )


def create_batch(name: str) -> None:
    name = validate_batch_name(name)
    folder = get_settings().paths.media_folder / name
    folder.mkdir(parents=True, exist_ok=True)


def delete_batch(name: str) -> None:
    name = validate_batch_name(name)
    media_folder = get_settings().paths.media_folder
    folder = media_folder / name
    # Defense en profondeur : verifie que le chemin resolu est bien sous le
    # dossier des medias (empeche un symlink ou une path-traversal residuelle).
    resolved = folder.resolve()
    media_resolved = media_folder.resolve()
    if not str(resolved).startswith(str(media_resolved) + os.sep) and resolved != media_resolved:
        raise InvalidBatchNameError(
            f"Refus de supprimer un dossier hors de {media_resolved} : {resolved}"
        )
    if folder.is_dir():
        shutil.rmtree(folder)


def save_uploaded_file(batch_name: str, filename: str, data: bytes) -> Path:
    batch_name = validate_batch_name(batch_name)
    # Filtre le filename : on refuse explicitement tout filename qui
    # contient des sequences suspectes AVANT toute resolution. Path(x).name
    # neutralise '../evil.jpg' en 'evil.jpg', donc on ne peut pas detecter le
    # traversal apres coup — on verifie sur la valeur brute.
    if (
        not filename
        or filename in (".", "..")
        or ".." in filename
        or "/" in filename
        or "\\" in filename
        or "\x00" in filename
        or filename.startswith(".")
    ):
        raise ValueError(f"Nom de fichier invalide ou suspect : {filename!r}")
    folder = get_settings().paths.media_folder / batch_name
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / filename
    # Verifie que dest reste sous folder apres resolution (anti-symlink)
    if not str(dest.resolve()).startswith(str(folder.resolve()) + os.sep):
        raise ValueError(f"Refus d ecrire hors du dossier du lot : {dest}")
    dest.write_bytes(data)
    return dest


# ---------- gestion legendes (LEGACY .txt — conserve pour compat) ----------

def list_captions() -> list[Path]:
    folder = get_settings().paths.caption_folder
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".txt")


def read_caption(name: str) -> str:
    return (get_settings().paths.caption_folder / name).read_text(encoding="utf-8")


def write_caption(name: str, content: str) -> None:
    folder = get_settings().paths.caption_folder
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(content, encoding="utf-8")


def delete_caption(name: str) -> None:
    p = get_settings().paths.caption_folder / name
    if p.exists():
        p.unlink()


# ---------- gestion lots de legendes (JSON) ----------

_CAPTION_SEPARATOR = "---"  # ligne seule entre 2 captions dans l'editeur


def _caption_batch_path(name: str):
    # Validation centralisee : impossible de construire un path de caption
    # batch sans passer par la verif du nom. C est la defense unique pour
    # toutes les fonctions read/write/delete qui appellent ce helper.
    name = validate_batch_name(name)
    return get_settings().paths.caption_folder / f"{name}.json"


def list_caption_batches() -> list[dict]:
    """Liste tous les lots de légendes (fichiers .json) avec leurs métadonnées."""
    import json
    folder = get_settings().paths.caption_folder
    if not folder.is_dir():
        return []
    out = []
    for p in sorted(folder.iterdir()):
        if not (p.is_file() and p.suffix.lower() == ".json"):
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            captions = data.get("captions", [])
            out.append({
                "name": data.get("name", p.stem),
                "description": data.get("description", ""),
                "created_at": data.get("created_at", ""),
                "size": len(captions) if isinstance(captions, list) else 0,
                "path": str(p),
            })
        except Exception:  # noqa: BLE001
            out.append({"name": p.stem, "description": "(erreur lecture)",
                        "created_at": "", "size": 0, "path": str(p)})
    return out


def read_caption_batch(name: str) -> dict | None:
    import json
    p = _caption_batch_path(name)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def write_caption_batch(
    name: str,
    captions: list[str],
    description: str = "",
) -> None:
    """Cree ou ecrase un lot. `captions` est une liste de chaines (1 par legende).

    Ecriture ATOMIQUE : on ecrit dans un fichier temporaire puis on rename,
    pour eviter que le worker ne lise un JSON partiel pendant l'ecriture.
    """
    import json
    import os
    from datetime import datetime, timezone
    name = validate_batch_name(name)
    folder = get_settings().paths.caption_folder
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / f"{name}.json"
    # Conserve created_at si le fichier existait deja
    created_at = datetime.now(timezone.utc).isoformat()
    if p.is_file():
        try:
            old = json.loads(p.read_text(encoding="utf-8"))
            if "created_at" in old:
                created_at = old["created_at"]
        except Exception:  # noqa: BLE001
            pass
    payload = {
        "name": name,
        "description": description,
        "created_at": created_at,
        "captions": [c for c in captions if c and c.strip()],
    }
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    # Ecriture atomique : tmp file + os.replace (atomic sur POSIX & Windows)
    tmp = folder / f".{name}.json.tmp"
    tmp.write_text(serialized, encoding="utf-8")
    os.replace(tmp, p)


def delete_caption_batch(name: str) -> None:
    p = _caption_batch_path(name)
    if p.exists():
        p.unlink()


def parse_captions_text(text: str) -> list[str]:
    """Decoupe une textarea en captions individuelles via la ligne separatrice `---`."""
    import re
    if not text:
        return []
    # Split sur lignes contenant uniquement ---
    parts = re.split(r"^\s*---\s*$", text, flags=re.MULTILINE)
    return [p.strip() for p in parts if p and p.strip()]


def serialize_captions_text(captions: list[str]) -> str:
    """Combine une liste de captions en un texte editable avec separateurs."""
    return f"\n{_CAPTION_SEPARATOR}\n".join(c.strip() for c in captions if c and c.strip())


def run_bot_subprocess(args: list[str], detached: bool = False) -> subprocess.Popen:
    """Lance `python -m fansly_bot <args>`. Si detached=True, le subprocess
    survit a la fermeture de Streamlit."""
    env = os.environ.copy()
    kwargs: dict[str, Any] = dict(
        cwd=str(project_root()),
        env=env,
        text=True,
    )
    if detached:
        # Detache de Streamlit, ne meurt pas si Streamlit est arrete
        kwargs["start_new_session"] = True
        stdout_path = get_settings().paths.logs_dir / "worker.stdout"
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        kwargs["stdout"] = open(stdout_path, "ab")
        kwargs["stderr"] = subprocess.STDOUT
        kwargs["stdin"] = subprocess.DEVNULL
    else:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.STDOUT
    return subprocess.Popen(["python", "-m", "fansly_bot", *args], **kwargs)


# ---------- worker (PID-based) ----------

def _worker_pid_file() -> Path:
    return get_settings().paths.state_db.parent / "worker.pid"


_WORKER_RUNTIME_FILES = (
    "worker.pid",
    "worker.state",
    "worker.started_at",
    "worker.pid.lock",
)


def _cleanup_worker_files() -> None:
    """Supprime les fichiers runtime du worker (pid, state, started_at, lock).

    Appele apres un SIGKILL (le finally du worker n a pas pu tourner) ou avant
    un nouveau start (au cas ou un kill precedent aurait laisse des fichiers).
    """
    from contextlib import suppress as _suppress

    base = get_settings().paths.state_db.parent
    for name in _WORKER_RUNTIME_FILES:
        with _suppress(FileNotFoundError, OSError):
            (base / name).unlink()


def _is_process_zombie(pid: int) -> bool:
    """Vrai si le PID pointe vers un processus zombie (defunct).

    `os.kill(pid, 0)` retourne SUCCES sur un zombie (le process existe encore
    en table tant qu il n a pas ete reape par son parent), ce qui fait croire
    a tort que le worker tourne. On verifie l etat via `ps -o stat`.
    """
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode != 0:
            return False
        return result.stdout.strip().startswith("Z")
    except (subprocess.TimeoutExpired, OSError):
        return False


def worker_is_alive() -> bool:
    p = _worker_pid_file()
    if not p.is_file():
        return False
    try:
        pid = int(p.read_text().strip())
    except (ValueError, OSError):
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    # Le process existe en table — verifie qu il n est pas zombie
    # (defunct). Apres un SIGKILL, le child reste en zombie jusqu a ce que
    # le parent (Streamlit) l ait reape. Pendant cette fenetre, os.kill(0)
    # ne dit pas la verite et le bandeau UI affiche faussement "actif".
    if _is_process_zombie(pid):
        return False
    return True


def worker_pid() -> int | None:
    p = _worker_pid_file()
    if not p.is_file():
        return None
    try:
        return int(p.read_text().strip())
    except (ValueError, OSError):
        return None


def worker_started_at():
    """Renvoie le datetime UTC de demarrage du worker, ou None si inconnu."""
    from datetime import datetime as _dt
    p = get_settings().paths.state_db.parent / "worker.started_at"
    if not p.is_file():
        return None
    try:
        return _dt.fromisoformat(p.read_text().strip())
    except (ValueError, OSError):
        return None


def worker_is_stopping() -> bool:
    """Vrai si le worker a recu une demande d arret gracieux mais tourne encore.
    Indique a l UI : 'finit la tache en cours puis s arretera'."""
    p = get_settings().paths.state_db.parent / "worker.state"
    if not p.is_file():
        return False
    try:
        return p.read_text().strip() == "stopping"
    except OSError:
        return False


def latest_source_mtime():
    """Renvoie le datetime UTC du fichier .py le plus recent dans src/fansly_bot/.
    Utilise pour detecter si le code a ete modifie apres le demarrage du worker."""
    from datetime import datetime as _dt, timezone as _tz
    src_dir = project_root() / "src" / "fansly_bot"
    if not src_dir.is_dir():
        return None
    latest = 0.0
    for p in src_dir.rglob("*.py"):
        try:
            m = p.stat().st_mtime
            if m > latest:
                latest = m
        except OSError:
            continue
    if latest == 0:
        return None
    return _dt.fromtimestamp(latest, tz=_tz.utc)


def worker_is_stale() -> bool:
    """Vrai si du code source est plus recent que le demarrage du worker.
    Signale a l'UI qu'un redemarrage du worker est necessaire pour appliquer
    les modifs de code."""
    started = worker_started_at()
    if started is None:
        return False
    src_mtime = latest_source_mtime()
    if src_mtime is None:
        return False
    # Marge de 5 secondes pour eviter les faux positifs sur les ecritures
    # quasi-simultanees au demarrage.
    return (src_mtime - started).total_seconds() > 5


def _mark_running_jobs_failed(reason: str) -> int:
    """Marque les jobs running/cancelling comme failed apres un kill brutal.

    Appele par stop_worker(force=True) car le SIGKILL ne laisse pas tourner
    le finally du worker qui aurait normalement appelle mark_job_failed.
    Retourne le nombre de jobs nettoyes.
    """
    state = get_state()
    from datetime import datetime as _dt, timezone as _tz

    now = _dt.now(_tz.utc).isoformat()
    with state._conn:  # noqa: SLF001
        cur = state._conn.execute(  # noqa: SLF001
            "UPDATE job_queue SET status='failed', finished_at=?, "
            "error=COALESCE(error,'') || ? "
            "WHERE status IN ('running','cancelling')",
            (now, f" [{reason}]"),
        )
    return cur.rowcount


def start_worker() -> tuple[bool, str]:
    """Demarre le worker en subprocess detache. Renvoie (ok, message)."""
    if worker_is_alive():
        return False, "Le worker tourne déjà."
    # Si on arrive ici, le worker n est pas vivant — mais des fichiers
    # residuels (pid, state, started_at, lock) peuvent subsister d un kill
    # brutal precedent. On les nettoie pour permettre un demarrage propre.
    _cleanup_worker_files()
    proc = run_bot_subprocess(["worker"], detached=True)
    # Attente courte que le PID file apparaisse
    import time as _time
    for _ in range(20):
        if worker_is_alive():
            return True, f"Worker démarré (PID {worker_pid()})."
        _time.sleep(0.2)
    # Pas de PID file -> le worker a peut-etre crashe
    return False, "Le worker n'a pas démarré (vérifier les logs)."


def stop_worker(force: bool = False) -> tuple[bool, str]:
    pid = worker_pid()
    if pid is None or not worker_is_alive():
        # Pas vivant mais des fichiers peuvent rester : on nettoie pour
        # remettre l UI dans un etat coherent.
        _cleanup_worker_files()
        return False, "Le worker n'est pas actif."
    try:
        import signal as _sig
        os.kill(pid, _sig.SIGKILL if force else _sig.SIGTERM)
        # Attente courte
        import time as _time
        for _ in range(50):
            if not worker_is_alive():
                if force:
                    # SIGKILL n a pas laisse tourner le finally du worker.
                    # On termine son boulot a sa place : nettoyage fichiers +
                    # marquage des jobs running comme failed. Et on tente
                    # de reaper le zombie si on est le parent (best-effort).
                    _cleanup_worker_files()
                    n = _mark_running_jobs_failed("killed_by_user_via_forcer")
                    msg = "Worker tué (Forcer)."
                    if n:
                        msg += f" {n} job(s) marqué(s) failed."
                    from contextlib import suppress as _suppress
                    with _suppress(OSError, ChildProcessError):
                        os.waitpid(pid, os.WNOHANG)
                    return True, msg
                return True, "Worker arrêté (gracieux)."
            _time.sleep(0.1)
        return False, "Worker n'a pas répondu au signal (essaie 'Forcer')."
    except (ProcessLookupError, PermissionError) as e:
        # Le process etait deja mort entre la verif et le kill — nettoie
        # quand meme les fichiers residuels.
        _cleanup_worker_files()
        return False, f"Impossible de signaler le worker : {e}"


# ---------- queue ----------

def enqueue_publish_job(
    batch_name: str,
    max_cycles: int,
    interval_median_minutes: float,
    interval_min: float,
    interval_max: float,
    interval_sigma: float = 0.5,
    delete_previous_cycle: bool = True,
    captions_batch_name: str | None = None,
) -> int:
    state = get_state()
    config = {
        "batch_name": batch_name,
        "max_cycles": int(max_cycles),
        "interval_median_minutes": float(interval_median_minutes),
        "interval_min": float(interval_min),
        "interval_max": float(interval_max),
        "interval_sigma": float(interval_sigma),
        "delete_previous_cycle": bool(delete_previous_cycle),
    }
    if captions_batch_name:
        config["captions_batch_name"] = captions_batch_name
    return state.enqueue_job("publish", config)


def enqueue_purge_job(
    keywords: list[str],
    age_threshold_days: int = 0,
    keyword_match_mode: str = "any",
    dry_run: bool = True,
    max_deletions: int = 20,
    scroll_cap: int = 500,
    start_date_iso: str | None = None,
    end_date_iso: str | None = None,
) -> int:
    state = get_state()
    config: dict[str, Any] = {
        "keywords": keywords,
        "age_threshold_days": int(age_threshold_days),
        "keyword_match_mode": keyword_match_mode,
        "dry_run": bool(dry_run),
        "max_deletions": int(max_deletions),
        "scroll_cap": int(scroll_cap),
    }
    if start_date_iso and end_date_iso:
        config["start_date"] = start_date_iso
        config["end_date"] = end_date_iso
    return state.enqueue_job("purge", config)


# ---------- confirmation 2-clics reutilisable ----------

def confirm_destructive_action(
    button_label: str,
    confirm_message: str,
    state_key: str,
    button_help: str = "",
    icon: str = ":material/delete:",
    button_kwargs: dict | None = None,
) -> bool:
    """Affiche un bouton qui demande confirmation avant d executer l action.

    Workflow :
    1. Premier clic sur le bouton -> on memorise l intention dans session_state
       et on affiche un avertissement avec 'Oui, confirmer' / 'Annuler'.
    2. Clic sur 'Oui, confirmer' -> retourne True (l appelant peut executer
       l action). L etat est nettoye.
    3. Clic sur 'Annuler' -> nettoie l etat, retourne False.

    Le bouton initial reste affiche meme apres le premier clic ; la confirmation
    apparait dessous. La fonction retourne True UNIQUEMENT au moment ou
    l utilisateur a clique 'Oui, confirmer' (le rerun le re-evalue automatiquement).

    Args:
        button_label: Libelle du bouton declencheur.
        confirm_message: Message d avertissement (peut contenir du markdown).
        state_key: Cle session_state unique pour cette action (ex: 'del_batch_X').
        button_help: Tooltip du bouton declencheur.
        icon: Material icon du bouton.
        button_kwargs: Args additionnels passes a st.button (use_container_width, etc.).

    Returns:
        True si l utilisateur a confirme cette frame. False sinon.
    """
    import streamlit as st

    btn_args: dict[str, Any] = {
        "icon": icon,
        "help": button_help,
        "use_container_width": True,
    }
    if button_kwargs:
        btn_args.update(button_kwargs)

    if st.button(button_label, key=f"{state_key}__trigger", **btn_args):
        st.session_state[state_key] = True
        st.rerun()

    if st.session_state.get(state_key):
        st.warning(confirm_message, icon=":material/warning:")
        cc1, cc2 = st.columns(2)
        confirmed = cc1.button(
            "Oui, confirmer",
            key=f"{state_key}__yes",
            icon=":material/check:",
            type="primary",
            use_container_width=True,
        )
        cancelled = cc2.button(
            "Annuler",
            key=f"{state_key}__no",
            use_container_width=True,
        )
        if confirmed:
            st.session_state.pop(state_key, None)
            return True
        if cancelled:
            st.session_state.pop(state_key, None)
            st.rerun()

    return False


# ---------- bandeau (header) ----------

def render_worker_header() -> None:
    """Bandeau d'etat du worker, a appeler en haut de chaque vue.

    Trois etats possibles :
    - Inactif : affiche 'Worker inactif', bouton 'Lancer' actif.
    - Actif : affiche 'Worker actif (PID, age)', boutons 'Arreter'/'Forcer' actifs.
    - En arret gracieux : affiche 'Arret en cours...', bouton 'Arreter' grise,
      bouton 'Forcer' reste actif pour kill immediat.

    Les messages succes/erreur passent par st.toast (survit au rerun)
    plutot que st.success/error (eclipse par le rerun).
    """
    import streamlit as st
    from datetime import datetime as _dt, timezone as _tz

    alive = worker_is_alive()
    stopping = alive and worker_is_stopping()
    cols = st.columns([3, 1, 1, 1])
    with cols[0]:
        if stopping:
            # Etat transitoire : worker recu SIGTERM et termine sa tache courante
            st.markdown(
                ":material/hourglass_top: **Arrêt en cours** — le worker "
                "termine sa tâche courante puis s'arrêtera."
            )
            st.caption(
                "Si l'attente est trop longue, utilise le bouton **Forcer** "
                "pour interrompre immédiatement."
            )
        elif alive:
            started = worker_started_at()
            started_str = ""
            stale = worker_is_stale()
            if started:
                age_s = (_dt.now(_tz.utc) - started).total_seconds()
                if age_s < 60:
                    age = f"{int(age_s)}s"
                elif age_s < 3600:
                    age = f"{int(age_s / 60)}min"
                else:
                    age = f"{age_s / 3600:.1f}h"
                started_str = (
                    f" — actif depuis {age} (depuis {started.strftime('%H:%M')})"
                )
            st.markdown(
                f":material/play_circle: **Worker actif** (PID {worker_pid()}){started_str}"
            )
            if stale:
                st.warning(
                    "**Le code source a été modifié après le démarrage du worker.** "
                    "Redémarre-le pour appliquer les modifications.",
                    icon=":material/sync_problem:",
                )
        else:
            st.markdown(":material/pause_circle: **Worker inactif**")
    with cols[1]:
        if st.button(
            "Lancer",
            icon=":material/play_arrow:",
            disabled=alive,
            use_container_width=True,
            key="hdr_start_worker",
        ):
            ok, msg = start_worker()
            st.toast(
                msg,
                icon=":material/check_circle:" if ok else ":material/error:",
            )
            st.rerun()
    with cols[2]:
        # Bouton 'Arreter' grise quand un arret est deja en cours
        if st.button(
            "Arrêter",
            icon=":material/stop:",
            disabled=(not alive) or stopping,
            use_container_width=True,
            key="hdr_stop_worker",
            help=(
                "Demande un arrêt gracieux : le worker termine sa tâche "
                "courante (publication, suppression) puis s'arrête."
            ),
        ):
            ok, msg = stop_worker(force=False)
            st.toast(
                msg,
                icon=":material/check_circle:" if ok else ":material/warning:",
            )
            st.rerun()
    with cols[3]:
        if st.button(
            "Forcer",
            icon=":material/cancel:",
            disabled=not alive,
            use_container_width=True,
            key="hdr_kill_worker",
            help=(
                "Tue le worker immédiatement (SIGKILL). À utiliser si l'arrêt "
                "gracieux n'aboutit pas dans un temps raisonnable."
            ),
        ):
            ok, msg = stop_worker(force=True)
            st.toast(
                msg,
                icon=":material/check_circle:" if ok else ":material/error:",
            )
            st.rerun()
    st.divider()


# ---------- lecture logs ----------

def read_last_log_lines(n: int = 30) -> list[dict[str, Any]]:
    log_file = get_settings().paths.logs_dir / "fansly-bot.jsonl"
    if not log_file.is_file():
        return []
    try:
        with log_file.open("rb") as f:
            f.seek(0, io.SEEK_END)
            size = f.tell()
            chunk = min(size, 200_000)
            f.seek(size - chunk)
            raw = f.read().decode("utf-8", errors="ignore")
    except OSError:
        return []
    lines = raw.strip().split("\n")[-n:]
    out: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            out.append({"event": "raw", "raw": line})
    return out
