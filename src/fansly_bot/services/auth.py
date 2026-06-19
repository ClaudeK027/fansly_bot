# src/fansly_bot/services/auth.py
"""Authentification Fansly avec gestion du premier login interactif.

Approche :
  - `ensure_logged_in()` est appele au demarrage et periodiquement. Il navigue
    vers /home et verifie qu'on n'est pas redirige vers /login.
  - `interactive_setup()` est invoque par la sous-commande `setup-auth`. Il
    relance la session en headless=False, fait le login, attend que l'humain
    resolve le 2FA/captcha, puis valide en atterrissant sur /home.
  - Le contexte persistant capture cookies et localStorage : tout
    redemarrage ulterieur reprend la session sans repasser par le login.
"""

from __future__ import annotations

import asyncio
import time

import structlog
from playwright.async_api import TimeoutError as PWTimeout

from ..browser.humanizer import Humanizer
from ..browser.session import BrowserSession
from ..config import Settings
from ..selectors import Sel

log = structlog.get_logger("services.auth")


class AuthError(RuntimeError):
    """Probleme d'authentification non-recuperable sans intervention humaine."""


class AuthService:
    def __init__(self, settings: Settings, session: BrowserSession, humanizer: Humanizer) -> None:
        self._settings = settings
        self._session = session
        self._humanizer = humanizer
        # Timestamp monotonique du dernier check session reussi. Sert au cache
        # de ensure_logged_in() : on ne refait pas un page.goto(/home) a chaque
        # publication, on le fait au plus une fois par session_check_interval_minutes.
        # Avant ce fix, ensure_logged_in etait appele a CHAQUE publish_next() —
        # explosion de la surface d'exposition au hang sur SPA Angular zombie.
        self._last_check_monotonic: float = 0.0

    @property
    def _home_url(self) -> str:
        return self._settings.auth.base_url.rstrip("/") + self._settings.auth.home_path

    @property
    def _login_url(self) -> str:
        return self._settings.auth.base_url.rstrip("/") + "/login"

    # ---------- verification rapide ----------

    async def ensure_logged_in(self, *, force: bool = False) -> None:
        """Verifie la session ; leve AuthError si on est rejete sur /login.

        CACHE : sauf si force=True, on skip le check si le dernier verif a
        moins de `session_check_interval_minutes` (config.yaml, default 60min).
        Sans ce cache, ensure_logged_in() faisait un page.goto(/home) a CHAQUE
        publish_next() — explosion de la surface au hang sur SPA Angular zombie.

        DEFENSE EN PROFONDEUR : le page.goto est wrappe dans asyncio.wait_for
        applicatif, en plus du navigation_timeout Playwright. Sur un transport
        CDP gele (renderer zombie), le timeout Playwright peut ne pas se
        declencher car son comptage depend du CDP lui-meme. Notre wait_for
        externe coupe quoi qu'il arrive.
        """
        # Cache : skip si dernier check assez recent
        interval_s = self._settings.auth.session_check_interval_minutes * 60
        now = time.monotonic()
        if not force and (now - self._last_check_monotonic) < interval_s:
            log.debug(
                "auth_check_cached_skip",
                last_check_age_s=round(now - self._last_check_monotonic, 1),
                interval_s=interval_s,
            )
            return

        page = await self._session.page()
        log.info("auth_check_starting", url=self._home_url)
        # navigation_timeout_ms est applique par Playwright (default 30s) ; on
        # ajoute 5s de marge et un wait_for asyncio externe au cas ou Playwright
        # ne respecterait pas le timeout (CDP gele).
        nav_timeout_s = self._settings.browser.navigation_timeout_ms / 1000.0
        try:
            await asyncio.wait_for(
                page.goto(self._home_url, wait_until="domcontentloaded"),
                timeout=nav_timeout_s + 5.0,
            )
        except (PWTimeout, asyncio.TimeoutError):
            raise AuthError("Navigation vers /home a expire")

        await self._humanizer.short_pause()
        await self._dismiss_overlays(page)

        current = page.url
        if "/login" in current:
            log.warning("auth_session_missing", current_url=current)
            raise AuthError(
                "Session Fansly absente ou expiree. Lance "
                "`python -m fansly_bot setup-auth` une fois pour t'authentifier."
            )
        # Mise a jour du cache : prochain skip pendant interval_s
        self._last_check_monotonic = time.monotonic()
        log.info("auth_session_ok", url=current)

    # ---------- premier login interactif ----------

    async def interactive_setup(self) -> None:
        """Sous-commande setup-auth : laisse l'humain finaliser le login."""
        page = await self._session.page()
        timeout_s = self._settings.auth.interactive_login_timeout_s

        log.info("auth_interactive_starting", base_url=self._settings.auth.base_url)
        await page.goto(self._settings.auth.base_url, wait_until="domcontentloaded")
        await self._humanizer.long_pause()
        await self._dismiss_overlays(page)

        # Si deja sur /home, c'est bon.
        if self._is_logged_in_url(page.url):
            log.info("auth_already_logged_in", url=page.url)
            return

        # Sinon on declenche le formulaire et on pre-remplit les champs pour
        # aider l'humain. Le 2FA / captcha eventuel sera resolu manuellement.
        await self._try_open_login_form(page)
        await self._fill_credentials_if_visible(page)

        log.info(
            "auth_interactive_waiting_for_home",
            timeout_s=timeout_s,
            hint="Resous 2FA / captcha dans la fenetre Chromium ouverte.",
        )

        try:
            await page.wait_for_url(
                lambda url: self._is_logged_in_url(url),
                timeout=timeout_s * 1000,
            )
        except PWTimeout:
            raise AuthError(
                f"Login interactif non termine apres {timeout_s}s. "
                "Relance `setup-auth` et reprends."
            )

        await self._dismiss_overlays(page)
        log.info("auth_interactive_success", url=page.url)

    # ---------- helpers ----------

    def _is_logged_in_url(self, url: str) -> bool:
        return self._settings.auth.home_path in url and "/login" not in url

    async def _try_open_login_form(self, page) -> None:
        # Age gate eventuel
        try:
            btn = Sel.age_gate_enter(page)
            if await btn.is_visible(timeout=2000):
                await self._humanizer.hover_then_click(btn)
                await self._humanizer.short_pause()
        except Exception:  # noqa: BLE001
            pass

        try:
            btn = Sel.header_login_button(page)
            if await btn.is_visible(timeout=3000):
                await self._humanizer.hover_then_click(btn)
                await self._humanizer.short_pause()
        except Exception:  # noqa: BLE001
            pass

    async def _fill_credentials_if_visible(self, page) -> None:
        try:
            u = Sel.username_input(page)
            p = Sel.password_input(page)
            if await u.is_visible(timeout=4000):
                await self._humanizer.type_humanly(
                    u, self._settings.secrets.username.get_secret_value()
                )
                await self._humanizer.short_pause()
                await self._humanizer.type_humanly(
                    p, self._settings.secrets.password.get_secret_value()
                )
                await self._humanizer.short_pause()
                # On ne clique PAS sur Sign in ici : c'est a l'humain de valider
                # apres avoir verifie / resolu les challenges.
        except Exception as e:  # noqa: BLE001
            log.debug("auth_prefill_skipped", reason=str(e))

    async def _dismiss_overlays(self, page) -> None:
        """Ferme les modales courantes Fansly si visibles.

        Ordre important : on traite la modale Push Notifications en premier
        (elle bloque tout le reste), puis le banner cookies, puis les variantes.
        """
        attempts = (
            ("push_modal", Sel.push_notifications_maybe_later),
            ("cookies", Sel.cookie_accept),
            ("generic_maybe_later", Sel.generic_maybe_later),
        )
        for label, getter in attempts:
            try:
                loc = getter(page)
                if await loc.is_visible(timeout=1500):
                    await self._humanizer.hover_then_click(loc)
                    log.info("overlay_dismissed", which=label)
                    await asyncio.sleep(0.6)
            except Exception as e:  # noqa: BLE001
                log.debug("overlay_dismiss_skipped", which=label, error=str(e))
