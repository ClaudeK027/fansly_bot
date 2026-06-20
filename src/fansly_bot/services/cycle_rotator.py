# src/fansly_bot/services/cycle_rotator.py
"""Service de rotation per-media : avant chaque publication, supprime
la version precedente du meme media.

AVANT chaque publication d'un media dans le cycle N+1, on cherche en
BDD si ce meme media a deja ete publie dans un cycle anterieur du
meme run/batch ; si oui ET si on a son fansly_post_id (capture Phase A),
on supprime ce post precedent via PurgerService.delete_post_by_fansly_id().
La suppression utilise le permalien direct Fansly (`/post/<id>`), pas
de scroll du feed profil.

Skip gracieux (jamais d'exception remontee a l'appelant) :
  - Premier cycle (current_cycle <= 1) : rien a supprimer
  - Filename absent des cycles precedents (nouveau media dans le pool)
  - Cycle precedent existe mais sans fansly_post_id (capture ratee)
  - Post Fansly deja supprime cote serveur (status 'not_found')
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Optional

import structlog

from ..browser.humanizer import Humanizer
from ..browser.session import BrowserSession
from ..config import Settings
from ..infra.retry import RetryPolicies
from ..infra.state import StateStore
from .auth import AuthService
from .purger import PurgerService

if TYPE_CHECKING:
    from playwright.async_api import Page

log = structlog.get_logger("services.cycle_rotator")


class CycleRotator:
    def __init__(
        self,
        settings: Settings,
        session: BrowserSession,
        humanizer: Humanizer,
        auth: AuthService,
        state: StateStore,
        retries: RetryPolicies,
    ) -> None:
        self._settings = settings
        self._session = session
        self._humanizer = humanizer
        self._auth = auth
        self._state = state
        self._retries = retries
        # On instancie le PurgerService UNE FOIS pour eviter de payer
        # le cout de construction a chaque rotation. Pas d'etat partage
        # entre rotations qui poserait probleme : cancel_check est
        # explicitement passe a chaque appel.
        self._purger = PurgerService(
            settings, session, humanizer, auth, state, retries,
        )

    async def rotate_before_publish(
        self,
        *,
        page: "Page",
        run_id: int,
        batch_name: str,
        current_cycle: int,
        media_filename: str,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> dict:
        """AVANT de publier un media au cycle N+1, supprime sa version
        precedente (publiee en cycle <= N) si elle existe.

        Comportement metier demande : "lorsqu'il choisit de poster un
        nouveau media, qu'il le supprime avant". On rote d'abord, on
        publie ensuite. Si la publication echoue par la suite, l'ancien
        post sera deja supprime (compromis assume).

        `page` est fournie par l'appelant (uploader, dans un contexte
        `async with self._session.use()`). On NE re-acquiert PAS la
        session — sans ca, deadlock sur asyncio.Lock non-reentrant.

        NE LEVE jamais : toute erreur est loggee et degrade en skip.
        Statuts possibles :
          - first_cycle_skip : current_cycle <= 1, rien a roter
          - no_previous_id : pas de fansly_post_id capture pour ce media
                             dans un cycle precedent
          - db_error : exception inattendue au lookup BDD
          - deleted | not_found | cancelled | failed | guard_fyp :
              statuts remontes par PurgerService.delete_post_by_fansly_id
        """
        # Premier cycle : pas de publication anterieure.
        if current_cycle <= 1:
            log.debug(
                "rotator_first_cycle_skip",
                media=media_filename, cycle=current_cycle,
            )
            return {
                "status": "first_cycle_skip",
                "media": media_filename,
                "cycle": current_cycle,
            }

        # Lookup BDD avec run_id OBLIGATOIRE pour garantir l'isolation
        # cross-runs (cf. state.get_fansly_post_id_for_previous_cycle).
        try:
            fansly_id = self._state.get_fansly_post_id_for_previous_cycle(
                run_id=run_id,
                batch_name=batch_name,
                media_filename=media_filename,
                current_cycle=current_cycle,
            )
        except Exception as e:  # noqa: BLE001
            log.error(
                "rotator_db_lookup_failed",
                error=type(e).__name__,
                media=media_filename, cycle=current_cycle,
            )
            return {
                "status": "db_error",
                "media": media_filename,
                "cycle": current_cycle,
                "error": str(e),
            }

        if not fansly_id:
            log.info(
                "rotator_skip_no_previous_id",
                media=media_filename, cycle=current_cycle,
                hint="filename absent du cycle prec OU capture HTTP ratee",
            )
            return {
                "status": "no_previous_id",
                "media": media_filename,
                "cycle": current_cycle,
            }

        # On loggue en DEBUG l'ID brut (PII : identifiant correlateur
        # Fansly stable). Le log.info ne contient que les metadonnees.
        log.debug(
            "rotator_target_identified",
            media=media_filename, cycle=current_cycle,
            fansly_post_id=fansly_id,
        )
        log.info(
            "rotator_target_identified_meta",
            media=media_filename, cycle=current_cycle,
        )

        # Strategie : navigation directe au permalien Fansly du post.
        # Pas de scroll feed (le DOM Fansly du profil n'a ni href post
        # ni attribut data-* avec l'ID post — match impossible par cette
        # voie). Le permalien est l'unique point d'identification fiable.
        try:
            result = await self._purger.delete_post_by_fansly_id(
                fansly_id,
                page=page,
                cancel_check=cancel_check,
                require_fyp=True,
            )
        except Exception as e:  # noqa: BLE001
            log.error(
                "rotator_purger_exception",
                error=type(e).__name__,
                media=media_filename,
            )
            return {
                "status": "purger_exception",
                "media": media_filename,
                "cycle": current_cycle,
                "error": str(e),
            }

        log.info(
            "rotator_complete",
            media=media_filename, cycle=current_cycle,
            purger_status=result.get("status"),
            purger_examined=result.get("examined"),
        )
        # Si la rotation a echoue silencieusement (not_found, failed,
        # guard_fyp), on escalade en log.error pour visibilite operateur
        # — sinon ces statuts s'accumulent en warning et passent inapercus.
        bad_statuses = {"not_found", "failed", "guard_fyp"}
        if result.get("status") in bad_statuses:
            log.error(
                "rotator_unresolved",
                media=media_filename, cycle=current_cycle,
                purger_status=result.get("status"),
                hint="rotation failed; old post may still exist on Fansly",
            )

        return {
            "status": result.get("status"),
            "media": media_filename,
            "cycle": current_cycle,
            "purger_examined": result.get("examined"),
            "error": result.get("error"),
        }
