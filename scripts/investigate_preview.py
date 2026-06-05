"""Script d'investigation autonome — DOM Fansly composer + Preview + Promote Post.

Methode : on parcourt le composer Fansly etape par etape, on dump HTML +
screenshot a chaque etape cle, et on extrait les indices techniques
(nombre d'input[type=file], boutons disponibles, toggles, etc.).

Pre-requis : worker arrete (sinon conflit sur browser_profile).

Lancement :
    cd /Users/claudemenye/Documents/Project./fansly
    source .venv/bin/activate
    python scripts/investigate_preview.py
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Permet d'importer les modules du projet
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import structlog
from playwright.async_api import Locator, Page

from fansly_bot.browser.session import BrowserSession
from fansly_bot.config import load_settings
from fansly_bot.logging_setup import setup_logging
from fansly_bot.selectors import Sel
from fansly_bot.services.auth import AuthService
from fansly_bot.browser.humanizer import Humanizer

log = structlog.get_logger("investigate")

ARTIFACTS_DIR = ROOT / "data" / "artifacts" / "investigation"


# ---------- helpers ----------

def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


async def dump(page: Page, label: str, note: str = "") -> Path:
    """Dump HTML + screenshot + meta du DOM courant."""
    folder = ARTIFACTS_DIR / f"{stamp()}_{label}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "page.html").write_text(await page.content(), encoding="utf-8")
    try:
        await page.screenshot(path=str(folder / "screen.png"), full_page=True)
    except Exception:  # noqa: BLE001
        pass
    if note:
        (folder / "note.txt").write_text(note, encoding="utf-8")
    log.info("dump_saved", label=label, folder=str(folder))
    return folder


async def count_inputs(page: Page) -> tuple[int, list[dict]]:
    """Compte les input[type=file] et renvoie aussi leurs attributs."""
    inputs = page.locator("input[type='file']")
    n = await inputs.count()
    details: list[dict] = []
    for i in range(n):
        try:
            details.append({
                "index": i,
                "accept": await inputs.nth(i).get_attribute("accept"),
                "multiple": await inputs.nth(i).get_attribute("multiple") is not None,
                "name": await inputs.nth(i).get_attribute("name"),
                "id": await inputs.nth(i).get_attribute("id"),
                "is_visible": await inputs.nth(i).is_visible(timeout=500),
            })
        except Exception as e:  # noqa: BLE001
            details.append({"index": i, "error": str(e)})
    return n, details


async def search_dom_keywords(page: Page, keywords: list[str]) -> dict[str, int]:
    """Cherche des mots-cles dans le DOM (case-insensitive) et compte les occurrences."""
    html = await page.content()
    html_lower = html.lower()
    out = {}
    for k in keywords:
        out[k] = html_lower.count(k.lower())
    return out


# ---------- etapes d'investigation ----------

async def step_1_open_composer(page: Page) -> bool:
    """Ouvre le composer principal Fansly et dump."""
    log.info("STEP 1 — Ouverture composer principal")
    # Navigation
    await page.goto("https://fansly.com/home", wait_until="domcontentloaded")
    await asyncio.sleep(3)

    # Tente de fermer overlays courants (Maybe Later, cookies, push)
    for selector in [
        ".modal-wrapper >> text=Maybe Later",
        "div.btn:has-text('Maybe Later')",
        "div.btn:has-text('Accept')",
        "app-web-push-enable-modal div.btn:has-text('Maybe Later')",
    ]:
        try:
            loc = page.locator(selector).first
            if await loc.is_visible(timeout=1500):
                await loc.click()
                await asyncio.sleep(0.5)
                log.info("overlay_dismissed", selector=selector)
        except Exception:  # noqa: BLE001
            pass

    await dump(page, "1_home_initial", "Home page initial, avant interaction")

    # Cherche un bouton "new post" / icone +
    candidates = [
        ".mobile-new-post-button",
        "div.new-post-btn",
        "div.btn:has-text('New Post')",
        "div.btn:has-text('Create Post')",
        "[aria-label*='new post' i]",
        "[aria-label*='create post' i]",
        "i.fa-plus",
        ".fa-circle-plus",
    ]
    found = None
    for sel in candidates:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible(timeout=1500):
                found = (sel, loc)
                log.info("new_post_button_found", selector=sel)
                break
        except Exception:  # noqa: BLE001
            continue

    if not found:
        log.error("no_new_post_button_found")
        await dump(page, "1_no_button", "Aucun bouton new-post trouve")
        return False

    sel, loc = found
    await loc.click()
    await asyncio.sleep(2.5)
    await dump(page, "2_composer_open", f"Composer ouvert via {sel}")
    return True


async def step_2_inventory_composer(page: Page) -> dict:
    """Inventaire complet du composer ouvert."""
    log.info("STEP 2 — Inventaire composer")

    n_inputs, inputs_detail = await count_inputs(page)
    keywords = [
        "promote post",
        "promote_post",
        "promote-post",
        "free preview",
        "add free preview",
        "addfreepreview",
        "FYP",
        "for you page",
        "app-create-post",
        "app-account-media-upload",
        "media-permissions",
        "advanced permissions",
        "require subscription",
        "subscriber",
    ]
    kw_counts = await search_dom_keywords(page, keywords)

    # Cherche les textareas / inputs visibles
    textareas = await page.locator("textarea").count()
    buttons = await page.locator("div.btn, button.btn").count()

    inventory = {
        "n_input_file": n_inputs,
        "input_file_details": inputs_detail,
        "n_textarea": textareas,
        "n_buttons": buttons,
        "keyword_counts": kw_counts,
    }
    folder = await dump(page, "3_inventory", json.dumps(inventory, indent=2, default=str))
    (folder / "inventory.json").write_text(json.dumps(inventory, indent=2, default=str), encoding="utf-8")
    log.info("inventory_done", **{k: v for k, v in inventory.items() if k != "input_file_details"})
    return inventory


async def step_3_upload_main_file(page: Page, media_path: Path) -> dict:
    """Tente d'uploader le fichier principal et inventaire avant/après."""
    log.info("STEP 3 — Upload principal", media=media_path.name)

    # Etat avant
    n_before, _ = await count_inputs(page)
    log.info("inputs_before_upload", count=n_before)

    # Envoie le fichier au PREMIER input file
    inputs = page.locator("input[type='file']")
    if n_before == 0:
        log.error("no_input_file_in_composer")
        return {"error": "no_input"}
    try:
        await inputs.nth(0).set_input_files(str(media_path))
        log.info("set_input_files_done", target_index=0)
    except Exception as e:  # noqa: BLE001
        log.error("set_input_files_failed", error=str(e))
        return {"error": str(e)}

    # Attente que le DOM se mette a jour (modale Upload media doit s'afficher)
    await asyncio.sleep(4)
    await dump(page, "4_after_upload_main", "Apres set_input_files sur input[0]")

    # Etat apres
    n_after, details_after = await count_inputs(page)
    log.info("inputs_after_upload", count=n_after)

    # Cherche specifiquement les elements lies a la preview / promote
    kw_after = await search_dom_keywords(page, [
        "preview",
        "add free preview",
        "promote post",
        "promote",
        "advanced permissions",
        "require subscription",
        "app-account-media-upload",
        "clone",
        "free preview",
    ])

    result = {
        "inputs_before": n_before,
        "inputs_after": n_after,
        "inputs_after_details": details_after,
        "keywords_after": kw_after,
    }
    log.info("step_3_result", **{k: v for k, v in result.items() if k != "inputs_after_details"})
    return result


