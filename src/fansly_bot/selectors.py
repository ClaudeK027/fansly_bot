# src/fansly_bot/selectors.py
"""Selecteurs Playwright centralises pour Fansly.

Realites observees sur le DOM Fansly (Angular SPA, mai 2026) :
  - Les boutons sont des `<div class="btn ...">`, PAS des `<button>` →
    impossible d'utiliser `get_by_role("button")`.
  - Les classes ngcontent (`_ngcontent-ng-cXXX`) sont generees a chaque build,
    inutilisables.
  - Les modales / composers sont des composants Angular `<app-XXX>` →
    on cible par tag racine du composant.
  - Le composer n'est pas dans le DOM par defaut sur /home — il faut
    naviguer vers /post/new (ou ouvrir une modale).
"""

from __future__ import annotations

import re

from playwright.async_api import Locator, Page


# ------- helpers -------

def _btn_with_text(page: Page, text: str | re.Pattern) -> Locator:
    """`<div class="btn ...">` Fansly contenant un texte donne."""
    return page.locator("div.btn, button.btn").filter(has_text=text).first


def _btn_within(scope: Locator, text: str | re.Pattern) -> Locator:
    return scope.locator("div.btn, button.btn").filter(has_text=text).first


class Sel:
    # ------- Age gate -------
    @staticmethod
    def age_gate_enter(page: Page) -> Locator:
        return _btn_with_text(page, re.compile(r"^\s*Enter\b", re.I))

    # ------- Login -------
    @staticmethod
    def header_login_button(page: Page) -> Locator:
        return _btn_with_text(page, re.compile(r"^\s*Login\s*$", re.I))

    @staticmethod
    def username_input(page: Page) -> Locator:
        return page.locator(
            "input[name='username'], input[autocomplete='username']"
        ).first

    @staticmethod
    def password_input(page: Page) -> Locator:
        return page.locator(
            "input[type='password'], input[autocomplete='current-password']"
        ).first

    @staticmethod
    def signin_submit(page: Page) -> Locator:
        return _btn_with_text(page, re.compile(r"Sign\s*in", re.I))

    # ------- Overlays / modales -------
    @staticmethod
    def push_notifications_modal(page: Page) -> Locator:
        return page.locator("app-web-push-enable-modal").first

    @staticmethod
    def push_notifications_maybe_later(page: Page) -> Locator:
        return Sel.push_notifications_modal(page).locator(
            "div.btn, button.btn"
        ).filter(has_text=re.compile(r"Maybe\s*Later", re.I)).first

    @staticmethod
    def generic_maybe_later(page: Page) -> Locator:
        return page.locator("div.btn, button.btn").filter(
            has_text=re.compile(r"Maybe\s*Later", re.I)
        ).first

    @staticmethod
    def cookie_accept(page: Page) -> Locator:
        return page.locator("div.btn, button.btn").filter(
            has_text=re.compile(r"(Accept All|Essential Only|I Accept)", re.I)
        ).first

    @staticmethod
    def email_verification_banner(page: Page) -> Locator:
        return page.locator("text=/email is not yet verified/i").first

    # ------- Composer / Upload -------
    @staticmethod
    def open_composer_button(page: Page) -> Locator:
        """Bouton "+ new post". Sur mobile : `.mobile-new-post-button`.
        Sur desktop : un bouton dans la sidebar ou la nav."""
        return page.locator(
            ".mobile-new-post-button, "
            "div.new-post-btn, "
            "div.btn:has-text('New Post'), "
            "div.btn:has-text('Create Post'), "
            "[aria-label*='new post' i], "
            "[aria-label*='create post' i]"
        ).first

    @staticmethod
    def composer_textarea(page: Page) -> Locator:
        return page.locator("textarea").first

    @staticmethod
    def file_input(page: Page) -> Locator:
        return page.locator("input[type='file']")

    @staticmethod
    def save_changes_button(page: Page) -> Locator:
        return _btn_with_text(page, re.compile(r"Save\s*Changes", re.I))

    # ------- Modale "Media Permissions" qui apparait apres l'upload -------
    @staticmethod
    def media_permissions_modal(page: Page) -> Locator:
        return page.locator("app-account-media-upload").first

    @staticmethod
    def media_permissions_upload_button(page: Page) -> Locator:
        """Bouton "Upload" du footer de app-account-media-upload qui confirme
        les permissions et ferme la modale. Texte exact "Upload" (pas "Upload New")."""
        return page.locator(
            "app-account-media-upload .modal-footer div.btn.solid-blue, "
            "app-account-media-upload .modal-footer button.btn.solid-blue"
        ).filter(has_text=re.compile(r"^\s*Upload\s*$", re.I)).first

    # ------- Editeur de preview (s'ouvre apres set_input_files sur input[1]) -------
    @staticmethod
    def media_editor_modal(page: Page) -> Locator:
        return page.locator("app-media-editor.active-modal").first

    @staticmethod
    def media_editor_save_changes(page: Page) -> Locator:
        """Bouton 'Save Changes' de l'editeur de preview. Boutons disponibles :
        Discard | Save Changes."""
        return Sel.media_editor_modal(page).locator(
            "div.btn.solid-blue, button.btn.solid-blue, div.btn:has-text('Save'), button.btn:has-text('Save')"
        ).filter(has_text=re.compile(r"Save\s*Changes", re.I)).first

    @staticmethod
    def submit_post_button(page: Page) -> Locator:
        """Bouton de validation de la modale composer."""
        return page.locator("div.btn.solid-blue, button.btn.solid-blue").filter(
            has_text=re.compile(r"^(Post|Publish|Submit)$", re.I)
        ).first

    @staticmethod
    def new_post_action_button(page: Page) -> Locator:
        """Bouton de finalisation post-traitement upload."""
        return page.locator("div.new-post-btn, button.new-post-btn").first

    # ------- Feed / posts (purge) -------
    # Structure Fansly observee : chaque post est un <app-post class="feed-item">
    # contenant `.feed-item-timestamp` (texte relatif "7m", "1d", "Jul 14, 2025"),
    # `.feed-item-description` (legende), `.feed-item-actions` (menu options).
    @staticmethod
    def feed_items(page: Page) -> Locator:
        return page.locator("app-post.feed-item, app-post")

    @staticmethod
    def feed_item_time(item: Locator) -> Locator:
        return item.locator(".feed-item-timestamp, time").first

    @staticmethod
    def feed_item_caption(item: Locator) -> Locator:
        return item.locator(".feed-item-description, .feed-item-content").first

    @staticmethod
    def feed_item_meta_menu(item: Locator) -> Locator:
        return item.locator(".feed-item-actions, .more-dropdown").first

    @staticmethod
    def feed_item_delete_entry(page: Page) -> Locator:
        return page.locator("div.dropdown-item, [role='menuitem']").filter(
            has_text=re.compile(r"Delete\s*Post", re.I)
        ).first

    @staticmethod
    def confirm_yes(page: Page) -> Locator:
        return _btn_with_text(page, re.compile(r"^(Yes|Confirm|Delete)$", re.I))
