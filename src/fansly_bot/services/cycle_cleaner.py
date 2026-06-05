# src/fansly_bot/services/cycle_cleaner.py
"""Service de nettoyage du cycle precedent dans un job de publication.

A chaque transition cycle N → cycle N+1, si l'option 'delete_previous_cycle'
est active, ce service supprime de Fansly tous les posts publies durant le
cycle N. Strategie :
  - Fenetre temporelle precise (min/max published_at + marge de 5 min)
  - Filtre par mot-cle '#fyp' (signature systematique du bot) → on ne touche
    JAMAIS aux posts publies manuellement par le createur
  - Retry x3 si la suppression est incomplete
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable, Optional

import structlog

from ..browser.humanizer import Humanizer
from ..browser.session import BrowserSession
from ..config import Settings
from ..infra.retry import RetryPolicies
from ..infra.state import StateStore
from .auth import AuthService
from .purger import PurgerService

log = structlog.get_logger("services.cycle_cleaner")


class CycleCleaner:
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

    async def delete_previous_cycle(
        self,
        batch_name: str,
        completed_cycle: int,
        cancel_check: Optional[Callable[[], bool]] = None,
        max_retries: int = 3,
        run_id: Optional[int] = None,
    ) -> dict[str, Any]:
        """Supprime les posts du cycle `completed_cycle` du lot `batch_name`.

        Si `run_id` est fourni, on ne regarde que les publications de ce run —
        cela isole strictement le cleanup au job courant et empeche d englober
        les publications d un run anterieur sur le meme lot.

        Renvoie un dict avec : expected, deleted, status (complete|partial|failed|empty)."""
        # 1) Liste des publications enregistrees pour ce cycle.
        # Si run_id est fourni : filtre strict sur ce run. Sinon (retro-compat
        # ou run_id NULL en BDD) : on garde l ancien comportement.
        if run_id is not None:
            cur = self._state._conn.execute(  # noqa: SLF001
                "SELECT published_at FROM media_published "
                "WHERE run_id = ? AND batch_name = ? AND cycle_number = ? "
                "ORDER BY published_at",
                (run_id, batch_name, completed_cycle),
            )
        else:
            cur = self._state._conn.execute(  # noqa: SLF001
                "SELECT published_at FROM media_published "
                "WHERE batch_name = ? AND cycle_number = ? "
                "ORDER BY published_at",
                (batch_name, completed_cycle),
            )
        rows = cur.fetchall()

        if not rows:
            log.info(
                "cycle_cleanup_no_records",
                batch=batch_name,
                cycle=completed_cycle,
            )
            return {"expected": 0, "deleted": 0, "status": "empty"}

        # Verifie que le lot est toujours actif (peut avoir ete arrete via UI
        # entre le declenchement et l'execution effective du cleanup)
        active = self._state.get_active_batch()
        if active is None or active.name != batch_name:
            log.warning(
                "cycle_cleanup_batch_gone",
                batch_name=batch_name,
                active_now=(active.name if active else None),
            )
            return {"expected": len(rows), "deleted": 0, "status": "batch_gone"}

        expected = len(rows)
        dates = [datetime.fromisoformat(r["published_at"]) for r in rows]
        # Marge de 5 min de chaque cote (gere les decalages d'horloge Fansly/local)
        start = min(dates) - timedelta(minutes=5)
        end = max(dates) + timedelta(minutes=5)

        # Cap strict : nombre de fichiers media reellement presents dans le
        # dossier du lot. Empeche de supprimer plus que la taille physique
        # du lot, meme si la DB contient des publications "fantomes" pour ce
        # cycle. Si le dossier est introuvable on retombe sur `expected`.
        batch_folder = self._settings.paths.media_folder / batch_name
        if batch_folder.is_dir():
            exts = set(self._settings.publishing.media_extensions)
            batch_size = sum(
                1 for p in batch_folder.iterdir()
                if p.is_file() and p.suffix.lower() in exts
            )
        else:
            batch_size = expected
        # Le cap effectif est le min entre ce qu'on attend (DB) et la taille du
        # lot — jamais plus que le nombre de medias publiables une fois.
        max_dels = min(expected, batch_size) if batch_size > 0 else expected

        log.info(
            "cycle_cleanup_started",
            batch=batch_name,
            cycle=completed_cycle,
            expected=expected,
            batch_size=batch_size,
            max_deletions_cap=max_dels,
            window_start=start.isoformat(),
            window_end=end.isoformat(),
        )

        # 2) Construit un PurgerService temporaire cible (fenetre + #fyp).
        # On cherche "#fyp" (avec le #) plutot que juste "fyp" : evite de
        # matcher accidentellement un mot contenant "fyp" en sous-chaine.
        # PurgerService._normalize preserve le # (juste casefold + diacritiques),
        # donc "#fyp" reste "#fyp" apres normalisation.
        purge_cfg = self._settings.purge.model_copy(
            update={
                "keywords": ["#fyp"],  # signature systematique stricte du bot
                "keyword_match_mode": "any",
                "dry_run": False,
                # Plafond strict = taille reelle du lot. Securite contre une DB
                # qui sur-comptabiliserait les publications du cycle.
                "max_deletions_per_run": max_dels,
                "scroll_safety_cap": max(80, max_dels * 5),
            }
        )
        custom_settings = self._settings.model_copy(update={"purge": purge_cfg})
        purger = PurgerService(
            custom_settings,
            self._session,
            self._humanizer,
            self._auth,
            self._state,
            self._retries,
        )
        purger._date_window = (start, end)  # noqa: SLF001
        if cancel_check is not None:
            purger._cancel_check = cancel_check  # noqa: SLF001

        # 3) Retry jusqu'a `max_retries` fois si la suppression est partielle.
        # On isole strictement le compte : on releve l'id max de purge_runs
        # AVANT chaque run, et on somme les `deleted` des runs creees apres.
        total_deleted = 0
        last_error: Optional[str] = None
        status = "failed"

        for attempt in range(1, max_retries + 1):
            row = self._state._conn.execute(  # noqa: SLF001
                "SELECT COALESCE(MAX(id), 0) AS max_id FROM purge_runs"
            ).fetchone()
            pre_max_id = int(row["max_id"]) if row else 0

            try:
                await purger.run()
                row2 = self._state._conn.execute(  # noqa: SLF001
                    "SELECT COALESCE(SUM(deleted), 0) AS s FROM purge_runs WHERE id > ?",
                    (pre_max_id,),
                ).fetchone()
                run_deleted = int(row2["s"]) if row2 else 0
                total_deleted += run_deleted
                log.info(
                    "cycle_cleanup_attempt",
                    attempt=attempt,
                    run_deleted=run_deleted,
                    total_deleted=total_deleted,
                    target=max_dels,
                    expected=expected,
                )
                if total_deleted >= max_dels or run_deleted == 0:
                    # Soit on a atteint le cap, soit on ne progresse plus (posts deja partis)
                    break
            except Exception as e:  # noqa: BLE001
                last_error = str(e)
                log.warning("cycle_cleanup_attempt_failed", attempt=attempt, error=last_error)
            # Pause humaine entre retries
            try:
                await self._humanizer.short_pause()
            except Exception:  # noqa: BLE001
                pass

        # status base sur le cap (max_dels), pas sur expected : si la DB
        # contient des publications fantomes au-dela de batch_size, ce n'est
        # pas un echec d'avoir supprime "seulement" batch_size posts.
        if total_deleted >= max_dels:
            status = "complete"
        elif total_deleted > 0:
            status = "partial"
        else:
            status = "failed"

        log.info(
            "cycle_cleanup_done",
            batch=batch_name,
            cycle=completed_cycle,
            expected=expected,
            target=max_dels,
            deleted=total_deleted,
            status=status,
            error=last_error,
        )
        return {
            "expected": expected,
            "target": max_dels,
            "deleted": total_deleted,
            "status": status,
            "error": last_error,
        }
