# src/fansly_bot/services/uploader.py
"""Service d'upload de medias sur Fansly."""

from __future__ import annotations

import asyncio
import base64
import mimetypes
import random
import re
import time
from pathlib import Path
from typing import Optional

import structlog
from playwright.async_api import Page, TimeoutError as PWTimeout

from ..browser.humanizer import Humanizer
from ..browser.session import BrowserSession
from ..config import Settings
from ..infra.retry import RetryPolicies
from ..infra.state import StateStore
from ..selectors import Sel
from .auth import AuthService
from .caption_picker import CaptionPicker

log = structlog.get_logger("services.uploader")


class UploaderService:
    def __init__(
        self,
        settings: Settings,
        session: BrowserSession,
        humanizer: Humanizer,
        auth: AuthService,
        captions: CaptionPicker,
        state: StateStore,
        retries: RetryPolicies,
        captions_batch_name: str | None = None,
        run_id: int | None = None,
    ) -> None:
        self._settings = settings
        self._session = session
        self._humanizer = humanizer
        self._auth = auth
        self._captions = captions
        self._state = state
        self._retries = retries
        # Nom du lot de legendes a utiliser. Si None : fallback sur les .txt
        # a la racine de data/Captions/.
        self._captions_batch_name = captions_batch_name
        # Identifiant du job publish qui a instancie cet uploader. Sert de
        # discriminant pour media_published et publish_in_flight, ce qui
        # empeche le cleaner inter-cycle de comptabiliser des publications
        # d un run anterieur sur le meme lot.
        self._run_id = run_id

    # ---------- API publique ----------

    def has_pending_work(self) -> bool:
        """Vrai si le bot a quelque chose a publier maintenant OU au cycle suivant.

        Critere :
          - aucun lot actif → False
          - lot actif et au moins 1 media non publie dans le cycle courant → True
          - cycle courant termine mais on peut avancer au cycle suivant → True
          - max_cycles atteint → True (pour permettre l'arret formel via publish_next)
        """
        batch = self._state.get_active_batch()
        if batch is None:
            return False
        # On a un lot actif. Verifier qu'au moins un fichier existe dans son dossier.
        batch_folder = self._settings.paths.media_folder / batch.name
        if not batch_folder.is_dir():
            return False
        exts = set(self._settings.publishing.media_extensions)
        any_file = any(
            p.is_file() and p.suffix.lower() in exts
            for p in batch_folder.iterdir()
        )
        return any_file

    def list_pending_media(self) -> list[Path]:
        """Liste les medias du LOT ACTIF non encore publies dans le cycle courant.

        S'il n'y a pas de lot actif, renvoie une liste vide (mode rotation
        uniquement, plus de mode one-shot a la racine).
        """
        batch = self._state.get_active_batch()
        if batch is None:
            return []
        batch_folder = self._settings.paths.media_folder / batch.name
        if not batch_folder.is_dir():
            log.warning("uploader_batch_folder_missing", batch=batch.name)
            return []
        exts = set(self._settings.publishing.media_extensions)
        all_files = sorted(
            p for p in batch_folder.iterdir()
            if p.is_file() and p.suffix.lower() in exts
        )
        published = set(batch.published_in_cycle)
        return [p for p in all_files if p.name not in published]

    def advance_cycle_if_needed(self) -> str:
        """Verifie si on doit avancer le cycle ou arreter le lot.

        Synchrone (operations BDD pures). A appeler par le worker AVANT
        publish_next() — cela permet d'intercaler un nettoyage entre la fin
        d'un cycle et le debut du suivant sans race condition.

        Renvoie :
            "continue" : aucun changement (cycle en cours)
            "advanced" : cycle avance (cycle precedent etait complet)
            "stopped"  : lot arrete (max_cycles atteint)
            "no_batch" : pas de lot actif
        """
        batch = self._state.get_active_batch()
        if batch is None:
            return "no_batch"
        pending = self.list_pending_media()
        if pending:
            return "continue"
        # cycle complet
        next_cycle = batch.current_cycle + 1
        if 0 < batch.max_cycles < next_cycle:
            log.info(
                "uploader_batch_max_cycles_reached",
                batch=batch.name,
                max_cycles=batch.max_cycles,
                total_published=batch.total_published,
            )
            self._state.stop_batch()
            return "stopped"
        log.info(
            "uploader_cycle_complete",
            batch=batch.name,
            completed_cycle=batch.current_cycle,
            next_cycle=next_cycle,
        )
        self._state.advance_batch_cycle(next_cycle)
        return "advanced"

    async def publish_next(self) -> Optional[str]:
        """Publie un media du lot actif (tirage aleatoire avec memoire).

        Ne gere PLUS l'avancement de cycle — c'est la responsabilite du worker
        via advance_cycle_if_needed() (decouplage pour eviter race condition
        avec le rolling cycle cleanup).

        Idempotence : on inscrit un marqueur 'in_flight' AVANT le clic Post
        Fansly. Si la tentative plante (composer qui ne ferme pas, etc.),
        le marqueur reste en place. Au prochain passage sur le meme media,
        ou au prochain demarrage du worker, ce marqueur indique 'la tentative
        a peut-etre abouti cote Fansly mais on n a pas pu confirmer'. On le
        traite alors comme publie pour eviter une republication.
        """
        async with self._session.use():
            batch = self._state.get_active_batch()
            if batch is None:
                log.info("uploader_no_active_batch")
                return None

            pending = self.list_pending_media()
            if not pending:
                log.info("uploader_no_pending_in_cycle", batch=batch.name)
                return None

            # Tirage aleatoire dans le cycle courant
            media = random.choice(pending)
            caption = self._captions.pick(batch_name=self._captions_batch_name)
            log.info(
                "uploader_starting",
                media=media.name,
                batch=batch.name,
                cycle=batch.current_cycle,
                caption_length=len(caption),
            )

            try:
                await self._auth.ensure_logged_in()

                # Retry tenacity sur tout le upload : robustesse face aux
                # timings cote Fansly (encodage video, latence reseau, etc.).
                # Si le 1er essai echoue, on relance toute la sequence et
                # Fansly a eu le temps de finaliser entre temps.
                async for attempt in self._retries.network():
                    with attempt:
                        await self._do_upload(media, caption)

                self._mark_published(media, caption, batch.name, batch.current_cycle)
                log.info(
                    "uploader_published",
                    media=media.name,
                    batch=batch.name,
                    cycle=batch.current_cycle,
                )
                return media.name

            except Exception as e:  # noqa: BLE001
                log.error("uploader_failed", media=media.name, error=str(e), exc_info=True)
                await self._dump_artifact("upload_failed", media.name)
                return None

    # ---------- coeur du flow Playwright ----------

    async def _do_upload(self, media: Path, caption: str) -> None:
        page = await self._session.page()

        # ─── Instrumentation HTTP : capte tout le trafic vers fansly.com
        # pendant l upload pour diagnostiquer d eventuels rejets cote serveur
        # (status 4xx/5xx, headers manquants, etc.). Filtre minimal pour
        # eviter le bruit (skip les assets statiques).
        # IMPORTANT : on attache les listeners en debut d _do_upload et on
        # les DETACHE en fin via try/finally. Sans ce detach, chaque appel
        # successif a _do_upload accumule des listeners et chaque event est
        # logge N fois (× nombre d appels precedents) — cree de fausses
        # duplications dans les logs HTTP.
        skip_resource_types = {"image", "stylesheet", "font", "media", "manifest", "other"}

        async def _log_request(request):
            try:
                if "fansly.com" not in request.url:
                    return
                if request.resource_type in skip_resource_types:
                    return
                post_data_size = len(request.post_data or b"") if request.post_data else 0
                ct = request.headers.get("content-type", "")
                log.info(
                    "http_req",
                    method=request.method,
                    url=request.url[:180],
                    rtype=request.resource_type,
                    ct=ct[:60],
                    body_bytes=post_data_size,
                )
            except Exception as e:  # noqa: BLE001
                log.debug("http_req_log_error", error=str(e))

        async def _log_response(response):
            try:
                if "fansly.com" not in response.url:
                    return
                req = response.request
                if req.resource_type in skip_resource_types:
                    return
                status = response.status
                body_preview = ""
                if status >= 400:
                    try:
                        body = await response.text()
                        body_preview = body[:500]
                    except Exception:  # noqa: BLE001
                        body_preview = "<unreadable>"
                log.info(
                    "http_resp",
                    status=status,
                    method=req.method,
                    url=response.url[:180],
                    body=body_preview if status >= 400 else None,
                )
            except Exception as e:  # noqa: BLE001
                log.debug("http_resp_log_error", error=str(e))

        # On garde une reference aux callbacks pour pouvoir les detacher.
        def _on_req(req):
            asyncio.create_task(_log_request(req))

        def _on_resp(resp):
            asyncio.create_task(_log_response(resp))

        page.on("request", _on_req)
        page.on("response", _on_resp)
        log.info("http_instrumentation_attached")

        try:
            await self._do_upload_inner(page, media, caption)
        finally:
            try:
                page.remove_listener("request", _on_req)
                page.remove_listener("response", _on_resp)
                log.info("http_instrumentation_detached")
            except Exception as e:  # noqa: BLE001
                log.warning("http_instrumentation_detach_failed", error=str(e))

    async def _do_upload_inner(self, page, media: Path, caption: str) -> None:
        """Implementation interne de _do_upload (sans gestion des listeners
        HTTP). Permet a `_do_upload` de gerer l attach/detach dans un
        try/finally proprement, sans dupliquer le code metier."""
        await page.goto(self._settings.auth.base_url + "/home", wait_until="domcontentloaded")
        await self._humanizer.long_pause()
        await self._auth._dismiss_overlays(page)  # type: ignore[attr-defined]

        # 0) Ouvrir le composer (modale ou route dediee)
        await self._open_composer(page)

        # 1) Saisir la legende — auto-ajout de #fyp si absent
        # La doc Fansly Help Center recommande explicitement ce hashtag pour
        # maximiser la visibilite sur le For You Page.
        if not re.search(r"#fyp\b", caption, re.IGNORECASE):
            caption = caption.rstrip() + " #fyp"
            log.info("uploader_fyp_hashtag_added")
        textarea = Sel.composer_textarea(page)
        await textarea.wait_for(state="visible", timeout=15_000)
        await self._humanizer.type_humanly(textarea, caption)
        await self._humanizer.short_pause()

        # 2) Attacher le fichier PRINCIPAL.
        # NOTE technique : `set_input_files` de Playwright ne declenche pas
        # toujours les events que l app Fansly (Angular) ecoute. Resultat
        # observe : le fichier est pose dans le DOM mais aucune requete XHR
        # d upload ne part vers les serveurs Fansly → la modale affiche
        # "no content selected" et le bouton final reste indisponible.
        # On utilise un drop simule via DataTransfer : on construit un File
        # JS natif a partir du contenu du fichier et on dispatche les events
        # dragenter/dragover/drop sur l input, exactement comme si l utilisateur
        # avait depose le fichier physiquement.
        inputs = Sel.file_input(page)
        n_inputs = await inputs.count()
        if n_inputs == 0:
            raise RuntimeError("Aucun input[type=file] trouve dans le composer.")
        await self._drop_file_into_input(page, inputs.nth(0), media)
        log.info("uploader_main_uploaded", media=media.name)
        await self._humanizer.long_pause()

        # 3) Attendre que la modale "Upload Media" s'ouvre
        try:
            await page.locator("app-account-media-upload").first.wait_for(
                state="visible", timeout=10_000
            )
            log.info("upload_media_modal_open")
        except PWTimeout:
            log.warning("upload_media_modal_not_visible_continuing")

        # 4) Uploader la PREVIEW dans input[1]
        # C'est la cle de la visibilite FYP : un post sans preview reste invisible
        # algorithmiquement. On utilise le meme fichier que le principal
        # (equivalent de l'option "Clone" du dropdown Add Free Preview).
        inputs_after = page.locator("input[type='file']")
        n_after = await inputs_after.count()
        log.info("uploader_inputs_after_main", count=n_after)
        if n_after >= 2:
            try:
                # Meme strategie que pour le main : drop simule via DataTransfer
                await self._drop_file_into_input(page, inputs_after.nth(1), media)
                log.info("uploader_preview_uploaded", media=media.name)
            except Exception as e:  # noqa: BLE001
                log.warning("uploader_preview_upload_failed", error=str(e))
        else:
            log.warning("uploader_preview_input_not_found",
                        hint="Fansly n'expose pas input[1] — post sans preview = pas FYP")

        await self._humanizer.short_pause()

        # 5) L'editeur de preview (app-media-editor.active-modal) s'ouvre
        # automatiquement apres set_input_files sur input[1]. On le ferme via
        # "Save Changes" (sans modifier la preview, equivalent du "clone original").
        try:
            editor = Sel.media_editor_modal(page)
            if await editor.is_visible(timeout=5_000):
                log.info("uploader_media_editor_detected")
                save_btn = Sel.media_editor_save_changes(page)
                await save_btn.wait_for(state="visible", timeout=5_000)
                await self._humanizer.hover_then_click(save_btn)
                await editor.wait_for(state="hidden", timeout=10_000)
                log.info("uploader_media_editor_saved")
                await self._humanizer.short_pause()
        except PWTimeout:
            log.debug("uploader_media_editor_not_shown")
        except Exception as e:  # noqa: BLE001
            log.warning("uploader_media_editor_error", error=str(e))

        # 6) Confirmer la modale "Media Permissions" via le bouton "Upload"
        try:
            perm_modal = Sel.media_permissions_modal(page)
            if await perm_modal.is_visible(timeout=8000):
                log.info("media_permissions_modal_detected")
                upload_btn = Sel.media_permissions_upload_button(page)
                await upload_btn.wait_for(state="visible", timeout=5000)
                await self._humanizer.hover_then_click(upload_btn)
                await perm_modal.wait_for(state="hidden", timeout=20_000)
                log.info("media_permissions_confirmed")
                await self._humanizer.short_pause()
        except PWTimeout as e:
            log.warning("media_permissions_timeout", error=str(e))
        except Exception as e:  # noqa: BLE001
            log.warning("media_permissions_handle_error", error=str(e))

        # 4) Etape "Save Changes" pour les images (best effort, optionnel)
        try:
            save_btn = Sel.save_changes_button(page)
            if await save_btn.is_visible(timeout=2500):
                await self._humanizer.hover_then_click(save_btn)
                await self._humanizer.short_pause()
        except Exception:  # noqa: BLE001
            pass

        # 5) Attendre que le bouton final "Post" devienne ACTIF
        # Fansly garde la classe "disabled" tant qu'il n'a pas fini de traiter
        # le fichier cote serveur. Cliquer pendant ce temps ne publie rien.
        submit = Sel.submit_post_button(page)
        await submit.wait_for(state="visible", timeout=30_000)

        deadline = time.monotonic() + 180
        wait_count = 0
        while time.monotonic() < deadline:
            cls = (await submit.get_attribute("class")) or ""
            if "disabled" not in cls:
                break
            wait_count += 1
            if wait_count % 10 == 0:
                log.info("uploader_waiting_server_processing", seconds_elapsed=wait_count)
            await asyncio.sleep(1.0)
        else:
            raise RuntimeError("Bouton Post est reste 'disabled' au-dela de 180s")

        log.info("uploader_submit_button_active")
        await self._humanizer.short_pause()
        await self._humanizer.hover_then_click(submit)

        # 6) Verification que la publication a abouti : URL change ou composer ferme
        try:
            await page.wait_for_function(
                """() => {
                    const modal = document.querySelector('app-account-media-upload, .modal-wrapper');
                    return !modal || modal.offsetParent === null;
                }""",
                timeout=60_000,
            )
            log.info("uploader_composer_closed")
        except PWTimeout:
            log.warning("uploader_composer_did_not_close", media=media.name)
            raise RuntimeError("Composer Fansly n'a pas ete ferme apres clic Post")

        # 7) Une eventuelle modale de confirmation "Post" peut apparaitre apres
        try:
            confirm = page.locator("div.btn.solid-blue").filter(
                has_text=re.compile(r"^\s*Post\s*$", re.I)
            ).first
            if await confirm.is_visible(timeout=3000):
                await self._humanizer.hover_then_click(confirm)
                await self._humanizer.short_pause()
        except Exception:  # noqa: BLE001
            pass

        await self._humanizer.long_pause()
        log.info("uploader_post_submitted", media=media.name)

    async def _open_composer(self, page) -> None:
        """Ouvre le composer Fansly. Strategie : essayer le bouton "+ new post"
        sur la page courante. Si rien ne s'ouvre, fallback /post/new."""
        # Tentative 1 : cliquer le bouton "+ new post"
        try:
            btn = Sel.open_composer_button(page)
            if await btn.is_visible(timeout=3000):
                await self._humanizer.hover_then_click(btn)
                await self._humanizer.short_pause()
                # Verifie qu'un textarea est apparu (modale composer ouverte)
                if await Sel.composer_textarea(page).is_visible(timeout=4000):
                    log.info("composer_opened", via="new_post_button")
                    return
        except Exception as e:  # noqa: BLE001
            log.debug("composer_button_click_failed", error=str(e))

        # Tentative 2 : navigation directe vers /post/new
        try:
            await page.goto(
                self._settings.auth.base_url + "/post/new",
                wait_until="domcontentloaded",
            )
            await self._humanizer.long_pause()
            await self._auth._dismiss_overlays(page)  # type: ignore[attr-defined]
            if await Sel.composer_textarea(page).is_visible(timeout=8000):
                log.info("composer_opened", via="direct_url")
                return
        except Exception as e:  # noqa: BLE001
            log.debug("composer_direct_url_failed", error=str(e))

        raise RuntimeError("Impossible d'ouvrir le composer Fansly")

        # 5) Attendre la fin du traitement serveur : le bouton "new post" devient
        # actif. Fallback : attendre que le composer disparaisse.
        try:
            new_post = Sel.new_post_button(page)
            await new_post.wait_for(state="visible", timeout=120_000)
            cls = (await new_post.get_attribute("class")) or ""
            # On attend qu'il ne soit plus "disabled"
            deadline = asyncio.get_event_loop().time() + 120
            while "disabled" in cls and asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(1.0)
                cls = (await new_post.get_attribute("class")) or ""
            await self._humanizer.hover_then_click(new_post)
        except PWTimeout:
            # Fallback : on considere la publication faite si le composer s'est referme.
            log.warning("uploader_new_post_btn_missing_fallback", media=media.name)

        await self._humanizer.long_pause()
        log.info("uploader_post_submitted", media=media.name)

    # ---------- attachement de fichier via drop simule ----------

    async def _drop_file_into_input(self, page: Page, input_locator, media: Path) -> None:
        """Attache un fichier a un <input type="file"> via un evenement
        drop synthetique avec DataTransfer.

        Pourquoi pas `set_input_files` (Playwright) :
            Playwright ecrit le fichier dans l input mais le change event
            n est pas toujours capte par les apps Angular qui ecoutent le
            DataTransfer / drop natif. Resultat : aucune requete XHR
            d upload n est lancee et le serveur ne recoit jamais le fichier.

        Strategie :
            1. Lire le contenu binaire du fichier cote Python.
            2. L injecter dans le navigateur en base64 via page.evaluate.
            3. Cote JS : decoder en Uint8Array → construire un File natif
               → mettre dans un DataTransfer → dispatcher
               dragenter/dragover/drop sur l element cible.
            Le browser traite l upload comme s il venait d un vrai user.

        Cout : transit base64 via CDP (gros pour videos). Acceptable
        pour des medias < 100 Mo. Si plus gros, prevoir un serveur HTTP
        local de fichiers et faire le drop via une URL.
        """
        file_bytes = media.read_bytes()
        if not file_bytes:
            raise RuntimeError(f"Fichier vide : {media}")
        b64 = base64.b64encode(file_bytes).decode("ascii")
        mime, _ = mimetypes.guess_type(media.name)
        if not mime:
            mime = "application/octet-stream"
        log.info(
            "drop_file_starting",
            media=media.name,
            size_bytes=len(file_bytes),
            mime=mime,
        )

        # On utilise evaluate_handle pour passer l input locator au JS.
        element = await input_locator.element_handle()
        if element is None:
            raise RuntimeError("Input file introuvable au moment du drop.")

        await page.evaluate(
            """
            async ({input, name, b64, mime}) => {
                // Decodage base64 → Uint8Array
                const binStr = atob(b64);
                const len = binStr.length;
                const bytes = new Uint8Array(len);
                for (let i = 0; i < len; i++) bytes[i] = binStr.charCodeAt(i);

                const file = new File([bytes], name, {type: mime, lastModified: Date.now()});
                const dt = new DataTransfer();
                dt.items.add(file);

                // Pose le fichier dans input.files puis dispatch UN SEUL event :
                // `change`, l event standard que tout composant qui consomme un
                // <input type="file"> ecoute. Avec bubbles=true mais SANS les
                // events drag-drop (qui creaient 3 uploads dupliques par
                // propagation aux containers parents Angular).
                try {
                    Object.defineProperty(input, 'files', {
                        value: dt.files,
                        writable: false,
                        configurable: true,
                    });
                } catch (e) {
                    // Si redefinition refusee, on tente quand meme le change.
                }
                input.dispatchEvent(new Event('change', {bubbles: true, composed: true}));
            }
            """,
            {"input": element, "name": media.name, "b64": b64, "mime": mime},
        )
        log.info("drop_file_done", media=media.name)

    # ---------- post-traitement ----------

    def _mark_published(
        self, media: Path, caption: str, batch_name: str, cycle: int
    ) -> None:
        """Mode lot : on NE deplace PAS le fichier (il sera republie au prochain
        cycle). On enregistre la publication en BDD et on marque le fichier
        comme publie dans le cycle courant."""
        self._state.add_to_batch_published(media.name)
        self._state.record_media_published(
            media.name, caption,
            batch_name=batch_name, cycle_number=cycle,
            run_id=self._run_id,
        )

    async def _dump_artifact(self, label: str, media_name: str) -> None:
        from datetime import datetime, timezone

        try:
            page = await self._session.page()
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            out = self._settings.paths.artifacts_dir / "upload" / f"{stamp}_{label}_{media_name}"
            out.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(out / "screen.png"), full_page=True)
            (out / "page.html").write_text(await page.content(), encoding="utf-8")
            log.info("uploader_artifact_saved", path=str(out))
        except Exception as e:  # noqa: BLE001
            log.warning("uploader_artifact_dump_failed", error=str(e))