async def step_4_explore_preview_button(page: Page) -> dict:
    """Cherche et clique 'Add Free Preview' s'il existe."""
    log.info("STEP 4 — Exploration bouton Add Free Preview")

    # Plusieurs selecteurs candidats
    candidates = [
        "div.btn:has-text('Add Free Preview')",
        "div:has-text('Add Free Preview')",
        "[aria-label*='preview' i]",
        "button:has-text('Add Free Preview')",
        ".add-free-preview",
        ".free-preview",
    ]

    found_info = []
    for sel in candidates:
        try:
            loc = page.locator(sel)
            count = await loc.count()
            is_vis = await loc.first.is_visible(timeout=300) if count > 0 else False
            found_info.append({"selector": sel, "count": count, "first_visible": is_vis})
        except Exception as e:  # noqa: BLE001
            found_info.append({"selector": sel, "error": str(e)})

    log.info("preview_button_candidates", info=found_info)

    # Tente le clic sur le premier candidat trouve visible
    clicked_selector = None
    for info in found_info:
        if info.get("first_visible") and info.get("count", 0) > 0:
            try:
                await page.locator(info["selector"]).first.click()
                clicked_selector = info["selector"]
                await asyncio.sleep(2)
                log.info("preview_button_clicked", selector=clicked_selector)
                break
            except Exception as e:  # noqa: BLE001
                log.warning("preview_button_click_failed", selector=info["selector"], error=str(e))

    await dump(page, "5_preview_button_explored",
               f"Apres clic Add Free Preview\nselector={clicked_selector}\n"
               f"candidates={json.dumps(found_info, indent=2)}")

    n_after, details = await count_inputs(page)
    return {
        "candidates": found_info,
        "clicked": clicked_selector,
        "inputs_after_click": n_after,
        "inputs_details": details,
    }


