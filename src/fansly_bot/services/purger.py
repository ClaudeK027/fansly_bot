# src/fansly_bot/services/purger.py
"""Purge intelligente — implementation du pseudocode de la Phase 2.

Decision a double facteur :
  un post est supprime SI age > seuil ET legende contient un mot-cle.
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import pendulum
import structlog
from playwright.async_api import Locator, Page, TimeoutError as PWTimeout

from ..browser.humanizer import Humanizer
from ..browser.session import BrowserSession
from ..config import Settings
from ..infra.retry import RetryPolicies
from ..infra.state import PurgeRunReport, StateStore
from ..selectors import Sel
from .auth import AuthError, AuthService

log = structlog.get_logger("services.purger")


# ---------- Decision ----------

KEEP = "KEEP"
DELETE = "DELETE"
SKIPPED = "SKIPPED"
DELETED = "DELETED"
UNREADABLE = "UNREADABLE"


@dataclass
class Candidate:
    post_id: str
    age_days: float
    caption_excerpt: str
    reason: str


@dataclass
class Decision:
    action: str
    reason: str
    post_created_at: Optional[datetime]
    caption_excerpt: str


# ---------- Service ----------

class PurgerService:
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
        # Le worker peut injecter une closure pour permettre l'annulation
        # propre (verifiee entre les scrolls et les suppressions).
        self._cancel_check = lambda: False
        # Plage de dates absolue (override du seuil d'age si fournie)
        self._date_window: Optional[tuple[datetime, datetime]] = None

    # ---------- API publique ----------

    async def run(self) -> None:
        cfg = self._settings.purge
        if not cfg.enabled:
            log.info("purger_disabled")
            return

        started_at = datetime.now(timezone.utc)
        candidates: list[Candidate] = []
        examined = 0
        deleted = 0
        scroll_cap_hit = False
        max_deletions_hit = False

        async with self._session.use():
            try:
                await self._auth.ensure_logged_in()
                page = await self._session.page()
                await self._goto_profile(page)

                # Decouverte + suppression inline en un seul passage
                (
                    examined,
                    candidates,
                    scroll_cap_hit,
                    max_deletions_hit,
                    deleted,
                ) = await self._discover(page)

                if cfg.dry_run:
                    log.info("purger_dry_run_summary", candidates=len(candidates))

            except AuthError as e:
                log.error("purger_auth_failed", error=str(e))
            except Exception as e:  # noqa: BLE001
                log.error("purger_fatal", error=str(e), exc_info=True)
                await self._dump_artifact("purger_fatal")

        # Rapport final
        finished_at = datetime.now(timezone.utc)
        report = PurgeRunReport(
            started_at=started_at,
            finished_at=finished_at,
            examined=examined,
            candidates=len(candidates),
            deleted=deleted,
            skipped=len(candidates) - deleted,
            scroll_cap_hit=scroll_cap_hit,
            max_deletions_hit=max_deletions_hit,
            dry_run=cfg.dry_run,
        )
        run_id = self._state.record_purge_run(report)
        log.info(
            "purger_run_summary",
            run_id=run_id,
            duration_s=(finished_at - started_at).total_seconds(),
            examined=examined,
            candidates=len(candidates),
            deleted=deleted,
            dry_run=cfg.dry_run,
        )

    async def delete_post_by_fansly_id(
        self,
        fansly_post_id: str,
        *,
        page: "Page",
        cancel_check=None,
        scroll_cap: int | None = None,
        require_fyp: bool = True,
    ) -> dict:
        """Supprime UN post specifique identifie par son ID Fansly officiel
        (celui retourne par POST /api/v1/post et persiste en BDD via
        record_media_published.fansly_post_id).

        Utilise par CycleRotator pour la rotation per-media : APRES avoir
        republie un media au cycle N+1, on supprime sa version du cycle N.

        IMPORTANT — gestion du verrou session :
        L'appelant DOIT deja detenir la session (avoir ouvert
        `async with self._session.use():`) et nous passer la `page` qu'il
        a recuperee. Cette methode NE re-acquiert PAS la session — sinon
        l'asyncio.Lock non-reentrant de BrowserSession deadlockerait
        immediatement. Cette contrainte est verifiee par le caller
        (UploaderService.publish_next).

        GARDE-FOU `require_fyp` :
        Si True (default), apres relocalisation du post on lit sa caption
        et on EXIGE qu'elle contienne '#fyp' (signature systematique du
        bot, cf. CycleCleaner). Si la caption ne contient pas '#fyp', on
        ABORT le delete avec status='guard_fyp' + log critical — defense
        en profondeur contre un faux positif de capture Phase A qui
        pointerait vers un post manuel du createur.

        `scroll_cap` :
        Si None, utilise self._settings.purge.scroll_safety_cap. Sinon
        utilise la valeur passee (ex. publishing.rotation_scroll_safety_cap
        pour decoupler la rotation de la purge classique).

        Retour : dict avec :
          - status : 'deleted' | 'not_found' | 'cancelled' | 'failed' | 'guard_fyp'
          - fansly_post_id : echo du parametre
          - examined : nombre d'items DOM examines
          - error : str si status='failed'
        """
        if cancel_check is not None:
            self._cancel_check = cancel_check
        target_id = str(fansly_post_id).strip()
        if not target_id:
            return {
                "status": "failed",
                "fansly_post_id": fansly_post_id,
                "examined": 0,
                "error": "empty_fansly_post_id",
            }

        cap = scroll_cap if scroll_cap is not None else self._settings.purge.scroll_safety_cap

        # La page est fournie par l'appelant (session deja acquise) — on
        # navigue vers le profil mais on ne re-acquiert PAS le verrou.
        try:
            await self._goto_profile(page)
        except Exception as e:  # noqa: BLE001
            log.error("rotator_navigation_failed", error=str(e), exc_info=True)
            return {
                "status": "failed", "fansly_post_id": target_id,
                "examined": 0, "error": f"nav:{e}",
            }

        return await self._scan_and_delete(
            page, target_id, cap=cap, require_fyp=require_fyp,
        )

    async def _scan_and_delete(
        self, page: "Page", target_id: str, *, cap: int, require_fyp: bool,
    ) -> dict:
        """Coeur de delete_post_by_fansly_id : scrolle le feed du profil
        en cherchant le post cible, applique le garde-fou #fyp si
        require_fyp=True, supprime via _delete_one."""
        last_height = 0
        stagnant_scrolls = 0
        examined_total = 0

        while examined_total < cap:
            if self._cancel_check():
                log.info("rotator_cancelled", fansly_post_id=target_id)
                return {
                    "status": "cancelled",
                    "fansly_post_id": target_id,
                    "examined": examined_total,
                }

            item = await self._relocate_by_fansly_id(page, target_id)
            if item is not None:
                log.info(
                    "rotator_target_found",
                    fansly_post_id=target_id, examined=examined_total,
                )

                # Garde-fou : on n'effectue le delete que si la caption
                # du post cible contient #fyp (signature bot). Protege
                # contre un faux positif de capture Phase A qui pointerait
                # vers un post manuel du createur.
                if require_fyp:
                    try:
                        caption = await self._extract_caption(item)
                    except Exception:  # noqa: BLE001
                        caption = ""
                    caption_norm = _normalize(caption)
                    if "#fyp" not in caption_norm:
                        log.critical(
                            "rotator_guard_fyp_blocked",
                            fansly_post_id=target_id,
                            caption_excerpt=caption[:80],
                            hint="post lacks #fyp signature; refusing to delete",
                        )
                        await self._dump_artifact(
                            f"rotator_guard_blocked_{_safe(target_id)}"
                        )
                        return {
                            "status": "guard_fyp",
                            "fansly_post_id": target_id,
                            "examined": examined_total,
                        }

                try:
                    async for attempt in self._retries.network():
                        with attempt:
                            await self._delete_one(page, item)
                    log.info("rotator_post_deleted", fansly_post_id=target_id)
                    return {
                        "status": "deleted",
                        "fansly_post_id": target_id,
                        "examined": examined_total,
                    }
                except Exception as e:  # noqa: BLE001
                    log.error(
                        "rotator_delete_failed",
                        fansly_post_id=target_id,
                        error=str(e), exc_info=True,
                    )
                    await self._dump_artifact(
                        f"rotator_delete_failed_{_safe(target_id)}"
                    )
                    return {
                        "status": "failed",
                        "fansly_post_id": target_id,
                        "examined": examined_total,
                        "error": f"delete:{type(e).__name__}",
                    }

            try:
                items = Sel.feed_items(page)
                examined_total = await items.count()
            except Exception:  # noqa: BLE001
                pass

            await self._humanizer.scroll_human(page, direction="down")
            await self._humanizer.short_pause()
            try:
                new_height = await page.evaluate("document.body.scrollHeight")
            except Exception:  # noqa: BLE001
                new_height = last_height
            if new_height == last_height:
                stagnant_scrolls += 1
                if stagnant_scrolls >= 2:
                    log.info(
                        "rotator_feed_bottom_reached",
                        fansly_post_id=target_id, examined=examined_total,
                    )
                    break
            else:
                stagnant_scrolls = 0
                last_height = new_height

        log.warning(
            "rotator_post_not_found",
            fansly_post_id=target_id,
            examined=examined_total,
            scroll_cap_hit=(examined_total >= cap),
        )
        return {
            "status": "not_found",
            "fansly_post_id": target_id,
            "examined": examined_total,
        }

    # ---------- navigation profil ----------

    async def _goto_profile(self, page: Page) -> None:
        cfg = self._settings
        # Priorite de resolution du chemin de profil :
        #  1. cfg.purge.profile_path : valeur explicite du YAML (utile en
        #     dev pour overrider sans toucher au .env).
        #  2. cfg.secrets.profile_slug : env / .env — c est la voie standard
        #     pour identifier le profil de l utilisateur (FANSLY_PROFILE_SLUG).
        #     Ne fuit pas dans le repo car .env est gitignored.
        #  3. cfg.secrets.username : fallback historique. Peu fiable car le
        #     username est un email, pas un slug Fansly. Mais on le garde
        #     pour ne pas casser une vieille config qui en dependrait.
        profile_path = cfg.purge.profile_path
        if not profile_path:
            slug = cfg.secrets.profile_slug
            if slug:
                profile_path = f"/{slug.strip('/')}"
            else:
                username = cfg.secrets.username.get_secret_value()
                profile_path = f"/{username}"
                log.warning(
                    "purger_profile_slug_missing",
                    fallback="username",
                    hint="defini FANSLY_PROFILE_SLUG dans .env pour fiabiliser",
                )
        url = cfg.auth.base_url.rstrip("/") + profile_path
        log.info("purger_navigating", url=url)

        async for attempt in self._retries.network():
            with attempt:
                await page.goto(url, wait_until="domcontentloaded")

        await self._humanizer.long_pause()
        items = Sel.feed_items(page)
        try:
            await items.first.wait_for(state="visible", timeout=15_000)
        except PWTimeout:
            log.warning("purger_no_feed_items_visible")
            await self._dump_artifact("no_feed_items_visible")

    # ---------- decouverte ----------

    async def _discover(
        self, page: Page
    ) -> tuple[int, list[Candidate], bool, bool, int]:
        """Decouverte + suppression inline (sauf dry_run).

        Renvoie : (examined, candidates, scroll_cap_hit, max_deletions_hit, deleted_inline)
        """
        cfg = self._settings.purge
        seen_ids: set[str] = set()
        candidates: list[Candidate] = []
        consecutive_already_decided = 0
        last_height = 0
        stagnant_scrolls = 0
        examined = 0
        scroll_cap_hit = False
        max_deletions_hit = False
        deleted_inline = 0

        loop_count = 0
        while examined < cfg.scroll_safety_cap:
            loop_count += 1
            items = Sel.feed_items(page)
            count = await items.count()

            # Debug : visibilite + texte du premier element s'il y en a
            if loop_count == 1 or count == 0 and stagnant_scrolls > 0:
                visible_count = 0
                for i in range(min(count, 5)):
                    try:
                        if await items.nth(i).is_visible(timeout=500):
                            visible_count += 1
                    except Exception:  # noqa: BLE001
                        pass
                log.info(
                    "purger_loop_iter",
                    iter=loop_count,
                    items_count=count,
                    items_visible_sample=visible_count,
                    scroll_height=last_height,
                )
                if count == 0:
                    await self._dump_artifact(f"discover_empty_iter_{loop_count}")

            for i in range(count):
                item = items.nth(i)
                try:
                    if not await item.is_visible():
                        continue
                except Exception:  # noqa: BLE001
                    continue

                post_id = await self._extract_post_id(item)
                if not post_id:
                    # Pas d'id stable : on utilise le hash comme fallback et on
                    # CONTINUE l'evaluation (decision basee sur date + caption).
                    post_id = await self._hash_fallback_id(item)
                if post_id in seen_ids:
                    continue
                seen_ids.add(post_id)
                examined += 1

                # Skip si decide recemment
                row = self._state.get_post(post_id)
                if row and row.decision in (KEEP, DELETED):
                    last_exam = row.last_examined_at
                    age_h = (datetime.now(timezone.utc) - last_exam).total_seconds() / 3600.0
                    if age_h < 12:
                        consecutive_already_decided += 1
                        continue
                consecutive_already_decided = 0

                decision = await self._evaluate(item, post_id)
                self._state.upsert_post(
                    post_id,
                    decision.action,
                    decision.reason,
                    decision.post_created_at,
                    decision.caption_excerpt[:200],
                )

                if decision.action == DELETE:
                    # Annulation utilisateur entre 2 suppressions ?
                    if self._cancel_check():
                        log.info("purger_cancelled_between_deletions")
                        return examined, candidates, scroll_cap_hit, max_deletions_hit, deleted_inline
                    candidate = Candidate(
                        post_id=post_id,
                        age_days=self._days_since(decision.post_created_at),
                        caption_excerpt=decision.caption_excerpt[:200],
                        reason=decision.reason,
                    )
                    candidates.append(candidate)

                    # Suppression INLINE (sauf dry_run) : on a deja l'item Locator,
                    # plus besoin de relocaliser par id instable.
                    if not cfg.dry_run:
                        try:
                            async for attempt in self._retries.network():
                                with attempt:
                                    await self._delete_one(page, item)
                            self._state.mark_post_deleted(
                                post_id, candidate.reason + " | confirmed_inline"
                            )
                            deleted_inline += 1
                            log.info(
                                "purger_post_deleted",
                                post_id=post_id,
                                age_days=candidate.age_days,
                                reason=candidate.reason,
                                deleted_count=deleted_inline,
                            )
                            # Pause humaine entre suppressions
                            await self._humanizer.between_destructive_actions()
                            # Apres suppression, le DOM a change : on sort de la boucle
                            # interne et on re-evalue items.count() au prochain tour.
                            break
                        except Exception as e:  # noqa: BLE001
                            log.error("purger_delete_failed", post_id=post_id, error=str(e))
                            self._state.upsert_post(
                                post_id, SKIPPED,
                                f"delete_failed:{type(e).__name__}", None, None,
                            )
                            await self._dump_artifact(f"delete_failed_{_safe(post_id)}")

                    if len(candidates) >= cfg.max_deletions_per_run:
                        log.info("purger_max_deletions_cap_hit", cap=cfg.max_deletions_per_run)
                        max_deletions_hit = True
                        return examined, candidates, scroll_cap_hit, max_deletions_hit, deleted_inline

            # Arret heuristiques
            if consecutive_already_decided >= 15:
                log.info("purger_decided_streak", streak=consecutive_already_decided)
                break

            # Log progression toutes les 20 evaluations
            if examined > 0 and examined % 20 == 0:
                log.info(
                    "purger_progress",
                    examined=examined,
                    candidates=len(candidates),
                    seen_unique=len(seen_ids),
                )

            # Annulation utilisateur ?
            if self._cancel_check():
                log.info("purger_cancelled_between_scrolls")
                break

            # Scroll humain
            await self._humanizer.scroll_human(page, direction="down")
            await self._humanizer.short_pause()
            new_height = await page.evaluate("document.body.scrollHeight")
            if new_height == last_height:
                stagnant_scrolls += 1
                if stagnant_scrolls >= 2:
                    log.info("purger_feed_bottom_reached")
                    break
            else:
                stagnant_scrolls = 0
                last_height = new_height

        if examined >= cfg.scroll_safety_cap:
            scroll_cap_hit = True
            log.warning("purger_scroll_safety_cap_hit", cap=cfg.scroll_safety_cap)

        # Detection d'anomalie : > 50% des examines marques DELETE
        if examined > 10 and len(candidates) / examined > 0.5:
            log.critical(
                "purger_suspicious_match_rate",
                examined=examined,
                candidates=len(candidates),
                action="aborting_to_protect_account",
            )
            return examined, [], scroll_cap_hit, max_deletions_hit, deleted_inline

        return examined, candidates, scroll_cap_hit, max_deletions_hit, deleted_inline

    # ---------- evaluation ----------

    async def _evaluate(self, item: Locator, post_id: str) -> Decision:
        cfg = self._settings.purge

        post_date_utc = await self._extract_date(item)
        if post_date_utc is None:
            self._state.upsert_post(post_id, UNREADABLE, "no_date", None, None)
            return Decision(KEEP, "unreadable_date", None, "")

        caption_raw = await self._extract_caption(item)
        caption_norm = _normalize(caption_raw)

        # Critere temporel : fenetre de dates si fournie, sinon seuil d'age
        if self._date_window is not None:
            start, end = self._date_window
            in_window = start <= post_date_utc <= end
            time_match = in_window
            time_reason = (
                f"in_window[{start.date()}..{end.date()}]"
                if in_window else f"outside_window[{start.date()}..{end.date()}]"
            )
        else:
            age_days = (pendulum.now("UTC") - pendulum.instance(post_date_utc)).total_days()
            time_match = age_days > cfg.age_threshold_days
            time_reason = (
                f"age={age_days:.1f}d>seuil" if time_match else f"age={age_days:.1f}d<=seuil"
            )

        keywords_norm = [_normalize(k) for k in cfg.keywords if k and k.strip()]
        # Pas de mot-cle fourni → on ne filtre PAS sur la legende. Utile pour
        # une purge purement temporelle (fenetre date a date). Le critere
        # devient alors uniquement le critere de temps.
        if not keywords_norm:
            keyword_hit = True
        elif cfg.keyword_match_mode == "any":
            keyword_hit = any(k in caption_norm for k in keywords_norm)
        else:
            keyword_hit = all(k in caption_norm for k in keywords_norm)

        if time_match and keyword_hit:
            return Decision(DELETE, f"{time_reason} & keyword_hit", post_date_utc, caption_raw)
        if not time_match and keyword_hit:
            return Decision(KEEP, f"keyword_hit_but_{time_reason}", post_date_utc, caption_raw)
        if time_match and not keyword_hit:
            return Decision(KEEP, f"{time_reason}_but_no_keyword", post_date_utc, caption_raw)
        return Decision(KEEP, f"{time_reason}_and_no_keyword", post_date_utc, caption_raw)

    # ---------- extractions ----------

    async def _extract_post_id(self, item: Locator) -> Optional[str]:
        # 1) attribut data-feed-item-id ou data-post-id (get_attribute rapide pour attrs absents)
        for attr in ("data-feed-item-id", "data-post-id", "data-id", "id"):
            try:
                v = await item.get_attribute(attr, timeout=1000)
                if v:
                    return f"{attr}:{v.strip()}"
            except Exception:  # noqa: BLE001
                continue

        # 2) lien permalien — verifier l'existence d'abord
        try:
            link = item.locator("a[href*='/post/']")
            if await link.count() > 0:
                href = await link.first.get_attribute("href", timeout=1000)
                if href and "/post/" in href:
                    return f"href:{href.split('/post/')[-1].split('?')[0]}"
        except Exception:  # noqa: BLE001
            pass

        return None

    async def _hash_fallback_id(self, item: Locator) -> str:
        try:
            html = await item.inner_html()
        except Exception:  # noqa: BLE001
            html = ""
        digest = hashlib.sha1(html.encode("utf-8", errors="ignore")).hexdigest()[:16]
        return f"hash:{digest}"

    async def _extract_date(self, item: Locator) -> Optional[datetime]:
        # 1) attribut datetime sur <time> — check d'existence d'abord pour eviter timeout 15s
        try:
            time_loc = item.locator("time[datetime]")
            if await time_loc.count() > 0:
                iso = await time_loc.first.get_attribute("datetime", timeout=2000)
                if iso:
                    return pendulum.parse(iso).in_tz("UTC").naive().replace(tzinfo=timezone.utc)
        except Exception:  # noqa: BLE001
            pass

        # 2) texte relatif — vise .feed-item-timestamp (Fansly)
        try:
            text_loc = Sel.feed_item_time(item)
            if await text_loc.count() > 0:
                raw = (await text_loc.inner_text(timeout=2000)).strip()
                if raw:
                    parsed = _parse_relative(raw)
                    if parsed:
                        return parsed
        except Exception:  # noqa: BLE001
            pass

        return None

    async def _extract_caption(self, item: Locator) -> str:
        try:
            cap = Sel.feed_item_caption(item)
            if await cap.count() > 0:
                return (await cap.inner_text(timeout=2000)).strip()
        except Exception:  # noqa: BLE001
            pass
        return ""

    # ---------- execution suppressions ----------

    async def _execute_deletions(self, page: Page, candidates: list[Candidate]) -> int:
        if not candidates:
            return 0

        # Petit shuffle local pour eviter suppression strictement chronologique
        candidates = _shuffle_partial(candidates, window=3)
        deleted = 0

        for cand in candidates:
            try:
                item = await self._relocate_by_id(page, cand.post_id)
                if item is None:
                    log.warning("purger_post_vanished", post_id=cand.post_id)
                    self._state.upsert_post(
                        cand.post_id, SKIPPED, "not_found_at_delete_time", None, None
                    )
                    continue

                async for attempt in self._retries.network():
                    with attempt:
                        await self._delete_one(page, item)

                self._state.mark_post_deleted(
                    cand.post_id, cand.reason + " | confirmed"
                )
                log.info(
                    "purger_post_deleted",
                    post_id=cand.post_id,
                    age_days=cand.age_days,
                    reason=cand.reason,
                )
                deleted += 1
            except Exception as e:  # noqa: BLE001
                log.error("purger_delete_failed", post_id=cand.post_id, error=str(e))
                self._state.upsert_post(
                    cand.post_id, SKIPPED, f"delete_failed:{type(e).__name__}", None, None
                )
                await self._dump_artifact(f"delete_failed_{_safe(cand.post_id)}")

            await self._humanizer.between_destructive_actions()

        return deleted

    async def _delete_one(self, page: Page, item: Locator) -> None:
        # 0) Ferme tout dropdown / overlay laisse ouvert par une suppression
        # precedente (Escape + clic neutre).
        await self._dismiss_overlays(page)

        await item.scroll_into_view_if_needed()
        await self._humanizer.short_pause()

        # Capture du compte AVANT le clic Confirm. On utilise un Locator parametre
        # par index (items.nth(i)) pour le clic, mais pour valider la suppression
        # on s appuie sur le DELTA du nombre total de posts visibles dans le feed.
        # C est plus robuste qu un wait_for(state="detached") sur item, qui
        # echoue toujours car items.nth(i) re-resout vers le post N+1 apres
        # reorganisation du DOM.
        feed_items = Sel.feed_items(page)
        try:
            n_before = await feed_items.count()
        except Exception:  # noqa: BLE001
            n_before = -1  # mesure indisponible, on degrade silencieusement

        # 1) Ouvrir le menu d'options du post
        menu = Sel.feed_item_meta_menu(item)
        await menu.wait_for(state="visible", timeout=5000)
        await self._humanizer.hover_then_click(menu)
        await self._humanizer.short_pause()

        # 2) Cliquer "Delete Post" — uniquement parmi les elements visibles
        # (sinon on resout sur un menuitem cache d'un autre dropdown).
        delete_entry = page.locator(
            "div.dropdown-item:visible, [role='menuitem']:visible"
        ).filter(has_text=re.compile(r"Delete\s*Post", re.I)).first
        try:
            await delete_entry.wait_for(state="visible", timeout=8000)
        except PWTimeout:
            # Le menu n'a peut-etre pas ouvert : on retente une fois
            log.warning("purger_delete_entry_invisible_retry")
            await self._dismiss_overlays(page)
            await self._humanizer.short_pause()
            await menu.wait_for(state="visible", timeout=3000)
            await self._humanizer.hover_then_click(menu)
            await delete_entry.wait_for(state="visible", timeout=5000)
        await self._humanizer.hover_then_click(delete_entry)
        await self._humanizer.short_pause()

        # 3) Confirmer
        confirm = Sel.confirm_yes(page)
        await confirm.wait_for(state="visible", timeout=5000)
        await self._humanizer.hover_then_click(confirm)
        await self._humanizer.short_pause()

        # 4) Validation : on attend que le nombre de posts dans le feed
        # diminue d au moins 1. Plus robuste que wait_for(state="detached")
        # sur un Locator parametre par index, qui echoue systematiquement.
        if n_before >= 0:
            deadline = time.monotonic() + 10.0
            ok = False
            while time.monotonic() < deadline:
                try:
                    n_after = await feed_items.count()
                except Exception:  # noqa: BLE001
                    n_after = n_before
                if n_after < n_before:
                    ok = True
                    break
                await asyncio.sleep(0.3)
            if not ok:
                # Vraie alerte : le delta n est pas detecte. Possible que le
                # post ait deja ete supprime (n_before incluait deja l absence)
                # ou que Fansly tarde a rafraichir. On loggue mais on ne fait
                # PAS exploser : la prochaine iteration relira le DOM.
                log.warning(
                    "purger_count_did_not_decrease_after_delete",
                    n_before=n_before,
                )
        else:
            log.debug("purger_delete_count_check_skipped_no_baseline")

        # 5) Cleanup : on force la fermeture de tout reste de modale/dropdown
        await self._dismiss_overlays(page)

    async def _dismiss_overlays(self, page: Page) -> None:
        """Ferme dropdowns et modales en pressant Escape + clic neutre."""
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.2)
            # Re-Escape au cas ou une modale est par-dessus le dropdown
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.2)
        except Exception:  # noqa: BLE001
            pass

    async def _relocate_by_id(self, page: Page, post_id: str) -> Optional[Locator]:
        """Relocalise un post par son id stable (le DOM a pu bouger pendant la decouverte)."""
        prefix, _, value = post_id.partition(":")
        items = Sel.feed_items(page)
        count = await items.count()
        for i in range(count):
            it = items.nth(i)
            try:
                if prefix.startswith("data-") or prefix == "id":
                    v = await it.get_attribute(prefix)
                    if v and v.strip() == value:
                        return it
                elif prefix == "href":
                    link = it.locator("a[href*='/post/']").first
                    href = await link.get_attribute("href")
                    if href:
                        # Match STRICT : extraire l'id segment exact entre
                        # /post/ et le prochain separateur (/, ?, #, fin).
                        # Sans ca, value='123' matcherait '/post/91234567'
                        # par substring => suppression du MAUVAIS post.
                        m = re.search(r"/post/([^/?#]+)", href)
                        if m and m.group(1) == value:
                            return it
                elif prefix == "hash":
                    html = await it.inner_html()
                    digest = hashlib.sha1(html.encode("utf-8", errors="ignore")).hexdigest()[:16]
                    if digest == value:
                        return it
            except Exception:  # noqa: BLE001
                continue
        return None

    async def _relocate_by_fansly_id(
        self, page: Page, fansly_id: str,
    ) -> Optional[Locator]:
        """Cherche un feed item dont l'ID Fansly correspond, en essayant
        plusieurs strategies de matching dans l'ordre :
          1. Attributs DOM : data-feed-item-id, data-post-id, data-id, id
             (le fansly_post_id capture par l'API correspond TYPIQUEMENT
             a l'un de ces attributs).
          2. Permalien : a[href*='/post/'] avec match strict du segment.

        Plus robuste que _relocate_by_id('href:<id>') qui forcait la
        branche href seule — le cleaner batch trouvait deja les posts
        via les attributs DOM, on suit la meme strategie ici.

        Logue l'attribut qui a permis le match pour observabilite.
        """
        items = Sel.feed_items(page)
        count = await items.count()
        attrs_to_try = ("data-feed-item-id", "data-post-id", "data-id", "id")
        for i in range(count):
            it = items.nth(i)
            try:
                # 1) Essai par attribut DOM
                for attr in attrs_to_try:
                    try:
                        v = await it.get_attribute(attr, timeout=500)
                    except Exception:  # noqa: BLE001
                        v = None
                    if v and v.strip() == fansly_id:
                        log.debug(
                            "rotator_match_via_attr",
                            attr=attr, fansly_id=fansly_id,
                        )
                        return it
                # 2) Fallback par permalien (match strict du segment)
                link = it.locator("a[href*='/post/']").first
                if await link.count() > 0:
                    href = await link.get_attribute("href", timeout=500)
                    if href:
                        m = re.search(r"/post/([^/?#]+)", href)
                        if m and m.group(1) == fansly_id:
                            log.debug(
                                "rotator_match_via_href",
                                fansly_id=fansly_id,
                            )
                            return it
            except Exception:  # noqa: BLE001
                continue
        return None

    # ---------- utilitaires ----------

    @staticmethod
    def _days_since(dt: Optional[datetime]) -> float:
        if dt is None:
            return -1.0
        return (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0

    async def _dump_artifact(self, label: str) -> None:
        try:
            page = await self._session.page()
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            out = self._settings.paths.artifacts_dir / "purge" / f"{stamp}_{label}"
            out.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(out / "screen.png"), full_page=True)
            (out / "page.html").write_text(await page.content(), encoding="utf-8")
            log.info("purger_artifact_saved", path=str(out))
        except Exception as e:  # noqa: BLE001
            log.warning("purger_artifact_dump_failed", error=str(e))


# ---------- helpers libres ----------

def _normalize(text: str) -> str:
    """Casefold + suppression diacritiques + collapse espaces."""
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", text)
    no_marks = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", no_marks.casefold()).strip()


_REL_RE = re.compile(
    r"^\s*(\d+)\s*(s|sec|secs|second|seconds|"
    r"m|min|mins|minute|minutes|"
    r"h|hr|hrs|hour|hours|"
    r"d|day|days|"
    r"w|wk|wks|week|weeks|"
    r"mo|mon|month|months|"
    r"y|yr|yrs|year|years)\s*(ago)?\s*$",
    re.IGNORECASE,
)


def _parse_relative(text: str) -> Optional[datetime]:
    """Parse texte relatif (`3d ago`, `yesterday`...) ou date absolue (`Mar 14`)."""
    if not text:
        return None
    t = text.strip().lower()

    if t in ("just now", "now"):
        return pendulum.now("UTC").naive().replace(tzinfo=timezone.utc)
    if t == "yesterday":
        return (pendulum.now("UTC").subtract(days=1)).naive().replace(tzinfo=timezone.utc)
    if t.startswith("today"):
        return pendulum.now("UTC").start_of("day").naive().replace(tzinfo=timezone.utc)

    m = _REL_RE.match(t)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        kw = _unit_to_pendulum(unit, n)
        if kw is not None:
            return pendulum.now("UTC").subtract(**kw).naive().replace(tzinfo=timezone.utc)

    # Date absolue (Mar 14, Mar 14 2024)
    for fmt in ("MMM D, YYYY", "MMM D YYYY", "MMM D"):
        try:
            dt = pendulum.from_format(text.strip(), fmt, tz="UTC")
            if dt > pendulum.now("UTC"):
                dt = dt.subtract(years=1)
            return dt.naive().replace(tzinfo=timezone.utc)
        except Exception:  # noqa: BLE001
            continue

    return None


def _unit_to_pendulum(unit: str, n: int) -> Optional[dict]:
    table = {
        "s": "seconds", "sec": "seconds", "secs": "seconds", "second": "seconds", "seconds": "seconds",
        "m": "minutes", "min": "minutes", "mins": "minutes", "minute": "minutes", "minutes": "minutes",
        "h": "hours", "hr": "hours", "hrs": "hours", "hour": "hours", "hours": "hours",
        "d": "days", "day": "days", "days": "days",
        "w": "weeks", "wk": "weeks", "wks": "weeks", "week": "weeks", "weeks": "weeks",
        "mo": "months", "mon": "months", "month": "months", "months": "months",
        "y": "years", "yr": "years", "yrs": "years", "year": "years", "years": "years",
    }
    key = table.get(unit)
    if not key:
        return None
    return {key: n}


def _shuffle_partial(items: list, window: int = 3) -> list:
    """Petit melange local : on shuffle des fenetres successives."""
    out = list(items)
    for i in range(0, len(out), window):
        chunk = out[i:i + window]
        random.shuffle(chunk)
        out[i:i + window] = chunk
    return out


def _safe(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", s)[:40]
