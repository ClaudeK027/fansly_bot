# src/fansly_bot/worker.py
"""Worker : boucle de consommation de la file `job_queue`.

Le worker ne fait RIEN tant que la queue est vide. Quand un job apparait :
  - publish → execute publish_next() en boucle jusqu'a epuisement du lot
  - purge   → execute purger.run() avec les criteres du job

Un seul job a la fois (verrou implicite via le seul navigateur).
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import signal
import sys
import time
from contextlib import suppress
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

import structlog
from playwright.async_api import TimeoutError as PWTimeout

from .browser.humanizer import Humanizer
from .browser.session import BrowserSession
from .config import Settings, load_settings
from .infra.names import InvalidBatchNameError, validate_batch_name
from .infra.retry import RetryPolicies
from .infra.state import Job, StateStore
from .logging_setup import setup_logging
from .services.auth import AuthError, AuthService
from .services.caption_picker import CaptionPicker
from .services.cycle_rotator import CycleRotator
from .services.purger import PurgerService
from .services.uploader import UploaderService

log = structlog.get_logger("worker")

POLL_INTERVAL_S = 2.0


class CancelledByUserError(RuntimeError):
    """Levee quand un job est annule par l'utilisateur via la BDD."""


# ---------- PID file + verrou exclusif ----------

# Verrou de fichier maintenu pour toute la duree de vie du worker.
# fcntl.flock libere automatiquement a la fermeture du fd (ou a la mort
# du process). On garde le fd au niveau module pour qu'il ne soit pas
# garbage-collected tant que le worker tourne.
_LOCK_FD: Optional[int] = None


class WorkerAlreadyRunningError(RuntimeError):
    """Levee quand un autre worker tient deja le verrou."""


def pid_file_path(settings: Settings) -> Path:
    return settings.paths.state_db.parent / "worker.pid"


def lock_file_path(settings: Settings) -> Path:
    """Fichier dedie au verrou OS (separe du pid file pour eviter les courses
    d'ecriture/lecture entre flock et write_text)."""
    return settings.paths.state_db.parent / "worker.pid.lock"


def started_file_path(settings: Settings) -> Path:
    return settings.paths.state_db.parent / "worker.started_at"


def state_file_path(settings: Settings) -> Path:
    """Fichier de statut du worker : contient 'running' ou 'stopping'.
    Permet a l UI d afficher 'Arret en cours' pendant la fin gracieuse."""
    return settings.paths.state_db.parent / "worker.state"


def write_worker_state(settings: Settings, state: str) -> None:
    with suppress(OSError):
        state_file_path(settings).write_text(state)


def remove_worker_state(settings: Settings) -> None:
    with suppress(FileNotFoundError):
        state_file_path(settings).unlink()


def acquire_worker_lock(settings: Settings) -> None:
    """Acquiert un verrou exclusif non-bloquant sur le fichier de lock.

    Si un autre processus tient deja le lock, leve WorkerAlreadyRunningError.
    Sinon, le lock est conserve pour toute la duree de vie du processus
    (libere automatiquement a la sortie via fermeture du fd ou kill).
    """
    global _LOCK_FD
    lock_path = lock_file_path(settings)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError) as e:
        os.close(fd)
        raise WorkerAlreadyRunningError(
            f"Un autre worker detient deja le verrou {lock_path}"
        ) from e
    # Conserve le fd au niveau module — il ne doit pas etre ferme tant que
    # le worker tourne, sinon fcntl.flock libererait le verrou.
    _LOCK_FD = fd


def release_worker_lock() -> None:
    global _LOCK_FD
    if _LOCK_FD is not None:
        with suppress(OSError):
            fcntl.flock(_LOCK_FD, fcntl.LOCK_UN)
            os.close(_LOCK_FD)
        _LOCK_FD = None


def write_pid(settings: Settings) -> None:
    pid_file_path(settings).write_text(str(os.getpid()))
    started_file_path(settings).write_text(
        datetime.now(timezone.utc).isoformat()
    )


def remove_pid(settings: Settings) -> None:
    for p in (pid_file_path(settings), started_file_path(settings)):
        with suppress(FileNotFoundError):
            p.unlink()


