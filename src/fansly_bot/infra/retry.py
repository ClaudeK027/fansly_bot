# src/fansly_bot/infra/retry.py
"""Politiques de retry centralisees (tenacity).

Deux profils :
  - network_retry : pour les actions exposees aux instabilites reseau
                    (navigation, clic, evaluate)
  - quick_retry   : pour les actions a faible cout, peu critiques
"""

from __future__ import annotations

from typing import Callable

import structlog
from playwright.async_api import Error as PlaywrightError, TimeoutError as PWTimeout
from tenacity import (
    AsyncRetrying,
    RetryError,
    before_sleep_log,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from ..config import Settings

log = structlog.get_logger("infra.retry")


# Exceptions reseau / DOM qu'on considere comme transitoires
RETRYABLE_PLAYWRIGHT = (PWTimeout, PlaywrightError, ConnectionError, TimeoutError)


def _build(attempts: int, initial: float, maximum: float) -> Callable[[], AsyncRetrying]:
    """Fabrique un AsyncRetrying configurable. tenacity expose un AsyncRetrying
    qu'on utilise via `async for attempt in retrier():` pour rester async-friendly.
    """

    def make() -> AsyncRetrying:
        return AsyncRetrying(
            stop=stop_after_attempt(attempts),
            wait=wait_exponential_jitter(initial=initial, max=maximum),
            retry=retry_if_exception_type(RETRYABLE_PLAYWRIGHT),
            before_sleep=before_sleep_log(  # type: ignore[arg-type]
                logger=__build_stdlib_logger(),
                log_level=30,  # WARNING
            ),
            reraise=True,
        )

    return make


def __build_stdlib_logger():
    """tenacity attend un logger stdlib pour before_sleep_log."""
    import logging
    return logging.getLogger("infra.retry")


class RetryPolicies:
    """Collection de politiques pretes a l'emploi, parametrees via Settings."""

    def __init__(self, settings: Settings) -> None:
        self._network = _build(
            attempts=settings.retry.attempts,
            initial=settings.retry.initial_wait_s,
            maximum=settings.retry.max_wait_s,
        )
        self._quick = _build(
            attempts=max(2, settings.retry.attempts - 1),
            initial=0.5,
            maximum=5.0,
        )

    def network(self) -> AsyncRetrying:
        return self._network()

    def quick(self) -> AsyncRetrying:
        return self._quick()


# Re-exports utiles pour les services
__all__ = ["RetryPolicies", "RetryError", "RETRYABLE_PLAYWRIGHT"]
