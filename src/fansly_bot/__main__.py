# src/fansly_bot/__main__.py
"""Point d'entree CLI du bot.

Sous-commandes :
  python -m fansly_bot setup-auth   → premier login interactif (Chromium visible)
  python -m fansly_bot worker       → demarre le worker permanent (consomme la queue)
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from contextlib import suppress

import structlog

from .browser.humanizer import Humanizer
from .browser.session import BrowserSession
from .config import load_settings
from .infra.state import StateStore
from .logging_setup import setup_logging
from .services.auth import AuthError, AuthService
from .worker import main as worker_main


# ---------- mode setup-auth ----------

async def cmd_setup_auth() -> int:
    settings = load_settings()
    settings.ensure_runtime_dirs()
    log = setup_logging(settings)
    log.info("setup_auth_starting")

    humanizer = Humanizer(settings)
    session = BrowserSession(settings)
    state = StateStore(settings)
    auth = AuthService(settings, session, humanizer)

    try:
        # On force le navigateur visible
        await session.start(headless_override=False)
        await auth.interactive_setup()
        log.info("setup_auth_done", hint="Tu peux maintenant lancer le worker.")
        return 0
    except AuthError as e:
        log.error("setup_auth_failed", error=str(e))
        return 2
    except Exception as e:  # noqa: BLE001
        log.error("setup_auth_fatal", error=str(e), exc_info=True)
        return 1
    finally:
        await session.stop()
        state.close()


# ---------- dispatch ----------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="fansly-bot")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup-auth", help="Premier login interactif (navigateur visible)")
    sub.add_parser("worker", help="Demarre le worker permanent")
    return parser.parse_args(argv)


def main() -> None:
    args = _parse_args()
    if args.cmd == "setup-auth":
        raise SystemExit(asyncio.run(cmd_setup_auth()))
    if args.cmd == "worker":
        worker_main()
        return
    raise SystemExit(2)


if __name__ == "__main__":
    main()
