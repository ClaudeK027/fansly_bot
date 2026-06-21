#!/usr/bin/env python3
"""Script de login Fansly visible via noVNC.

Tourne DANS le container fansly-novnc, lance Chromium en mode visible
(display :99 piloté par Xvfb + x11vnc + websockify + noVNC), pré-remplit
les identifiants si fournis, attend que l'utilisateur termine le login
manuellement dans la fenêtre noVNC du browser, puis :

  1. Détecte que la page Fansly a atteint /home (login OK)
  2. Sauvegarde le browser_profile/ dans /output/browser_profile/
  3. Sort en exit 0

En cas de timeout (login non terminé après TIMEOUT_S), sort en exit 1.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

import structlog
from playwright.async_api import async_playwright, TimeoutError as PWTimeout


log = structlog.get_logger("novnc.setup_auth_visual")

# Configuration depuis env vars (passees par le manager via docker run -e)
FANSLY_USERNAME = os.environ.get("FANSLY_USERNAME", "")
FANSLY_PASSWORD = os.environ.get("FANSLY_PASSWORD", "")
INSTANCE_NAME = os.environ.get("INSTANCE_NAME", "unknown")
TIMEOUT_S = int(os.environ.get("LOGIN_TIMEOUT_S", "600"))  # 10 min par defaut

# Paths internes au container
PROFILE_DIR = Path("/tmp/browser_profile")  # ecriture temporaire
OUTPUT_DIR = Path("/output/browser_profile")  # destination finale (volume host)
BASE_URL = "https://fansly.com"
HOME_PATH = "/home"


def _is_logged_in(url: str) -> bool:
    """Detecte si l'URL Fansly indique un login reussi.

    Logique : si on est sur /home (ou un sous-chemin du home) sans /login
    dans l'URL, on est connecte.
    """
    return HOME_PATH in url and "/login" not in url


async def _try_open_login_form(page) -> None:
    """Tente d'ouvrir le formulaire de login (age gate + bouton header).

    Best-effort : si les selectors ne matchent pas, on continue —
    l'utilisateur naviguera lui-meme dans la fenetre noVNC.
    """
    try:
        # Age gate eventuel
        age_btn = page.locator("button:has-text('Enter')").first
        if await age_btn.is_visible(timeout=2000):
            await age_btn.click()
            await page.wait_for_timeout(500)
    except Exception:  # noqa: BLE001
        pass

    try:
        # Bouton "Sign in" du header
        signin_btn = page.locator("a[href*='/login'], button:has-text('Sign in')").first
        if await signin_btn.is_visible(timeout=2000):
            await signin_btn.click()
            await page.wait_for_timeout(500)
    except Exception:  # noqa: BLE001
        pass


async def _prefill_credentials(page) -> None:
    """Pre-remplit email + password si visibles. Best-effort."""
    if not FANSLY_USERNAME or not FANSLY_PASSWORD:
        return
    try:
        email_input = page.locator(
            "input[type='email'], input[name='username']"
        ).first
        if await email_input.is_visible(timeout=3000):
            await email_input.fill(FANSLY_USERNAME)
            await page.wait_for_timeout(300)
        pwd_input = page.locator("input[type='password']").first
        if await pwd_input.is_visible(timeout=2000):
            await pwd_input.fill(FANSLY_PASSWORD)
            await page.wait_for_timeout(300)
        # On ne CLIQUE PAS Sign in ici — l'utilisateur valide manuellement
        # apres avoir verifie / resolu Cloudflare / 2FA.
    except Exception as e:  # noqa: BLE001
        log.debug("prefill_skipped", error=str(e))


async def main() -> int:
    log.info(
        "novnc_setup_auth_starting",
        instance=INSTANCE_NAME,
        has_credentials=bool(FANSLY_USERNAME and FANSLY_PASSWORD),
        timeout_s=TIMEOUT_S,
    )

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,  # CRITIQUE : visible via Xvfb/noVNC
            viewport={"width": 1280, "height": 800},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
            ignore_default_args=["--enable-automation"],
        )

        # Stealth best-effort
        try:
            from playwright_stealth import Stealth  # type: ignore[import-untyped]
            await Stealth().apply_stealth_async(context)
        except Exception as e:  # noqa: BLE001
            log.debug("stealth_unavailable", error=str(e))

        page = context.pages[0] if context.pages else await context.new_page()
        log.info("novnc_navigating", url=BASE_URL)
        await page.goto(BASE_URL, wait_until="domcontentloaded")

        # Si deja sur /home (cookies de session, peu probable au 1er run)
        if _is_logged_in(page.url):
            log.info("novnc_already_logged_in", url=page.url)
        else:
            await _try_open_login_form(page)
            await _prefill_credentials(page)

        log.info(
            "novnc_waiting_for_login",
            timeout_s=TIMEOUT_S,
            hint=(
                "L'utilisateur termine le login dans la fenetre noVNC. "
                "Le script detecte automatiquement quand la page atterrit "
                f"sur {HOME_PATH}."
            ),
        )

        try:
            await page.wait_for_url(
                lambda url: _is_logged_in(url),
                timeout=TIMEOUT_S * 1000,
            )
        except PWTimeout:
            log.error("novnc_login_timeout", timeout_s=TIMEOUT_S)
            await context.close()
            return 1

        # Login confirme — sauvegarder le profile
        final_url = page.url
        log.info("novnc_login_success", final_url=final_url)
        await context.close()

    # Copie le browser_profile dans /output (volume bind-monte sur l'host)
    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
    shutil.copytree(PROFILE_DIR, OUTPUT_DIR)
    log.info(
        "novnc_profile_saved",
        source=str(PROFILE_DIR),
        destination=str(OUTPUT_DIR),
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
