# src/fansly_bot/browser/humanizer.py
"""Simulation comportementale humaine.

Distribution log-normale pour les delais : un humain a une mediane courte
mais une longue traine de pauses (lecture, distraction). random.uniform serait
trop plat et detectable.
"""

from __future__ import annotations

import asyncio
import math
import random
from typing import Optional

import structlog
from playwright.async_api import Locator, Page

from ..config import PauseProfile, Settings, TypingProfile

log = structlog.get_logger("browser.humanizer")


def _lognormal_clamped(median: float, sigma: float, lo: float, hi: float) -> float:
    """Tirage log-normal centre sur `median` (en log-space), clampe sur [lo, hi]."""
    mu = math.log(max(median, 1e-9))
    value = random.lognormvariate(mu, sigma)
    return max(lo, min(hi, value))


class Humanizer:
    def __init__(self, settings: Settings) -> None:
        self._cfg = settings.humanizer

    # ---------- Pauses ----------

    async def short_pause(self) -> None:
        await asyncio.sleep(self._draw(self._cfg.short_pause))

    async def long_pause(self) -> None:
        await asyncio.sleep(self._draw(self._cfg.long_pause))

    async def between_destructive_actions(self) -> None:
        """Pause specifique entre deux suppressions : un peu plus longue."""
        delay = self._draw_seconds(self._cfg.long_pause.median_s * 2.5, 0.5, 8.0, 180.0)
        await asyncio.sleep(delay)

    @staticmethod
    def _draw(p: PauseProfile) -> float:
        return _lognormal_clamped(p.median_s, p.sigma, p.min_s, p.max_s)

    @staticmethod
    def _draw_seconds(median: float, sigma: float, lo: float, hi: float) -> float:
        return _lognormal_clamped(median, sigma, lo, hi)

    # ---------- Frappe ----------

    async def type_humanly(self, locator: Locator, text: str) -> None:
        """Frappe caractere par caractere avec delai log-normal + micro-pauses."""
        t: TypingProfile = self._cfg.typing
        await locator.click()
        await self.short_pause()
        for char in text:
            delay_ms = _lognormal_clamped(
                t.per_char_ms_median, t.per_char_ms_sigma, t.per_char_ms_min, t.per_char_ms_max
            )
            await locator.press_sequentially(char, delay=delay_ms)
            if random.random() < t.micro_pause_probability:
                pause_ms = random.uniform(t.micro_pause_ms_min, t.micro_pause_ms_max)
                await asyncio.sleep(pause_ms / 1000.0)

    # ---------- Navigation ----------

    async def hover_then_click(self, locator: Locator) -> None:
        """Survol + petite pause + clic — masque les sequences mecaniques."""
        try:
            await locator.hover()
        except Exception:  # noqa: BLE001 — hover est best-effort
            pass
        await asyncio.sleep(random.uniform(0.15, 0.45))
        await locator.click()

    async def scroll_human(self, page: Page, direction: str = "down", impulses: Optional[int] = None) -> None:
        """Scroll en plusieurs impulsions de hauteur variable, avec pauses entre."""
        n = impulses if impulses is not None else random.randint(2, 5)
        sign = 1 if direction == "down" else -1
        for _ in range(n):
            delta = sign * random.randint(220, 720)
            await page.mouse.wheel(0, delta)
            await asyncio.sleep(random.uniform(0.25, 0.95))

    # ---------- Intervalles de scheduling ----------

    def next_publication_delay_seconds(self) -> float:
        """Delai (en secondes) avant la prochaine publication, distribue log-normalement."""
        from ..config import Publishing  # import paresseux pour eviter circulaires inutiles
        cfg: Publishing = self._publishing_cfg  # type: ignore[attr-defined]
        minutes = _lognormal_clamped(
            cfg.interval_minutes_median,
            cfg.interval_minutes_sigma,
            cfg.interval_minutes_min,
            cfg.interval_minutes_max,
        )
        return minutes * 60.0

    def bind_publishing(self, publishing_cfg) -> None:
        """Le Humanizer a besoin de la config de publication ; on l'injecte apres coup
        pour ne pas creer de dependance circulaire dans le modele Settings."""
        self._publishing_cfg = publishing_cfg
