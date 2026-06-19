# src/fansly_bot/browser/session.py
"""Gestionnaire de session Playwright : contexte persistant + furtivite + verrou."""

from __future__ import annotations

import asyncio
from typing import Optional

import structlog
from playwright.async_api import (
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from ..config import Settings

log = structlog.get_logger("browser.session")


class BrowserSession:
    """Cycle de vie d'un BrowserContext persistant, stealth-patched.

    Le verrou `use()` doit etre acquis par tout service qui pilote le navigateur,
    pour eviter qu'uploader et purger s'entrechoquent.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._playwright: Optional[Playwright] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._start_lock = asyncio.Lock()
        self._use_lock = asyncio.Lock()  # serialise les acces metier au navigateur

    # ---------- cycle de vie ----------

    async def start(self, headless_override: Optional[bool] = None) -> None:
        async with self._start_lock:
            if self._context is not None:
                log.debug("session_already_started")
                return

            cfg = self._settings.browser
            cfg.user_data_dir.mkdir(parents=True, exist_ok=True)
            headless = cfg.headless if headless_override is None else headless_override

            log.info("session_starting", headless=headless, user_data_dir=str(cfg.user_data_dir))

            self._playwright = await async_playwright().start()
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ]

            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(cfg.user_data_dir),
                headless=headless,
                viewport={"width": cfg.viewport_width, "height": cfg.viewport_height},
                locale=cfg.locale,
                timezone_id=cfg.timezone,
                user_agent=cfg.user_agent,
                args=launch_args,
                ignore_default_args=["--enable-automation"],
            )
            self._context.set_default_navigation_timeout(cfg.navigation_timeout_ms)
            self._context.set_default_timeout(cfg.default_action_timeout_ms)

            await self._apply_stealth(self._context)

            pages = self._context.pages
            self._page = pages[0] if pages else await self._context.new_page()

            log.info("session_started", headless=headless)

    async def stop(self) -> None:
        async with self._start_lock:
            if self._context is not None:
                try:
                    await self._context.close()
                    log.info("session_context_closed")
                except Exception as e:  # noqa: BLE001
                    log.warning("session_context_close_error", error=str(e))
                finally:
                    self._context = None
                    self._page = None
            if self._playwright is not None:
                try:
                    await self._playwright.stop()
                    log.info("playwright_stopped")
                except Exception as e:  # noqa: BLE001
                    log.warning("playwright_stop_error", error=str(e))
                finally:
                    self._playwright = None

    # ---------- accesseurs ----------

    async def page(self) -> Page:
        if self._context is None:
            raise RuntimeError("BrowserSession non demarree.")
        if self._page is None or self._page.is_closed():
            # IMPORTANT : context.new_page() N'HERITE PAS de
            # set_default_navigation_timeout / set_default_timeout — ces
            # defaults ne couvrent que page.goto et locator.*. Sans wrapper
            # asyncio.wait_for, new_page() peut hang indefiniment si le
            # transport CDP est gele (renderer Chromium zombie).
            try:
                self._page = await asyncio.wait_for(
                    self._context.new_page(), timeout=10.0,
                )
            except asyncio.TimeoutError:
                log.error("session_new_page_timeout", hint="Chromium renderer may be zombie")
                raise RuntimeError(
                    "context.new_page() a timeout (10s) — session probablement corrompue"
                )
        return self._page

    @property
    def context(self) -> BrowserContext:
        if self._context is None:
            raise RuntimeError("BrowserSession non demarree.")
        return self._context

    def use(self) -> asyncio.Lock:
        """Verrou exclusif d'utilisation du navigateur.

        Usage :
            async with session.use():
                page = await session.page()
                ...
        """
        return self._use_lock

    async def is_alive(self) -> bool:
        """Healthcheck rapide : verifie que le renderer Chromium repond.

        Wrappe page.evaluate dans asyncio.wait_for car evaluate() N'HERITE PAS
        des defaults Playwright (set_default_timeout ne couvre que les
        locator.*/wait_for). Sans ce timeout, is_alive() peut elle-meme hang
        indefiniment sur un renderer zombie."""
        if self._context is None:
            return False
        try:
            page = await self.page()
            await asyncio.wait_for(page.evaluate("1+1"), timeout=5.0)
            return True
        except asyncio.TimeoutError:
            log.warning("session_healthcheck_timeout", hint="renderer Chromium unresponsive")
            return False
        except Exception as e:  # noqa: BLE001
            log.warning("session_healthcheck_failed", error=str(e))
            return False

    # ---------- internes ----------

    @staticmethod
    async def _apply_stealth(context: BrowserContext) -> None:
        try:
            from playwright_stealth import Stealth  # type: ignore

            await Stealth().apply_stealth_async(context)
            log.info("stealth_applied", api="Stealth.apply_stealth_async")
            return
        except (ImportError, AttributeError):
            pass

        try:
            from playwright_stealth import stealth_async  # type: ignore

            async def _apply_to_page(page: Page) -> None:
                try:
                    await stealth_async(page)
                except Exception as e:  # noqa: BLE001
                    log.warning("stealth_page_apply_error", error=str(e))

            for page in context.pages:
                await _apply_to_page(page)
            context.on("page", lambda p: asyncio.create_task(_apply_to_page(p)))
            log.info("stealth_applied", api="stealth_async (per-page)")
            return
        except ImportError:
            pass

        log.warning(
            "stealth_not_applied",
            hint="playwright-stealth introuvable — installe-le ou ajuste session.py",
        )