async def step_4b_upload_preview_direct(page: Page, media_path: Path) -> dict:
    """Teste si on peut uploader la preview directement via input[1].
    Hypothese : Fansly accepte set_input_files sur input[1] comme equivalent 'Clone'."""
    log.info("STEP 4b — Test upload direct preview via input[1]")
    inputs = page.locator("input[type='file']")
    n = await inputs.count()
    if n < 2:
        log.warning("step_4b_skipped_no_input_1", n=n)
        return {"skipped": True, "n": n}

    try:
        await inputs.nth(1).set_input_files(str(media_path))
        log.info("preview_set_input_files_done")
    except Exception as e:  # noqa: BLE001
        log.error("preview_upload_failed", error=str(e))
        return {"error": str(e)}

    await asyncio.sleep(3)
    await dump(page, "5b_after_preview_upload", "Apres set_input_files sur input[1] (preview)")

    # Verifie que la preview est attachee : presence de 'no-preview' ou structure indiquant preview present
    html = await page.content()
    has_no_preview = 'class="no-preview"' in html
    has_preview_thumb = bool(re.search(r'class="[^"]*preview-thumb[^"]*"', html))
    has_preview_overlay = 'Preview' in html and '<div' in html
    n_inputs_after, _ = await count_inputs(page)
    return {
        "input_1_uploaded": True,
        "has_no_preview_marker": has_no_preview,
        "has_preview_thumb": has_preview_thumb,
        "n_inputs_after": n_inputs_after,
    }


