# src/fansly_bot/services/uploader.py
"""Service d'upload de medias sur Fansly."""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import random
import re
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import structlog
from playwright.async_api import Page, TimeoutError as PWTimeout

from typing import TYPE_CHECKING, Callable

from ..browser.humanizer import Humanizer
from ..browser.session import BrowserSession
from ..config import Settings
from ..infra.retry import RetryPolicies
from ..infra.state import StateStore
from ..selectors import Sel
from .auth import AuthService
from .caption_picker import CaptionPicker

if TYPE_CHECKING:
    from .cycle_rotator import CycleRotator

log = structlog.get_logger("services.uploader")


class UploaderService:
    def __init__(
        self,
        settings: Settings,
        session: BrowserSession,
        humanizer: Humanizer,
        auth: AuthService,
        captions: CaptionPicker,
        state: StateStore,
        retries: RetryPolicies,
        captions_batch_name: str | None = None,
        run_id: int | None = None,
        cycle_rotator: Optional["CycleRotator"] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._settings = settings
        self._session = session
        self._humanizer = humanizer
        self._auth = auth
        self._captions = captions
        self._state = state
        self._retries = retries
        # Nom du lot de legendes a utiliser. Si None : fallback sur les .txt
        # a la racine de data/Captions/.
        self._captions_batch_name = captions_batch_name
        # Identifiant du job publish qui a instancie cet uploader. Sert de
        # discriminant pour media_published et publish_in_flight, ce qui
        # empeche le cleaner inter-cycle de comptabiliser des publications
        # d un run anterieur sur le meme lot.
        self._run_id = run_id
        # Compteur de captures d'ID Fansly ratees consecutives. Sert a
        # detecter un drift de schema API (ex. Fansly renomme "id" en
        # "postId") en escaladant en log.error apres N echecs successifs,
        # plutot que de tourner en silence avec 0% capture.
        self._capture_miss_streak: int = 0
        # Service de rotation per-media. Injecte par le worker, toujours
        # actif : AVANT chaque publication, rotate_before_publish() supprime
        # la version precedente du meme media via le permalien Fansly.
        # Pour les cycles 1 (pas de precedent) ou les medias sans
        # fansly_post_id capture, la rotation degrade en skip propre.
        self._cycle_rotator = cycle_rotator
        # Closure d'annulation propagee par le worker
        # (lambda: self._state.is_cancellation_requested(job.id)). Default
        # = lambda: False si non fournie. Sert au cleaner ET au rotator.
        self._cancel_check: Callable[[], bool] = cancel_check or (lambda: False)
        # Cle de la publication en cours (crash-resume) : {run_id, batch_name,
        # cycle_number, media_filename}. Posee par publish_next avant l'upload,
        # lue par _do_upload_inner (write-ahead + clicked) et le listener de
        # capture (set_in_flight_post_id). None hors publication.
        self._cur_inflight_key: Optional[dict] = None
        # Compteur d'echecs consecutifs PRE-clic par media (fichier corrompu,
        # selecteur casse). Apres N, on quarantaine le media pour que la
        # playlist avance au lieu de boucler indefiniment sur le meme (WS5).
        self._preclick_failures: dict[str, int] = {}

    # ---------- API publique ----------

    def has_pending_work(self) -> bool:
        """Vrai si le bot a quelque chose a publier maintenant OU au cycle suivant.

        Critere :
          - aucun lot actif → False
          - lot actif et au moins 1 media non publie dans le cycle courant → True
          - cycle courant termine mais on peut avancer au cycle suivant → True
          - max_cycles atteint → True (pour permettre l'arret formel via publish_next)
        """
        batch = self._state.get_active_batch()
        if batch is None:
            return False
        # On a un lot actif. Verifier qu'au moins un fichier existe dans son dossier.
        batch_folder = self._settings.paths.media_folder / batch.name
        if not batch_folder.is_dir():
            return False
        exts = set(self._settings.publishing.media_extensions)
        any_file = any(
            p.is_file() and p.suffix.lower() in exts
            for p in batch_folder.iterdir()
        )
        return any_file

    def _list_all_media_files(self, batch_name: str) -> list[Path]:
        """Liste tous les fichiers medias valides du dossier du batch, tries.

        Non-filtre : inclut publies ET pending. Utilise pour initialiser
        ou etendre la playlist ordonnee.
        """
        batch_folder = self._settings.paths.media_folder / batch_name
        if not batch_folder.is_dir():
            log.warning("uploader_batch_folder_missing", batch=batch_name)
            return []
        exts = set(self._settings.publishing.media_extensions)
        return sorted(
            p for p in batch_folder.iterdir()
            if p.is_file() and p.suffix.lower() in exts
        )

    @staticmethod
    def _select_next_media(
        current_playlist: list[str],
        disk_names_sorted: list[str],
        published_in_cycle: list[str],
        rng: "random.Random",
        blocked: "Optional[set[str]]" = None,
    ) -> tuple[Optional[str], list[str], str]:
        """Logique pure de selection du prochain media a publier.

        Extrait comme methode statique pour etre testable en isolation
        (aucune dependance a la BDD, au filesystem ou au network).

        Parametres :
          current_playlist   : la playlist actuellement stockee (peut etre [])
          disk_names_sorted  : les noms de fichiers presents sur disque, tries
          published_in_cycle : les fichiers deja publies dans le cycle courant
          rng                : instance random.Random pour shuffle reproductible

        Retour : (media_name, new_playlist, event_reason)
          media_name    : le fichier a publier (None si playlist epuisee)
          new_playlist  : la playlist mise a jour (a persister si != current)
          event_reason  : "init" | "extend" | "unchanged" | "exhausted"

        Regles :
          - Si current_playlist vide : shuffle tous les noms disque -> nouvelle playlist
          - Sinon : append en fin (ordre alphabetique stable) les noms
            disque absents de la playlist (A1)
          - Pick le premier nom de la playlist qui est absent de
            published_in_cycle ET present sur disque (B1 : fichiers absents
            sautes silencieusement).
          - `blocked` : medias EXCLUS de la republication (zombies id-NULL du
            run) — anti-doublon, jamais reselectionnes (cf.
            StateStore.get_unconfirmed_media).
        """
        disk_set = set(disk_names_sorted)
        published_set = set(published_in_cycle)
        blocked_set = blocked or set()

        # 1) Initialisation si vide
        if not current_playlist:
            new_playlist = list(disk_names_sorted)
            rng.shuffle(new_playlist)
            event = "init"
        else:
            new_playlist = list(current_playlist)
            event = "unchanged"

        # 2) A1 : appender nouveaux fichiers en ordre stable
        playlist_set = set(new_playlist)
        new_files = [n for n in disk_names_sorted if n not in playlist_set]
        if new_files:
            new_playlist = new_playlist + new_files
            event = "extend" if event == "unchanged" else event

        # 3) Pick : premier de la playlist qui est encore pending (pas publie) ET
        #    encore present sur disque (B1 : sinon on skip) ET non bloque
        #    (zombie id-NULL : anti-doublon, jamais republie).
        for name in new_playlist:
            if name in disk_set and name not in published_set and name not in blocked_set:
                return name, new_playlist, event

        # 4) Playlist epuisee : tout est publie OU tout a disparu du disque
        return None, new_playlist, "exhausted"

    @staticmethod
    def _should_emit_post_click(existing_in_flight: "Optional[dict]") -> bool:
        """Decision anti-double-POST DURABLE : faut-il emettre le clic Post ?

        Extraite en fonction pure pour etre testee en isolation — c'est LE
        garde-fou du doublon historique (une inversion ici = doublon reel sans
        crash), donc il doit etre verrouille par des tests.

        Entree : le resultat de StateStore.get_in_flight(**cle) pour le media
        courant (None si aucune ligne).
        Sortie :
          - False si une ligne existe avec clicked=1 => un clic Post a DEJA ete
            emis pour ce media (retry tenacity anterieur ou relance
            failsafe-timeout in-session) => NE PAS recliquer (le post a pu etre
            cree ; la reconciliation resoudra l'in_flight).
          - True sinon (aucune ligne, ou clicked=0) => cliquer (avec write-ahead).
        """
        return not (existing_in_flight and existing_in_flight.get("clicked"))

    def _published_this_cycle(self, batch) -> set[str]:
        """Medias deja publies dans le CYCLE COURANT, tous runs confondus.

        Union de :
          - batch.published_in_cycle : run courant (inclut aussi les
            quarantaines add_to_batch_published qui n'ecrivent PAS media_published) ;
          - media_published pour (batch, cycle_courant) : couvre les
            publications d'un run ANTERIEUR du meme lot (ex. apres un
            cancel->restart : le nouveau run repart avec published_in_cycle vide)
            -> sans ca, ces medias seraient republies = doublon in-cycle non
            rotable.
        """
        published = set(batch.published_in_cycle)
        try:
            published |= self._state.get_published_media_in_cycle(
                batch.name, batch.current_cycle
            )
        except Exception as e:  # noqa: BLE001 — ne jamais bloquer la publication
            log.warning("published_in_cycle_lookup_failed", batch=batch.name, error=str(e))
        return published

    def _blocked_media(self) -> set[str]:
        """Medias exclus de la (re)publication : zombies id-NULL du run courant.

        Anti-doublon (cf. StateStore.get_unconfirmed_media) : un media dont un
        cycle precedent a ete marque publie SANS id capture ne doit JAMAIS etre
        republie — le post precedent (peut-etre cree) n'est pas rotable, donc le
        republier creerait un doublon permanent. On le fige jusqu'a resolution
        manuelle (WS8)."""
        if self._run_id is None:
            return set()
        try:
            return self._state.get_unconfirmed_media(self._run_id)
        except Exception as e:  # noqa: BLE001 — jamais bloquer la publication sur cette lecture
            # Mais NE PAS masquer une panne durable : sans ce log, un echec
            # persistant de la lecture reactiverait silencieusement la
            # republication des zombies (= reouverture du trou #3).
            log.warning("blocked_media_lookup_failed", run_id=self._run_id, error=str(e))
            return set()

    def list_pending_media(self) -> list[Path]:
        """Liste les medias du LOT ACTIF non encore publies dans le cycle courant.

        S'il n'y a pas de lot actif, renvoie une liste vide (mode rotation
        uniquement, plus de mode one-shot a la racine). Exclut aussi les zombies
        id-NULL du run (anti-doublon, cf. _blocked_media).
        """
        batch = self._state.get_active_batch()
        if batch is None:
            return []
        all_files = self._list_all_media_files(batch.name)
        published = self._published_this_cycle(batch)  # cross-run (cancel->restart)
        blocked = self._blocked_media()
        return [
            p for p in all_files
            if p.name not in published and p.name not in blocked
        ]

    def advance_cycle_if_needed(self) -> str:
        """Verifie si on doit avancer le cycle ou arreter le lot.

        Synchrone (operations BDD pures). A appeler par le worker AVANT
        publish_next() — cela permet d'intercaler un nettoyage entre la fin
        d'un cycle et le debut du suivant sans race condition.

        Renvoie :
            "continue" : aucun changement (cycle en cours)
            "advanced" : cycle avance (cycle precedent etait complet)
            "stopped"  : lot arrete (max_cycles atteint)
            "no_batch" : pas de lot actif
        """
        batch = self._state.get_active_batch()
        if batch is None:
            return "no_batch"
        pending = self.list_pending_media()
        if pending:
            return "continue"
        # ANTI-SPIN (regression du fix #3) : si le cycle est "vide" UNIQUEMENT
        # parce que TOUS les medias du lot sont bloques (zombies id-NULL, non
        # republiables), avancer le cycle ne changera jamais rien -> boucle
        # infinie a vide. On stoppe le lot avec une alerte, plutot que de
        # tourner indefiniment. (Cas rare : exige que chaque media ait crashe
        # dans la fenetre clic->capture.)
        all_names = {p.name for p in self._list_all_media_files(batch.name)}
        blocked = self._blocked_media()
        if all_names and all_names <= blocked:
            log.critical(
                "uploader_batch_frozen_all_blocked",
                batch=batch.name,
                blocked=sorted(blocked),
                hint="tous les medias du lot sont non confirmes (post peut-etre "
                     "cree sans id capture) => lot STOPPE pour eviter une boucle "
                     "a vide ; verifier le compte et resoudre (WS8)",
            )
            self._state.stop_batch()
            return "stopped"
        # cycle complet
        next_cycle = batch.current_cycle + 1
        if 0 < batch.max_cycles < next_cycle:
            log.info(
                "uploader_batch_max_cycles_reached",
                batch=batch.name,
                max_cycles=batch.max_cycles,
                total_published=batch.total_published,
            )
            self._state.stop_batch()
            return "stopped"
        log.info(
            "uploader_cycle_complete",
            batch=batch.name,
            completed_cycle=batch.current_cycle,
            next_cycle=next_cycle,
        )
        self._state.advance_batch_cycle(next_cycle)
        return "advanced"

    async def publish_next(self) -> Optional[str]:
        """Publie un media du lot actif (tirage aleatoire avec memoire).

        Ne gere PLUS l'avancement de cycle — c'est la responsabilite du worker
        via advance_cycle_if_needed() (decouplage pour eviter race condition
        avec le rolling cycle cleanup).

        Capture de l'ID Fansly du post fraichement cree : un buffer local
        `captured = {"value": None}` est cree ICI et passe a _do_upload.
        La closure _log_response y depose l'ID extrait du payload de la
        reponse POST /api/v1/post. Le buffer est partage entre toutes les
        tentatives tenacity de cette publication (premier-gagne strict :
        une fois l'ID capte, les retries ne l'ecrasent pas).
        """
        # Log observabilite : si on hang ici, on saura que c'est sur
        # l'acquisition du _use_lock (un autre service tient la session).
        log.info("publish_next_acquiring_lock")
        async with self._session.use():
            log.info("publish_next_lock_acquired")
            # WS3 — reconciliation des publications en cours orphelines de CE
            # run AVANT toute selection. Couvre le chemin failsafe-timeout
            # (worker.py relance publish_next dans le meme process sans avoir
            # nettoye un in_flight de la tentative precedente) : l'orpheline est
            # resolue (marquee publiee si un clic a eu lieu), donc le media
            # n'est pas reselectionne et un second POST est evite. C'est le fix
            # du doublon reproductible SANS redemarrage (CP-RETRY).
            if self._run_id is not None:
                self._state.reconcile_publish_in_flight(run_id_filter=self._run_id)
            batch = self._state.get_active_batch()
            if batch is None:
                log.info("uploader_no_active_batch")
                return None

            pending = self.list_pending_media()
            if not pending:
                log.info("uploader_no_pending_in_cycle", batch=batch.name)
                return None

            # ---- Playlist ordonnee (fige au 1er cycle, conservee ensuite) ----
            # Comportement voulu :
            #   Cycle 1 : tirage aleatoire des medias du batch -> ordre fige
            #             (persiste en DB dans active_batch.playlist_order)
            #   Cycles 2+ : on suit CET ordre a l'identique, en sautant les
            #               medias deja publies dans le cycle courant.
            #   A1 : un nouveau fichier depose apres le 1er tirage est ajoute
            #        a la fin de la playlist (sans re-shuffle).
            #   B1 : un fichier disparu du disque est saute (l'ordre des
            #        autres n'est pas modifie).
            disk_files_sorted = self._list_all_media_files(batch.name)
            disk_names = [p.name for p in disk_files_sorted]

            # Zombies id-NULL du run : exclus de la selection (anti-doublon).
            # Surface un WARNING tant que WS8 (UI) n'existe pas, pour que
            # l'operateur sache qu'un media est fige et pourquoi.
            blocked = self._blocked_media()
            if blocked:
                log.warning(
                    "uploader_media_blocked_unconfirmed",
                    batch=batch.name,
                    blocked=sorted(blocked),
                    hint="post peut-etre cree sans id capture => NON republie pour "
                         "eviter un doublon non rotable ; verifier le compte (WS8)",
                )

            media_name, new_playlist, event = self._select_next_media(
                current_playlist=batch.playlist_order,
                disk_names_sorted=disk_names,
                # cross-run (cancel->restart) : union published_in_cycle + media_published du cycle
                published_in_cycle=list(self._published_this_cycle(batch)),
                rng=random,
                blocked=blocked,
            )

            # Persist si playlist modifiee (init ou extend)
            if event == "init":
                self._state.set_batch_playlist_order(new_playlist)
                log.info(
                    "uploader_playlist_initialized",
                    batch=batch.name,
                    size=len(new_playlist),
                    order=new_playlist,
                )
            elif event == "extend":
                self._state.set_batch_playlist_order(new_playlist)
                added = [n for n in new_playlist if n not in batch.playlist_order]
                log.info(
                    "uploader_playlist_extended",
                    batch=batch.name,
                    added=added,
                    new_size=len(new_playlist),
                )

            if media_name is None:
                log.warning(
                    "uploader_playlist_exhausted",
                    batch=batch.name,
                    playlist_size=len(new_playlist),
                    published=len(batch.published_in_cycle),
                )
                return None
            media = next(p for p in pending if p.name == media_name)
            caption = self._captions.pick(batch_name=self._captions_batch_name)
            log.info(
                "uploader_starting",
                media=media.name,
                batch=batch.name,
                cycle=batch.current_cycle,
                caption_length=len(caption),
            )

            # Buffer local de capture, partage entre toutes les tentatives
            # tenacity de cette publication. Initialise UNE SEULE FOIS ici :
            # si la tentative 1 capture l'ID Fansly (POST /api/v1/post 2xx)
            # mais echoue plus loin (ex: composer qui ne se ferme pas), la
            # tentative 2 peut succeder cote bot SANS re-trigger un POST
            # (le post est deja publie cote Fansly). On conserve donc l'ID
            # capte au premier essai. Premier-gagne strict : si plusieurs
            # 2xx arrivent, on garde le tout premier.
            # Dict mutable pour pouvoir etre modifie par closure dans
            # _log_response, sans repasser par un attribut d'instance
            # (eliminerait le risque de corruption cross-upload).
            captured: dict = {"value": None}

            # WS4 — cle de la publication en cours, lue par _do_upload_inner
            # (write-ahead + clicked) et le listener de capture (persist id).
            self._cur_inflight_key = {
                "run_id": self._run_id,
                "batch_name": batch.name,
                "cycle_number": batch.current_cycle,
                "media_filename": media.name,
            }

            try:
                await self._auth.ensure_logged_in()

                # Rotation AVANT publication : on supprime la version
                # precedente du media (publiee dans un cycle anterieur)
                # AVANT de publier la nouvelle. C'est le comportement
                # metier demande : "lorsqu'il choisit de poster un nouveau
                # media, qu'il le supprime avant". On passe la PAGE deja
                # recuperee dans le contexte `async with self._session.use()`
                # du publish_next pour eviter le deadlock (asyncio.Lock
                # non-reentrant). NE LEVE pas — toute erreur degrade en
                # log : on continue vers la publication meme si la
                # rotation a echoue (au pire on aura un doublon visible,
                # rattrape au cycle suivant).
                if self._cycle_rotator is not None:
                    try:
                        page = await self._session.page()
                        rot_result = await self._cycle_rotator.rotate_before_publish(
                            page=page,
                            run_id=self._run_id,
                            batch_name=batch.name,
                            current_cycle=batch.current_cycle,
                            media_filename=media.name,
                            cancel_check=self._cancel_check,
                        )
                        log.info("uploader_rotation_result", **rot_result)
                    except Exception as e:  # noqa: BLE001
                        log.error(
                            "uploader_rotation_exception",
                            error=type(e).__name__, media=media.name,
                        )

                # Retry tenacity sur tout le upload : robustesse face aux
                # timings cote Fansly (encodage video, latence reseau, etc.).
                # Si le 1er essai echoue, on relance toute la sequence et
                # Fansly a eu le temps de finaliser entre temps.
                async for attempt in self._retries.network():
                    with attempt:
                        await self._do_upload(media, caption, captured)

                fp_id = captured.get("value")
                # WS4 — commit ATOMIQUE : media_published + published_in_cycle +
                # clear publish_in_flight en UNE transaction (ferme CP4). Remplace
                # l'ancien _mark_published (2 ecritures non atomiques + pas de clear).
                if self._run_id is not None:
                    self._state.commit_publication(
                        run_id=self._run_id,
                        batch_name=batch.name,
                        cycle_number=batch.current_cycle,
                        media_filename=media.name,
                        caption=caption,
                        fansly_post_id=fp_id,
                        add_to_current_cycle=True,
                    )
                    if fp_id:
                        log.info(
                            "uploader_mark_published_with_fansly_id",
                            media=media.name, fansly_post_id=fp_id,
                        )
                    else:
                        log.warning(
                            "uploader_mark_published_without_fansly_id",
                            media=media.name,
                            reason="fansly_post_id_capture_failed_or_not_intercepted",
                        )
                else:
                    # Fallback defensif si l'uploader tourne sans run_id (ne
                    # devrait pas arriver : le worker passe toujours un run_id).
                    self._mark_published(
                        media, caption, batch.name, batch.current_cycle, fp_id,
                    )
                self._preclick_failures.pop(media.name, None)  # reset compteur WS5
                log.info(
                    "uploader_published",
                    media=media.name,
                    batch=batch.name,
                    cycle=batch.current_cycle,
                )

                return media.name

            except Exception as e:  # noqa: BLE001
                log.error("uploader_failed", media=media.name, error=str(e), exc_info=True)
                await self._dump_artifact("upload_failed", media.name)
                # WS5 — quarantaine anti-boucle-infinie sur echec PRE-clic.
                self._maybe_quarantine_after_failure(batch, media)
                return None
            finally:
                self._cur_inflight_key = None

    # ---------- coeur du flow Playwright ----------

    async def _do_upload(
        self, media: Path, caption: str, captured: dict,
    ) -> None:
        """Pilote un upload complet. Le buffer `captured` (cree par
        publish_next, partage entre les tentatives tenacity de la meme
        publication) sera renseigne par la closure _log_response si la
        reponse 2xx de POST /api/v1/post arrive pendant ce flow.

        IMPORTANT : on NE reset PAS captured ici. Si la tentative 1 capture
        l'ID puis echoue plus tard cote bot, la tentative 2 a deja l'ID au
        cas ou Fansly ne re-emettrait pas de POST (post deja publie cote
        serveur). Premier-gagne strict via _capture_fansly_post_id."""
        page = await self._session.page()

        # ─── Instrumentation HTTP : capte tout le trafic vers fansly.com
        # pendant l upload pour diagnostiquer d eventuels rejets cote serveur
        # (status 4xx/5xx, headers manquants, etc.). Filtre minimal pour
        # eviter le bruit (skip les assets statiques).
        # IMPORTANT : on attache les listeners en debut d _do_upload et on
        # les DETACHE en fin via try/finally. Sans ce detach, chaque appel
        # successif a _do_upload accumule des listeners et chaque event est
        # logge N fois (× nombre d appels precedents) — cree de fausses
        # duplications dans les logs HTTP.
        skip_resource_types = {"image", "stylesheet", "font", "media", "manifest", "other"}

        # Tasks de logging/capture en vol ; on les drain au finally pour
        # eliminer la race ou la task de capture ecrirait dans captured
        # APRES la lecture par publish_next (donnant un fansly_post_id None
        # pour une publication ou Fansly avait pourtant repondu 200 avec id).
        pending_tasks: list[asyncio.Task] = []

        def _is_fansly_host(url: str) -> bool:
            # Defense en profondeur : ne pas filtrer sur substring "fansly.com"
            # (qui matche aussi "fansly.com.attacker.example"). On extrait
            # l'hostname propre via urlparse et on verifie l'appartenance
            # stricte au domaine fansly.com.
            try:
                host = (urlparse(url).hostname or "").lower()
            except Exception:  # noqa: BLE001
                return False
            return host == "fansly.com" or host.endswith(".fansly.com")

        async def _log_request(request):
            try:
                if not _is_fansly_host(request.url):
                    return
                if request.resource_type in skip_resource_types:
                    return
                post_data_size = len(request.post_data or b"") if request.post_data else 0
                ct = request.headers.get("content-type", "")
                log.info(
                    "http_req",
                    method=request.method,
                    url=request.url[:180],
                    rtype=request.resource_type,
                    ct=ct[:60],
                    body_bytes=post_data_size,
                )
            except Exception as e:  # noqa: BLE001
                log.debug("http_req_log_error", error=str(e))

        async def _log_response(response):
            try:
                if not _is_fansly_host(response.url):
                    return
                req = response.request
                if req.resource_type in skip_resource_types:
                    return
                status = response.status
                body_preview = ""
                if status >= 400:
                    try:
                        body = await asyncio.wait_for(response.text(), timeout=5.0)
                        body_preview = body[:500]
                    except Exception:  # noqa: BLE001
                        body_preview = "<unreadable>"
                log.info(
                    "http_resp",
                    status=status,
                    method=req.method,
                    url=response.url[:180],
                    body=body_preview if status >= 400 else None,
                )
                # Capture passive de l ID Fansly du post fraichement cree.
                # On parse uniquement les reponses 2xx de POST sur le path
                # EXACT /api/v1/post (creation) — pas /api/v1/post/<id>/<action>
                # (like, delete, etc.) ni d'autres prefixes accidentels.
                if (
                    200 <= status < 300
                    and req.method == "POST"
                    and self._is_post_creation_url(response.url)
                ):
                    await self._capture_fansly_post_id(response, captured)
            except Exception as e:  # noqa: BLE001
                log.debug("http_resp_log_error", error=str(e))

        # Wrap les coroutines dans des tasks trackees, pour pouvoir les
        # drainer en finally avant de retourner (sinon race fire-and-forget).
        def _on_req(req):
            pending_tasks.append(asyncio.create_task(_log_request(req)))

        def _on_resp(resp):
            pending_tasks.append(asyncio.create_task(_log_response(resp)))

        page.on("request", _on_req)
        page.on("response", _on_resp)
        log.info("http_instrumentation_attached")

        try:
            await self._do_upload_inner(page, media, caption)
        finally:
            # Ordre IMPORTANT : on detache d'abord les listeners (plus
            # aucune nouvelle task ne sera append a pending_tasks), puis
            # on drain ce qui est en vol. Sans cet ordre, une task creee
            # in-extremis APRES le snapshot du gather ne serait jamais
            # attendue — race fire-and-forget non eteinte.
            try:
                page.remove_listener("request", _on_req)
                page.remove_listener("response", _on_resp)
            except Exception as e:  # noqa: BLE001
                log.warning("http_instrumentation_detach_failed", error=str(e))

            # Drain : on attend les tasks en vol AVANT de retourner. Sans
            # ca, la task qui parse POST /api/v1/post peut ecrire dans
            # captured APRES que publish_next ait deja lu None.
            # Timeout 12s : couvre le pire cas 5s+5s chaines (response.text
            # dans _log_response puis dans _capture_fansly_post_id) avec
            # une marge de 2s pour la coordination asyncio.
            if pending_tasks:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*pending_tasks, return_exceptions=True),
                        timeout=12.0,
                    )
                except asyncio.TimeoutError:
                    # Timeout : on ne laisse PAS les tasks en vol — sinon
                    # elles ecriraient dans captured APRES que publish_next
                    # ait deja lu (resurgence de la race blocker-1). On les
                    # cancel, puis on draine les cancellations.
                    log.warning(
                        "http_instrumentation_drain_timeout",
                        pending=len(pending_tasks),
                    )
                    for t in pending_tasks:
                        if not t.done():
                            t.cancel()
                    try:
                        await asyncio.wait_for(
                            asyncio.gather(*pending_tasks, return_exceptions=True),
                            timeout=2.0,
                        )
                    except asyncio.TimeoutError:
                        log.error(
                            "http_instrumentation_drain_cancel_timeout",
                            still_pending=sum(1 for t in pending_tasks if not t.done()),
                        )
                except Exception as e:  # noqa: BLE001
                    log.debug("http_instrumentation_drain_error", error=str(e))
            log.info("http_instrumentation_detached")

    async def _do_upload_inner(self, page, media: Path, caption: str) -> None:
        """Implementation interne de _do_upload (sans gestion des listeners
        HTTP). Permet a `_do_upload` de gerer l attach/detach dans un
        try/finally proprement, sans dupliquer le code metier."""
        await page.goto(self._settings.auth.base_url + "/home", wait_until="domcontentloaded")
        await self._humanizer.long_pause()
        await self._auth._dismiss_overlays(page)  # type: ignore[attr-defined]

        # 0) Ouvrir le composer (modale ou route dediee)
        await self._open_composer(page)

        # 1) Saisir la legende — auto-ajout de #fyp si absent
        # La doc Fansly Help Center recommande explicitement ce hashtag pour
        # maximiser la visibilite sur le For You Page.
        if not re.search(r"#fyp\b", caption, re.IGNORECASE):
            caption = caption.rstrip() + " #fyp"
            log.info("uploader_fyp_hashtag_added")
        textarea = Sel.composer_textarea(page)
        await textarea.wait_for(state="visible", timeout=15_000)
        await self._humanizer.type_humanly(textarea, caption)
        await self._humanizer.short_pause()

        # 2) Attacher le fichier PRINCIPAL.
        # NOTE technique : `set_input_files` de Playwright ne declenche pas
        # toujours les events que l app Fansly (Angular) ecoute. Resultat
        # observe : le fichier est pose dans le DOM mais aucune requete XHR
        # d upload ne part vers les serveurs Fansly → la modale affiche
        # "no content selected" et le bouton final reste indisponible.
        # On utilise un drop simule via DataTransfer : on construit un File
        # JS natif a partir du contenu du fichier et on dispatche les events
        # dragenter/dragover/drop sur l input, exactement comme si l utilisateur
        # avait depose le fichier physiquement.
        inputs = Sel.file_input(page)
        n_inputs = await inputs.count()
        if n_inputs == 0:
            raise RuntimeError("Aucun input[type=file] trouve dans le composer.")
        await self._drop_file_into_input(page, inputs.nth(0), media)
        log.info("uploader_main_uploaded", media=media.name)
        await self._humanizer.long_pause()

        # 3) Attendre que la modale "Upload Media" s'ouvre
        try:
            await page.locator("app-account-media-upload").first.wait_for(
                state="visible", timeout=10_000
            )
            log.info("upload_media_modal_open")
        except PWTimeout:
            log.warning("upload_media_modal_not_visible_continuing")

        # 4) Uploader la PREVIEW dans input[1]
        # C'est la cle de la visibilite FYP : un post sans preview reste invisible
        # algorithmiquement. On utilise le meme fichier que le principal
        # (equivalent de l'option "Clone" du dropdown Add Free Preview).
        inputs_after = page.locator("input[type='file']")
        n_after = await inputs_after.count()
        log.info("uploader_inputs_after_main", count=n_after)
        if n_after >= 2:
            try:
                # Meme strategie que pour le main : drop simule via DataTransfer
                await self._drop_file_into_input(page, inputs_after.nth(1), media)
                log.info("uploader_preview_uploaded", media=media.name)
            except Exception as e:  # noqa: BLE001
                log.warning("uploader_preview_upload_failed", error=str(e))
        else:
            log.warning("uploader_preview_input_not_found",
                        hint="Fansly n'expose pas input[1] — post sans preview = pas FYP")

        await self._humanizer.short_pause()

        # 5) L'editeur de preview (app-media-editor.active-modal) s'ouvre
        # automatiquement apres set_input_files sur input[1]. On le ferme via
        # "Save Changes" (sans modifier la preview, equivalent du "clone original").
        try:
            editor = Sel.media_editor_modal(page)
            if await editor.is_visible(timeout=5_000):
                log.info("uploader_media_editor_detected")
                save_btn = Sel.media_editor_save_changes(page)
                await save_btn.wait_for(state="visible", timeout=5_000)
                await self._humanizer.hover_then_click(save_btn)
                await editor.wait_for(state="hidden", timeout=10_000)
                log.info("uploader_media_editor_saved")
                await self._humanizer.short_pause()
        except PWTimeout:
            log.debug("uploader_media_editor_not_shown")
        except Exception as e:  # noqa: BLE001
            log.warning("uploader_media_editor_error", error=str(e))

        # 6) Confirmer la modale "Media Permissions" via le bouton "Upload"
        try:
            perm_modal = Sel.media_permissions_modal(page)
            if await perm_modal.is_visible(timeout=8000):
                log.info("media_permissions_modal_detected")
                upload_btn = Sel.media_permissions_upload_button(page)
                await upload_btn.wait_for(state="visible", timeout=5000)
                await self._humanizer.hover_then_click(upload_btn)
                await perm_modal.wait_for(state="hidden", timeout=20_000)
                log.info("media_permissions_confirmed")
                await self._humanizer.short_pause()
        except PWTimeout as e:
            log.warning("media_permissions_timeout", error=str(e))
        except Exception as e:  # noqa: BLE001
            log.warning("media_permissions_handle_error", error=str(e))

        # 4) Etape "Save Changes" pour les images (best effort, optionnel)
        try:
            save_btn = Sel.save_changes_button(page)
            if await save_btn.is_visible(timeout=2500):
                await self._humanizer.hover_then_click(save_btn)
                await self._humanizer.short_pause()
        except Exception:  # noqa: BLE001
            pass

        # 5) Attendre que le bouton final "Post" devienne ACTIF
        # Fansly garde la classe "disabled" tant qu'il n'a pas fini de traiter
        # le fichier cote serveur. Cliquer pendant ce temps ne publie rien.
        submit = Sel.submit_post_button(page)
        await submit.wait_for(state="visible", timeout=30_000)

        deadline = time.monotonic() + 180
        wait_count = 0
        while time.monotonic() < deadline:
            cls = (await submit.get_attribute("class")) or ""
            if "disabled" not in cls:
                break
            wait_count += 1
            if wait_count % 10 == 0:
                log.info("uploader_waiting_server_processing", seconds_elapsed=wait_count)
            await asyncio.sleep(1.0)
        else:
            raise RuntimeError("Bouton Post est reste 'disabled' au-dela de 180s")

        log.info("uploader_submit_button_active")
        await self._humanizer.short_pause()

        # WS4 — WRITE-AHEAD + garde anti-double-POST DURABLE (crash-resume).
        # Juste avant le clic Post : on ecrit l'intent (publish_in_flight) PUIS
        # clicked=1, commit AVANT le clic. A la reprise, l'etat clicked tranche :
        #   clicked=0 => aucun POST parti => republier en surete
        #   clicked=1 => un POST a pu partir => JAMAIS republier
        # Garde in-session : si un clic a DEJA ete emis pour ce media (tentative
        # tenacity anterieure ou relance failsafe-timeout), on NE reclique PAS.
        # Cette garde repose sur l'etat DURABLE (clicked), PAS sur captured[value]
        # en memoire (qui echoue justement en cas de capture ratee -> hole 4).
        key = self._cur_inflight_key
        if key and key.get("run_id") is not None:
            existing = self._state.get_in_flight(**key)
            if not self._should_emit_post_click(existing):
                log.warning(
                    "uploader_skip_reclick_already_clicked",
                    media=media.name,
                    hint="un clic Post a deja ete emis pour ce media -> pas de second POST",
                )
                # On ne reclique pas : le post a pu etre cree. La suite (attente
                # fermeture composer / capture) se deroule ; a defaut la
                # reconciliation du prochain publish_next resoudra l'in_flight.
            else:
                self._state.mark_publish_in_flight(
                    key["run_id"], key["batch_name"], key["cycle_number"],
                    key["media_filename"], caption,
                )
                self._state.mark_in_flight_clicked(
                    key["run_id"], key["batch_name"], key["cycle_number"],
                    key["media_filename"],
                )
                await self._humanizer.hover_then_click(submit)
        else:
            await self._humanizer.hover_then_click(submit)

        # 6) Verification que la publication a abouti : URL change ou composer ferme
        try:
            await page.wait_for_function(
                """() => {
                    const modal = document.querySelector('app-account-media-upload, .modal-wrapper');
                    return !modal || modal.offsetParent === null;
                }""",
                timeout=60_000,
            )
            log.info("uploader_composer_closed")
        except PWTimeout:
            log.warning("uploader_composer_did_not_close", media=media.name)
            raise RuntimeError("Composer Fansly n'a pas ete ferme apres clic Post")

        # 7) Une eventuelle modale de confirmation "Post" peut apparaitre apres
        try:
            confirm = page.locator("div.btn.solid-blue").filter(
                has_text=re.compile(r"^\s*Post\s*$", re.I)
            ).first
            if await confirm.is_visible(timeout=3000):
                await self._humanizer.hover_then_click(confirm)
                await self._humanizer.short_pause()
        except Exception:  # noqa: BLE001
            pass

        await self._humanizer.long_pause()
        log.info("uploader_post_submitted", media=media.name)

    async def _open_composer(self, page) -> None:
        """Ouvre le composer Fansly. Strategie : essayer le bouton "+ new post"
        sur la page courante. Si rien ne s'ouvre, fallback /post/new."""
        # Tentative 1 : cliquer le bouton "+ new post"
        try:
            btn = Sel.open_composer_button(page)
            if await btn.is_visible(timeout=3000):
                await self._humanizer.hover_then_click(btn)
                await self._humanizer.short_pause()
                # Verifie qu'un textarea est apparu (modale composer ouverte)
                if await Sel.composer_textarea(page).is_visible(timeout=4000):
                    log.info("composer_opened", via="new_post_button")
                    return
        except Exception as e:  # noqa: BLE001
            log.debug("composer_button_click_failed", error=str(e))

        # Tentative 2 : navigation directe vers /post/new
        try:
            await page.goto(
                self._settings.auth.base_url + "/post/new",
                wait_until="domcontentloaded",
            )
            await self._humanizer.long_pause()
            await self._auth._dismiss_overlays(page)  # type: ignore[attr-defined]
            if await Sel.composer_textarea(page).is_visible(timeout=8000):
                log.info("composer_opened", via="direct_url")
                return
        except Exception as e:  # noqa: BLE001
            log.debug("composer_direct_url_failed", error=str(e))

        raise RuntimeError("Impossible d'ouvrir le composer Fansly")

        # 5) Attendre la fin du traitement serveur : le bouton "new post" devient
        # actif. Fallback : attendre que le composer disparaisse.
        try:
            new_post = Sel.new_post_button(page)
            await new_post.wait_for(state="visible", timeout=120_000)
            cls = (await new_post.get_attribute("class")) or ""
            # On attend qu'il ne soit plus "disabled"
            deadline = asyncio.get_event_loop().time() + 120
            while "disabled" in cls and asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(1.0)
                cls = (await new_post.get_attribute("class")) or ""
            await self._humanizer.hover_then_click(new_post)
        except PWTimeout:
            # Fallback : on considere la publication faite si le composer s'est referme.
            log.warning("uploader_new_post_btn_missing_fallback", media=media.name)

        await self._humanizer.long_pause()
        log.info("uploader_post_submitted", media=media.name)

    # ---------- attachement de fichier via drop simule ----------

    async def _drop_file_into_input(self, page: Page, input_locator, media: Path) -> None:
        """Attache un fichier a un <input type="file"> via un evenement
        drop synthetique avec DataTransfer.

        Pourquoi pas `set_input_files` (Playwright) :
            Playwright ecrit le fichier dans l input mais le change event
            n est pas toujours capte par les apps Angular qui ecoutent le
            DataTransfer / drop natif. Resultat : aucune requete XHR
            d upload n est lancee et le serveur ne recoit jamais le fichier.

        Strategie :
            1. Lire le contenu binaire du fichier cote Python.
            2. L injecter dans le navigateur en base64 via page.evaluate.
            3. Cote JS : decoder en Uint8Array → construire un File natif
               → mettre dans un DataTransfer → dispatcher
               dragenter/dragover/drop sur l element cible.
            Le browser traite l upload comme s il venait d un vrai user.

        Cout : transit base64 via CDP (gros pour videos). Acceptable
        pour des medias < 100 Mo. Si plus gros, prevoir un serveur HTTP
        local de fichiers et faire le drop via une URL.
        """
        file_bytes = media.read_bytes()
        if not file_bytes:
            raise RuntimeError(f"Fichier vide : {media}")
        b64 = base64.b64encode(file_bytes).decode("ascii")
        mime, _ = mimetypes.guess_type(media.name)
        if not mime:
            mime = "application/octet-stream"
        log.info(
            "drop_file_starting",
            media=media.name,
            size_bytes=len(file_bytes),
            mime=mime,
        )

        # On utilise evaluate_handle pour passer l input locator au JS.
        element = await input_locator.element_handle()
        if element is None:
            raise RuntimeError("Input file introuvable au moment du drop.")

        await page.evaluate(
            """
            async ({input, name, b64, mime}) => {
                // Decodage base64 → Uint8Array
                const binStr = atob(b64);
                const len = binStr.length;
                const bytes = new Uint8Array(len);
                for (let i = 0; i < len; i++) bytes[i] = binStr.charCodeAt(i);

                const file = new File([bytes], name, {type: mime, lastModified: Date.now()});
                const dt = new DataTransfer();
                dt.items.add(file);

                // Pose le fichier dans input.files puis dispatch UN SEUL event :
                // `change`, l event standard que tout composant qui consomme un
                // <input type="file"> ecoute. Avec bubbles=true mais SANS les
                // events drag-drop (qui creaient 3 uploads dupliques par
                // propagation aux containers parents Angular).
                try {
                    Object.defineProperty(input, 'files', {
                        value: dt.files,
                        writable: false,
                        configurable: true,
                    });
                } catch (e) {
                    // Si redefinition refusee, on tente quand meme le change.
                }
                input.dispatchEvent(new Event('change', {bubbles: true, composed: true}));
            }
            """,
            {"input": element, "name": media.name, "b64": b64, "mime": mime},
        )
        log.info("drop_file_done", media=media.name)

    # ---------- post-traitement ----------

    def _maybe_quarantine_after_failure(self, batch, media: Path) -> None:
        """WS5 — anti-boucle-infinie sur echec PRE-clic.

        Si un upload echoue AVANT le write-ahead (fichier corrompu, selecteur
        casse, composer introuvable), aucune ligne publish_in_flight n'existe :
        aucun POST n'est parti, mais _select_next_media renverra deterministe-
        ment le MEME 1er-pending au prochain tour -> boucle 60s a l'infini, le
        lot n'avance jamais. On compte les echecs consecutifs par media ; apres
        N, on QUARANTAINE le media (ajout a published_in_cycle => saute pour ce
        cycle) pour que la playlist progresse, avec alerte CRITICAL.

        Si une ligne in_flight EXISTE deja (le clic a pu partir), on ne
        quarantaine PAS : la reconciliation du prochain publish_next resoudra
        proprement (marque publie si clicked, sans republier).
        """
        if self._run_id is None:
            return
        try:
            in_flight = self._state.get_in_flight(
                self._run_id, batch.name, batch.current_cycle, media.name
            )
        except Exception:  # noqa: BLE001
            in_flight = None
        if in_flight is not None:
            return  # write-ahead present -> la reconciliation gerera
        n = self._preclick_failures.get(media.name, 0) + 1
        self._preclick_failures[media.name] = n
        if n >= 3:
            self._state.add_to_batch_published(media.name)  # skip ce cycle
            self._preclick_failures.pop(media.name, None)
            log.critical(
                "uploader_media_quarantined",
                media=media.name, failures=n,
                hint="echec pre-clic repete (fichier/selecteur ?) -> media saute "
                     "pour que le lot avance ; a verifier manuellement",
            )
        else:
            log.warning("uploader_preclick_failure", media=media.name, count=n)

    def _mark_published(
        self, media: Path, caption: str, batch_name: str, cycle: int,
        fansly_post_id: Optional[str],
    ) -> None:
        """Mode lot : on NE deplace PAS le fichier (il sera republie au prochain
        cycle). On enregistre la publication en BDD et on marque le fichier
        comme publie dans le cycle courant.

        Si l ID Fansly du post a ete capture pendant _do_upload (via le
        listener HTTP sur POST /api/v1/post), on le persiste avec la
        publication. Sinon le champ reste NULL et la rotation per-media
        ne pourra pas cibler ce post (fallback : pas de rotation pour
        cette publication-la)."""
        self._state.add_to_batch_published(media.name)
        self._state.record_media_published(
            media.name, caption,
            batch_name=batch_name, cycle_number=cycle,
            run_id=self._run_id,
            fansly_post_id=fansly_post_id,
        )
        if fansly_post_id:
            log.info(
                "uploader_mark_published_with_fansly_id",
                media=media.name, fansly_post_id=fansly_post_id,
            )
        else:
            log.warning(
                "uploader_mark_published_without_fansly_id",
                media=media.name,
                reason="fansly_post_id_capture_failed_or_not_intercepted",
            )

    @staticmethod
    def _is_post_creation_url(url: str) -> bool:
        """Renvoie True ssi l'URL est exactement le endpoint de creation
        de post Fansly (POST /api/v1/post). Exclut explicitement les
        sous-paths d'action (/api/v1/post/<id>/like, /delete, etc.) et
        les variantes accidentelles (/api/v1/repost, /api/v2/api/v1/post)."""
        try:
            path = urlparse(url).path
        except Exception:  # noqa: BLE001
            return False
        return path.rstrip("/") == "/api/v1/post"

    async def _capture_fansly_post_id(self, response, captured: dict) -> None:
        """Parse la reponse JSON de POST /api/v1/post pour extraire l ID
        du post fraichement cree et le stocker dans `captured["value"]`.

        Premier-gagne strict : si captured["value"] est deja renseigne (par
        une reponse precedente du meme upload, ou par la tentative tenacity
        precedente), on NE remplace PAS. Cela protege contre :
          - Plusieurs reponses 2xx /api/v1/post pendant le meme _do_upload
            (draft auto + publication finale), dont l'ordre d'arrivee asyncio
            n'est pas deterministe.
          - Retry tenacity ou la tentative 2 ne re-trigger pas de POST mais
            ou Fansly avait deja repondu OK a la tentative 1.

        Formats observes sur l API Fansly (variantes possibles selon endpoint) :
          - {"success": true, "response": {"id": "<numeric>", ...}}
          - {"success": true, "response": [{"id": "<numeric>", ...}]}
          - {"id": "<numeric>", ...}              (fallback racine)
          - [{"id": "<numeric>", ...}]            (top-level array)

        Defensive : toute exception est logguee en debug et n empeche pas
        le flow upload de poursuivre. Si N captures consecutives echouent,
        on escalade en log.error pour signaler un eventuel drift de schema
        API Fansly (renommage de cle, changement de structure)."""
        # Premier-gagne : on ne re-ecrit pas une valeur deja capturee.
        if captured.get("value") is not None:
            return
        try:
            body = await asyncio.wait_for(response.text(), timeout=5.0)
        except Exception as e:  # noqa: BLE001
            log.debug("fansly_post_id_capture_body_unreadable", error=str(e))
            return
        try:
            data = json.loads(body)
        except Exception as e:  # noqa: BLE001
            log.debug("fansly_post_id_capture_json_parse_failed", error=str(e))
            return

        # Recherche non-exclusive : on essaye plusieurs emplacements, on
        # garde la premiere valeur non vide trouvee.
        fp_id = None
        block = data.get("response") if isinstance(data, dict) else None
        if isinstance(block, dict):
            fp_id = block.get("id")
        elif isinstance(block, list) and block and isinstance(block[0], dict):
            fp_id = block[0].get("id")
        if (fp_id is None or str(fp_id) == "") and isinstance(data, dict):
            fp_id = data.get("id")
        if (fp_id is None or str(fp_id) == "") and isinstance(data, list) and data and isinstance(data[0], dict):
            fp_id = data[0].get("id")

        if fp_id is not None and str(fp_id) != "":
            # Premier-gagne ATOMIQUE : on a possiblement yield-e sur
            # response.text() + json.loads pendant que d'autres tasks
            # concurrentes (cas plusieurs reponses 2xx parallels) ont
            # peut-etre deja ecrit dans captured. Re-verifier ICI, juste
            # avant l'ecriture, garantit que la premiere valeur durablement
            # ecrite est conservee (et non la derniere a finir le parse).
            if captured.get("value") is not None:
                log.debug(
                    "uploader_fansly_post_id_race_lost",
                    candidate=str(fp_id),
                    kept=captured["value"],
                )
                return
            captured["value"] = str(fp_id)
            self._capture_miss_streak = 0
            log.debug("uploader_captured_fansly_post_id", fansly_post_id=str(fp_id))
            # WS4 — rendre l'id DURABLE immediatement (premier-gagne cote DB).
            # Un 2xx sur POST /api/v1/post PROUVE que le post existe : on persiste
            # l'id dans publish_in_flight sans attendre commit_publication. Ainsi
            # un crash dans la fenetre "post cree / pas encore committe" (CP3,
            # celle qui a produit le doublon reel) laisse un in_flight AVEC id
            # que la reconciliation convertit en 'publie confirme', jamais en
            # 're-publier'. Best-effort : une erreur ici n'interrompt pas la capture.
            key = getattr(self, "_cur_inflight_key", None)
            if key and key.get("run_id") is not None:
                try:
                    self._state.set_in_flight_post_id(
                        key["run_id"], key["batch_name"], key["cycle_number"],
                        key["media_filename"], str(fp_id),
                    )
                except Exception as e:  # noqa: BLE001
                    log.debug("set_in_flight_post_id_failed", error=str(e))
        else:
            # PII redact : on ne logue PAS le body brut (peut contenir
            # accountId du createur, caption, mediaIds, tokens internes).
            # Seules les CLES top-level sont expose pour diagnostic schema.
            if isinstance(data, dict):
                shape = sorted(list(data.keys()))[:10]
            elif isinstance(data, list):
                shape = ["<list>"]
            else:
                shape = [type(data).__name__]
            self._capture_miss_streak += 1
            log.warning(
                "fansly_post_id_not_in_response",
                top_level_keys=shape,
                miss_streak=self._capture_miss_streak,
            )
            # Escalade : N echecs consecutifs = drift probable de l'API
            # Fansly (renommage de cle, restructuration). On veut un
            # signal fort pour ne pas tourner en silence avec 0% capture.
            if self._capture_miss_streak >= 3:
                log.error(
                    "fansly_post_id_capture_drift_suspected",
                    consecutive_misses=self._capture_miss_streak,
                    hint="API schema may have changed; check top_level_keys above",
                )

    async def _dump_artifact(self, label: str, media_name: str) -> None:
        from datetime import datetime, timezone

        try:
            page = await self._session.page()
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            out = self._settings.paths.artifacts_dir / "upload" / f"{stamp}_{label}_{media_name}"
            out.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(out / "screen.png"), full_page=True)
            (out / "page.html").write_text(await page.content(), encoding="utf-8")
            log.info("uploader_artifact_saved", path=str(out))
        except Exception as e:  # noqa: BLE001
            log.warning("uploader_artifact_dump_failed", error=str(e))