def read_pid(settings: Settings) -> Optional[int]:
    p = pid_file_path(settings)
    if not p.is_file():
        return None
    try:
        return int(p.read_text().strip())
    except (ValueError, OSError):
        return None


def is_worker_alive(settings: Settings) -> bool:
    pid = read_pid(settings)
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


# ---------- Worker ----------

class Worker:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._state = StateStore(settings)
        self._stop_event = asyncio.Event()
        self._current_job_id: Optional[int] = None
        self._jobs_dir = settings.paths.logs_dir / "jobs"
        self._jobs_dir.mkdir(parents=True, exist_ok=True)

    def _boot_recover(self) -> None:
        # ─── MACHINE A ETATS DE REPRISE AU BOOT (crash-resume) ───
        # Remplace l'ancien "recover_stale_jobs + cleanup_orphan" (qui JETAIT
        # l'etat) par une vraie reprise. Ordre STRICT (DB-only, aucun
        # navigateur — testable sans Docker) :
        #
        # 1) reconcile_publish_in_flight() : convertit chaque publication en
        #    cours orpheline en 'publie' (jamais en 're-publier'). Ferme la
        #    fenetre du doublon reel (post cree mais pas committe). Chaque
        #    orpheline en transaction isolee (un orphelin pourri ne bloque pas
        #    le boot).
        self._state.reconcile_publish_in_flight()
        # 2) reconcile_stale_jobs() : publish 'running' -> REQUEUE (repris,
        #    position intacte) ; publish 'cancelling' -> cancelled ; purge ->
        #    failed. Remplace le blanket running->failed.
        self._state.reconcile_stale_jobs()
        # 3) sweep_stale_cancelling() : tout 'cancelling' orphelin -> cancelled
        #    (ferme la fenetre TOCTOU d'un cancel UI concurrent au boot).
        self._state.sweep_stale_cancelling()
        # 4) cleanup_orphan_active_batch() : supprime active_batch UNIQUEMENT
        #    s'il n'a aucun job publish queued/running. Comme l'etape 2 a
        #    requeue le job publish interrompu (=> queued), l'active_batch A
        #    REPRENDRE est CONSERVE ; seul un batch reellement orphelin (aucun
        #    driver) est nettoye.
        orphan_batch = self._state.cleanup_orphan_active_batch()
        if orphan_batch:
            log.warning(
                "worker_cleaned_orphan_active_batch",
                batch=orphan_batch,
                rationale="aucun_job_publish_actif_au_demarrage",
            )

    async def run_forever(self) -> int:
        write_pid(self._settings)
        write_worker_state(self._settings, "running")
        log.info("worker_started", pid=os.getpid())
        self._boot_recover()
        try:
            while not self._stop_event.is_set():
                try:
                    job = self._state.get_next_queued_job()
                except Exception as e:  # noqa: BLE001
                    log.error("worker_queue_read_error", error=str(e))
                    await asyncio.sleep(POLL_INTERVAL_S * 5)
                    continue

                if job is None:
                    await asyncio.sleep(POLL_INTERVAL_S)
                    continue

                await self._execute(job)
            log.info("worker_stopping_gracefully")
            return 0
        finally:
            remove_worker_state(self._settings)
            remove_pid(self._settings)
            self._state.close()
            log.info("worker_stopped")

    def request_stop(self) -> None:
        # Signale aux clients (UI) qu un arret gracieux est en cours.
        # Le fichier sera supprime dans le finally de run_forever.
        write_worker_state(self._settings, "stopping")
        # Propage l annulation au job en cours : sans ca, le purger ou
        # l uploader ne consulte que `is_cancellation_requested(job_id)` et
        # ne voit jamais qu un arret a ete demande. En marquant le job
        # `cancelling`, le service (purger / uploader) sortira proprement
        # a son prochain point de controle (entre 2 scrolls, 2 suppressions,
        # ou entre 2 publications) sans avoir besoin d acceder au stop_event.
        if self._current_job_id is not None:
            try:
                self._state.request_job_cancel(self._current_job_id)
                log.info(
                    "worker_propagated_cancel_to_current_job",
                    job_id=self._current_job_id,
                )
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "worker_propagate_cancel_failed",
                    job_id=self._current_job_id,
                    error=str(e),
                )
        log.info("worker_stop_requested", graceful=True)
        self._stop_event.set()


    # ---------- execution ----------

    async def _execute(self, job: Job) -> None:
        self._current_job_id = job.id

        # Logger dedie au job (ajoute un handler au logger stdlib racine)
        log_path = self._jobs_dir / f"job_{job.id:05d}.jsonl"
        job_handler = RotatingFileHandler(
            log_path, maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8"
        )
        # Reutilise le formateur du handler principal (deja configure)
        for h in logging.getLogger().handlers:
            if isinstance(h, RotatingFileHandler) and h is not job_handler:
                job_handler.setFormatter(h.formatter)
                break
        logging.getLogger().addHandler(job_handler)

        self._state.mark_job_running(job.id, str(log_path))
        log.info("job_started", job_id=job.id, type=job.type, config=job.config)
        start = time.time()
        try:
            if job.type == "publish":
                await self._do_publish(job)
            elif job.type == "purge":
                await self._do_purge(job)
            else:
                raise ValueError(f"Type de job inconnu : {job.type}")
            self._state.mark_job_done(job.id)
            log.info("job_done", job_id=job.id, duration_s=round(time.time() - start, 1))
        except CancelledByUserError:
            self._state.mark_job_cancelled(job.id)
            log.info("job_cancelled", job_id=job.id)
        except Exception as e:  # noqa: BLE001
            self._state.mark_job_failed(job.id, str(e))
            log.error("job_failed", job_id=job.id, error=str(e), exc_info=True)
        finally:
            self._current_job_id = None
            logging.getLogger().removeHandler(job_handler)
            with suppress(Exception):
                job_handler.close()

    # ---------- type-specific runners ----------

    async def _do_publish(self, job: Job) -> None:
        cfg = job.config
        # Champs attendus : batch_name, max_cycles, interval_median_minutes,
        # interval_min, interval_max, interval_sigma
        # Defense en profondeur : on revalide le nom de lot meme s il vient
        # de la BDD (un job edite manuellement ou un futur import externe
        # pourrait contenir un nom non valide).
        try:
            batch_name = validate_batch_name(cfg["batch_name"])
        except (InvalidBatchNameError, KeyError, TypeError) as e:
            raise RuntimeError(f"Job {job.id} : batch_name invalide ({e})") from e
        max_cycles = int(cfg.get("max_cycles", 0))

        # Active OU REPREND le lot (crash-resume). Si un active_batch de meme
        # nom existe deja (job requeue apres crash), on RESUME sans reset :
        # current_cycle, published_in_cycle, playlist_order et le run_id STABLE
        # sont preserves -> la publication reprend exactement ou elle s'etait
        # arretee. Sinon demarrage frais avec job.id comme run_id d'ancrage.
        # Le run_id retourne (stable) est passe a l'uploader ET (via lui) au
        # rotator, garantissant l'idempotence de media_published/publish_in_flight
        # a travers les re-queues.
        run_id = self._state.start_or_resume_batch(batch_name, max_cycles, job.id)
        log.info(
            "publish_batch_activated",
            batch=batch_name, max_cycles=max_cycles,
            run_id=run_id, resumed=(run_id != job.id),
        )

        # Settings avec override des intervalles de publication
        custom_settings = self._settings.model_copy(deep=True)
        publishing = custom_settings.publishing.model_copy(
            update={
                "interval_minutes_median": float(cfg["interval_median_minutes"]),
                "interval_minutes_sigma": float(cfg.get("interval_sigma", 0.5)),
                "interval_minutes_min": float(cfg["interval_min"]),
                "interval_minutes_max": float(cfg["interval_max"]),
            }
        )
        custom_settings = custom_settings.model_copy(update={"publishing": publishing})

        humanizer = Humanizer(custom_settings)
        humanizer.bind_publishing(custom_settings.publishing)
        session = BrowserSession(custom_settings)
        captions = CaptionPicker(custom_settings)
        retries = RetryPolicies(custom_settings)
        auth = AuthService(custom_settings, session, humanizer)
        captions_batch_name = cfg.get("captions_batch_name") or None
        # Defense en profondeur : revalide aussi le nom du lot de legendes.
        if captions_batch_name is not None:
            try:
                captions_batch_name = validate_batch_name(captions_batch_name)
            except InvalidBatchNameError as e:
                raise RuntimeError(
                    f"Job {job.id} : captions_batch_name invalide ({e})"
                ) from e
        # CycleRotator : seul mode de nettoyage. Avant chaque publication
        # du cycle N+1, on supprime la version precedente du meme media
        # via le permalien direct Fansly. Plus de cleanup batch en fin de
        # cycle — la rotation se fait au cas par cas avant chaque republi.
        rotator = CycleRotator(
            custom_settings, session, humanizer, auth, self._state, retries,
        )
        uploader = UploaderService(
            custom_settings, session, humanizer, auth, captions, self._state, retries,
            captions_batch_name=captions_batch_name,
            run_id=run_id,  # run_id STABLE du lot (pas job.id volatil) -> idempotence
            cycle_rotator=rotator,
            cancel_check=lambda: self._state.is_cancellation_requested(job.id),
        )
        if captions_batch_name:
            log.info("publish_captions_batch", name=captions_batch_name)
        else:
            log.info("publish_captions_legacy_mode")

        await session.start()
        try:
            try:
                await auth.ensure_logged_in()
            except AuthError as e:
                raise RuntimeError(f"Auth invalide : {e}") from e

            iteration = 0
            while True:
                self._raise_if_cancelled(job.id)
                iteration += 1
                # Log de borne d'iteration : sert a localiser un hang dans
                # la boucle (avant ce fix, un hang entre publish_waiting_next
                # et l'iteration suivante etait quasi-invisible).
                log.info("publish_iter_begin", job_id=job.id, iteration=iteration)

                # 1) ETAPE BDD-ONLY : decider si on avance le cycle / on stoppe
                batch_before = self._state.get_active_batch()
                if batch_before is None:
                    log.info("publish_batch_finished", job_id=job.id)
                    break
                transition = uploader.advance_cycle_if_needed()
                if transition == "no_batch" or transition == "stopped":
                    log.info("publish_batch_finished", job_id=job.id)
                    break

                # 2) Publication d'un media du cycle courant
                # FAILSAFE ULTIME : wrapper publish_next() dans un asyncio.wait_for
                # global (10 min cap). Sans ca, un hang sur un appel Playwright
                # non-borne (CDP gele, renderer zombie) pouvait bloquer le
                # worker indefiniment — observe sur job 49 (2h en epoll_wait).
                # Sur timeout : recycle la session (stop+start) pour repartir
                # sur un Chromium frais, puis on continue la boucle.
                try:
                    published = await asyncio.wait_for(
                        uploader.publish_next(), timeout=600.0,
                    )
                except asyncio.TimeoutError:
                    log.error(
                        "publish_next_failsafe_timeout",
                        job_id=job.id, iteration=iteration,
                        hint="publish_next hung >10min ; recycling session",
                    )
                    # Recycle preventif de la session pour repartir propre
                    with suppress(Exception):
                        await session.stop()
                    with suppress(Exception):
                        await session.start()
                    await self._sleep_cancellable(30, job.id)
                    continue

                # Si stop_batch() a ete declenche par publish_next (max_cycles)
                if self._state.get_active_batch() is None:
                    log.info("publish_batch_finished", job_id=job.id)
                    break

                if published is None:
                    # Echec ou rien a publier : on s'accorde une pause de 60s puis on continue
                    log.warning("publish_iteration_returned_none", job_id=job.id)
                    await self._sleep_cancellable(60, job.id)
                    continue

                # Pause humaine avant la prochaine publication
                delay = humanizer.next_publication_delay_seconds()
                log.info(
                    "publish_waiting_next",
                    job_id=job.id,
                    delay_seconds=round(delay, 1),
                )
                await self._sleep_cancellable(delay, job.id)
        finally:
            # On laisse active_batch tel quel (le lot peut etre en cours de cycles)
            # mais on ferme le navigateur.
            with suppress(Exception):
                await session.stop()

    async def _do_purge(self, job: Job) -> None:
        cfg = job.config
        # Champs attendus : keywords, age_threshold_days, keyword_match_mode,
        # dry_run, max_deletions, scroll_cap, profile_path (optional)
        custom_settings = self._settings.model_copy(deep=True)
        purge = custom_settings.purge.model_copy(
            update={
                "keywords": list(cfg["keywords"]),
                "age_threshold_days": int(cfg["age_threshold_days"]),
                "keyword_match_mode": cfg.get("keyword_match_mode", "any"),
                "dry_run": bool(cfg.get("dry_run", True)),
                "max_deletions_per_run": int(cfg.get("max_deletions", 20)),
                "scroll_safety_cap": int(cfg.get("scroll_cap", 500)),
                "profile_path": cfg.get("profile_path", custom_settings.purge.profile_path),
            }
        )
        custom_settings = custom_settings.model_copy(update={"purge": purge})

        humanizer = Humanizer(custom_settings)
        session = BrowserSession(custom_settings)
        retries = RetryPolicies(custom_settings)
        auth = AuthService(custom_settings, session, humanizer)
        purger = PurgerService(
            custom_settings, session, humanizer, auth, self._state, retries
        )

        await session.start()
        try:
            try:
                await auth.ensure_logged_in()
            except AuthError as e:
                raise RuntimeError(f"Auth invalide : {e}") from e
            # On passe un callback de cancellation au purger via attribut
            purger._cancel_check = lambda: self._state.is_cancellation_requested(job.id)  # noqa: SLF001

            # Fenetre de dates absolue optionnelle (override de age_threshold_days)
            start_iso = cfg.get("start_date")
            end_iso = cfg.get("end_date")
            if start_iso and end_iso:
                from datetime import datetime as _dt
                from datetime import timezone as _tz
                start = _dt.fromisoformat(start_iso)
                end = _dt.fromisoformat(end_iso)
                if start.tzinfo is None:
                    start = start.replace(tzinfo=_tz.utc)
                if end.tzinfo is None:
                    end = end.replace(tzinfo=_tz.utc)
                purger._date_window = (start, end)  # noqa: SLF001
                log.info("purge_date_window_set", start=start.isoformat(), end=end.isoformat())

            # Mode "IDs stockes" : pour purger des posts PUBLIES PAR LE BOT sur
            # une fenetre de dates, on supprime par permalien (fiable) au lieu
            # de scroller le feed profil (fragile pour la suppression : le DOM
            # se reorganise apres chaque delete, les items suivants deviennent
            # introuvables). Necessite une fenetre de dates (start/end).
            use_stored = bool(cfg.get("use_stored_ids", False))
            if use_stored and start_iso and end_iso:
                targets = self._state.get_published_post_ids_in_window(start_iso, end_iso)
                log.info(
                    "purge_stored_targets",
                    count=len(targets),
                    window=f"{start_iso}..{end_iso}",
                )
                await purger.purge_stored_ids(
                    targets,
                    dry_run=bool(cfg.get("dry_run", True)),
                    max_deletions=int(cfg.get("max_deletions", 1000)),
                )
            else:
                await purger.run()
        finally:
            with suppress(Exception):
                await session.stop()

    # ---------- helpers ----------

    def _raise_if_cancelled(self, job_id: int) -> None:
        if self._state.is_cancellation_requested(job_id):
            raise CancelledByUserError()

    async def _sleep_cancellable(self, seconds: float, job_id: int) -> None:
        """Dort N secondes en verifiant l'annulation toutes les 2 secondes."""
        end = time.monotonic() + seconds
        while time.monotonic() < end and not self._stop_event.is_set():
            self._raise_if_cancelled(job_id)
            remaining = end - time.monotonic()
            await asyncio.sleep(min(2.0, remaining))


# ---------- entry point ----------

async def amain() -> int:
    settings = load_settings()
    settings.ensure_runtime_dirs()
    setup_logging(settings)

    # Verrou OS exclusif : empeche deux workers de tourner en parallele meme
    # si la verif PID file echoue (le fichier peut subsister apres un crash).
    # Sur Mac/Linux, fcntl.flock libere automatiquement a la mort du process.
    try:
        acquire_worker_lock(settings)
    except WorkerAlreadyRunningError as e:
        log.error(
            "worker_already_running",
            pid=read_pid(settings),
            lock=str(lock_file_path(settings)),
            error=str(e),
        )
        return 3

    try:
        worker = Worker(settings)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with suppress(NotImplementedError):
                loop.add_signal_handler(sig, worker.request_stop)
        return await worker.run_forever()
    finally:
        release_worker_lock()


def main() -> None:
    rc = asyncio.run(amain())
    sys.exit(rc)