async def step_4c_click_upload_in_modal(page: Page) -> dict:
    """Ferme d'abord l'editeur de preview (s'il est ouvert), puis valide la modale Upload Media."""
    log.info("STEP 4c — Validation modale (ferme editeur preview + Upload Media)")
    result = {"editor_closed": False, "upload_modal_closed": False}

    # 1) Si app-media-editor est ouvert (active-modal), trouver son bouton de sauvegarde
    editor = page.locator("app-media-editor.active-modal").first
    if await editor.count() > 0:
        log.info("media_editor_detected")
        await dump(page, "5c_media_editor_open", "Editeur de preview ouvert")

        # Cherche les boutons du footer de l'editeur
        editor_buttons = editor.locator("div.btn, button.btn")
        n_btns = await editor_buttons.count()
        btn_info = []
        for i in range(n_btns):
            try:
                txt = (await editor_buttons.nth(i).inner_text(timeout=500)).strip()
                visible = await editor_buttons.nth(i).is_visible(timeout=300)
                btn_info.append({"index": i, "text": txt, "visible": visible})
            except Exception:  # noqa: BLE001
                pass
        log.info("editor_buttons_inventory", buttons=btn_info)

        # Cherche le bouton de validation : Save Changes / Save / Confirm / Done / OK / Apply
        save_btn = editor.locator("div.btn.solid-blue, button.btn.solid-blue").filter(
            has_text=re.compile(r"(Save Changes|Save|Confirm|Done|Apply|OK)", re.I)
        ).first
        try:
            await save_btn.wait_for(state="visible", timeout=3000)
            await save_btn.click()
            log.info("media_editor_saved")
            await asyncio.sleep(2)
            result["editor_closed"] = True
        except Exception as e:  # noqa: BLE001
            log.error("media_editor_save_failed", error=str(e))
            return {**result, "error_editor": str(e), "editor_buttons": btn_info}

    # 2) Maintenant valider la modale Upload Media
    btn = page.locator(
        "app-account-media-upload .modal-footer div.btn.solid-blue, "
        "app-account-media-upload .modal-footer button.btn.solid-blue"
    ).filter(has_text=re.compile(r"^\s*Upload\s*$", re.I)).first
    try:
        await btn.wait_for(state="visible", timeout=5000)
        await btn.click()
        log.info("upload_modal_validated")
        result["upload_modal_closed"] = True
    except Exception as e:  # noqa: BLE001
        log.error("upload_modal_validate_failed", error=str(e))
        return {**result, "error_modal": str(e)}

    try:
        await page.locator("app-account-media-upload").first.wait_for(state="hidden", timeout=10_000)
    except Exception:  # noqa: BLE001
        pass
    await asyncio.sleep(2)
    await dump(page, "6_back_in_composer", "Composer principal final (apres fermeture modale)")
    return result


async def step_5_find_promote_toggle(page: Page) -> dict:
    """Cherche le toggle Promote Post dans le composer principal apres fermeture
    de la modale Upload Media."""
    log.info("STEP 5 — Recherche toggle Promote Post (composer final)")

    # Cherche large : tout mot lie a la visibilite / FYP / promote / discover
    keywords_broad = [
        "promote", "promot", "fyp", "for you", "for-you",
        "discover", "discoverable", "explore", "boost", "showcase",
        "public", "visible", "share", "make public",
        "homepage", "feed", "trending",
    ]
    html = await page.content()
    html_lower = html.lower()
    counts = {k: html_lower.count(k.lower()) for k in keywords_broad}
    log.info("broad_keyword_counts", counts=counts)

    # Pour chaque mot trouve, extrait quelques contextes
    contexts = {}
    for kw, n in counts.items():
        if n == 0:
            continue
        snippets = []
        for m in re.finditer(re.escape(kw), html, re.IGNORECASE):
            start = max(0, m.start() - 80)
            end = min(len(html), m.end() + 200)
            chunk = html[start:end]
            chunk = re.sub(r'_ngcontent-ng-c\d+="?"?', '', chunk)
            chunk = re.sub(r'\s+', ' ', chunk)
            snippets.append(chunk[:280])
            if len(snippets) >= 3:
                break
        contexts[kw] = snippets

    # Inspecte tous les checkboxes (app-xd-checkbox) visibles
    checkboxes_info = []
    cb_locator = page.locator("app-xd-checkbox")
    n_cb = await cb_locator.count()
    for i in range(min(n_cb, 20)):
        try:
            cb = cb_locator.nth(i)
            outer = await cb.evaluate("el => el.outerHTML")
            # Texte du parent
            parent_text = await cb.evaluate(
                "el => (el.parentElement ? el.parentElement.innerText : '').slice(0, 200)"
            )
            outer_clean = re.sub(r'_ngcontent-ng-c\d+="?"?', '', outer)
            outer_clean = re.sub(r'\s+', ' ', outer_clean)[:300]
            checkboxes_info.append({
                "index": i,
                "outer_html": outer_clean,
                "parent_text": parent_text.strip(),
            })
        except Exception as e:  # noqa: BLE001
            checkboxes_info.append({"index": i, "error": str(e)})

    # Inspecte aussi les toggles (app-xd-toggle ou input checkbox)
    toggles_info = []
    for sel in ["app-xd-toggle", "input[type='checkbox']", "[role='switch']"]:
        loc = page.locator(sel)
        n = await loc.count()
        toggles_info.append({"selector": sel, "count": n})

    folder = await dump(page, "7_promote_search_final",
                        f"counts={json.dumps(counts, indent=2)}\n\n"
                        f"checkboxes={json.dumps(checkboxes_info, indent=2)}\n\n"
                        f"toggles={json.dumps(toggles_info, indent=2)}\n\n"
                        f"contexts={json.dumps(contexts, indent=2)}")

    return {
        "broad_counts": counts,
        "checkboxes": checkboxes_info,
        "toggles": toggles_info,
        "contexts": {k: v[:2] for k, v in contexts.items()},  # 2 snippets max chacun
    }


