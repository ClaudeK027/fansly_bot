"""Test direct de publication avec preview attachee.

Lance une publication complete (sans queue, sans worker) pour valider
rapidement le nouveau flow d'upload qui inclut la preview.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import structlog

from fansly_bot.browser.humanizer import Humanizer
from fansly_bot.browser.session import BrowserSession
from fansly_bot.config import load_settings
from fansly_bot.infra.retry import RetryPolicies
from fansly_bot.infra.state import StateStore
from fansly_bot.logging_setup import setup_logging
from fansly_bot.services.auth import AuthService
from fansly_bot.services.caption_picker import CaptionPicker
from fansly_bot.services.uploader import UploaderService

log = structlog.get_logger("test_publish")


async def main() -> int:
    settings = load_settings()
    settings.ensure_runtime_dirs()
    setup_logging(settings)

    media = ROOT / "data" / "Medias" / "Test2" / "8ac7cc0022130308048d51d21c8a54e0.jpg"
    if not media.is_file():
        log.error("test_media_not_found", path=str(media))
        return 1

    # Configure un lot temporaire Test2 (le lot existe deja, mais on s'assure de l'activer)
    state = StateStore(settings)
    state.start_batch("Test2", max_cycles=1)
    log.info("test_batch_activated", batch="Test2")

    humanizer = Humanizer(settings)
    humanizer.bind_publishing(settings.publishing)
    session = BrowserSession(settings)
    captions = CaptionPicker(settings)
    retries = RetryPolicies(settings)
    auth = AuthService(settings, session, humanizer)
    uploader = UploaderService(settings, session, humanizer, auth, captions, state, retries)

    try:
        await session.start()
        try:
            await auth.ensure_logged_in()
            log.info("auth_ok")
        except Exception as e:  # noqa: BLE001
            log.error("auth_failed", error=str(e))
            return 2

        # Publication directe (gere lot Test2 + cycle + preview)
        log.info("TEST_PUBLISH_STARTING")
        result = await uploader.publish_next()
        log.info("TEST_PUBLISH_DONE", result=result)

        # Verifier l'etat post-publication
        batch = state.get_active_batch()
        if batch is None:
            log.info("batch_stopped_correctly_after_cycle_1")
        else:
            log.info("batch_state_after",
                     name=batch.name, cycle=batch.current_cycle,
                     published=batch.published_in_cycle,
                     total=batch.total_published)
        return 0 if result else 3
    except Exception as e:  # noqa: BLE001
        log.error("test_fatal", error=str(e), exc_info=True)
        return 1
    finally:
        await session.stop()
        state.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