# ---------- main ----------

async def main() -> int:
    settings = load_settings()
    settings.ensure_runtime_dirs()
    setup_logging(settings)
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

    # Choisi un media test
    media_candidates = [
        ROOT / "data" / "Medias" / "Test2" / "8ac7cc0022130308048d51d21c8a54e0.jpg",
        ROOT / "data" / "Medias" / "lot_test" / "photo_5807788497622994770_y.jpg",
    ]
    media = next((p for p in media_candidates if p.is_file()), None)
    if media is None:
        log.error("no_test_media_found")
        return 1
    log.info("test_media_selected", path=str(media))

    humanizer = Humanizer(settings)
    session = BrowserSession(settings)
    auth = AuthService(settings, session, humanizer)

    try:
        await session.start()
        page = await session.page()

        # Auth check
        try:
            await auth.ensure_logged_in()
            log.info("auth_ok")
        except Exception as e:  # noqa: BLE001
            log.error("auth_failed", error=str(e))
            await dump(page, "0_auth_fail", str(e))
            return 2

        # Etape 1
        ok = await step_1_open_composer(page)
        if not ok:
            log.error("step_1_failed_no_composer_open")
            return 3

        # Etape 2 — inventaire avant upload
        inventory_before = await step_2_inventory_composer(page)

        # Etape 3 — upload principal et observation
        upload_result = await step_3_upload_main_file(page, media)

        # Etape 4 — exploration bouton "Add Free Preview" si visible (juste pour confirmer)
        preview_exploration = await step_4_explore_preview_button(page)

        # Etape 4b — test upload direct sur input[1] (la vraie experimentation)
        preview_direct = await step_4b_upload_preview_direct(page, media)

        # Etape 4c — valide la modale Upload Media (clic 'Upload')
        modal_close = await step_4c_click_upload_in_modal(page)

        # Etape 5 — recherche du Promote Post dans le composer principal final
        promote_search = await step_5_find_promote_toggle(page)

        # Synthese
        summary = {
            "media_used": str(media),
            "inventory_before_upload": inventory_before,
            "upload_main_result": upload_result,
            "preview_button_exploration": preview_exploration,
            "preview_direct_upload": preview_direct,
            "modal_close": modal_close,
            "promote_post_search": promote_search,
        }
        summary_path = ARTIFACTS_DIR / f"{stamp()}_SUMMARY.json"
        summary_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
        log.info("INVESTIGATION_DONE", summary_path=str(summary_path))
        print(f"\n========== SUMMARY WRITTEN TO: {summary_path}\n")

        return 0

    except Exception as e:  # noqa: BLE001
        log.error("investigation_fatal", error=str(e), exc_info=True)
        return 1
    finally:
        await session.stop()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
