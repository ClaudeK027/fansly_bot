"""Composants UI reutilisables (HTML rendu via st.markdown).

Tous emis sans emoji. Status indicators = badges CSS color-coded.
"""
from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Iterable, Optional, Tuple

import streamlit as st

from .instances import Instance


# ─── Status helpers ──────────────────────────────────────────────────────

# Pour l'accessibilite (daltoniens deuteranopes/protanopes), on ajoute un
# symbole geometrique typographique distinct par status — distinguer par la
# forme ET la couleur, pas la couleur seule. Ces caracteres Unicode sont
# des symboles geometriques (pas des emoji decoratifs).
_STATUS_META = {
    "running":    ("fm-status-running", "●"),  # ● cercle plein
    "exited":     ("fm-status-stopped", "■"),  # ■ carre
    "dead":       ("fm-status-stopped", "■"),
    "paused":     ("fm-status-stopped", "▮"),  # ▮ rectangle
    "restarting": ("fm-status-restart", "◐"),  # ◐ cercle moitie
    "created":    ("fm-status-restart", "◐"),
}
_STATUS_DEFAULT = ("fm-status-unknown", "○")   # ○ cercle vide


def status_pill(status: str) -> str:
    """Retourne le HTML d'un badge status color-coded + symbole geometrique.

    Accessibilite : la couleur seule ne suffit pas pour les daltoniens —
    on ajoute un symbole Unicode distinct par status (cercle, carre, etc.).
    Inputs html.escape pour eviter toute injection si status venait d'un
    nom de container malicieux.

    Exemple : ``status_pill("running")`` -> badge vert "● RUNNING".
    """
    css_class, glyph = _STATUS_META.get(status.lower(), _STATUS_DEFAULT)
    label = html.escape(status.upper())
    return (
        f'<span class="fm-status {css_class}">'
        f'<span class="fm-status-glyph" aria-hidden="true">{glyph}</span>'
        f'<span>{label}</span>'
        f'</span>'
    )


# ─── Time helpers ────────────────────────────────────────────────────────

def _parse_iso(ts: str) -> Optional[datetime]:
    """Parse un timestamp Docker (RFC3339 avec nanoseconds parfois).

    Docker emet ``"2026-06-21T15:54:06.123456789Z"`` — Python's fromisoformat
    n'accepte pas les nanoseconds, on tronque a 6 chiffres (microseconds).
    """
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        # Tronque les nanoseconds excedentaires : ".123456789+00:00" -> ".123456+00:00"
        if "." in ts:
            head, dot_tail = ts.split(".", 1)
            frac, _, tz = dot_tail.partition("+")
            if not tz:
                frac, _, tz_minus = dot_tail.partition("-")
                tz = "-" + tz_minus if tz_minus else ""
            else:
                tz = "+" + tz
            ts = f"{head}.{frac[:6]}{tz}"
        return datetime.fromisoformat(ts)
    except (ValueError, AttributeError):
        return None


def humanize_duration(seconds: float) -> str:
    """Format human-readable d'une duree en secondes.

    Verifie ``seconds < 0`` AVANT la conversion en int (sinon int(-0.5) = 0
    et on rendrait "0 s" pour une valeur negative au lieu de "—").

    Exemples :
        humanize_duration(45)    -> "45 s"
        humanize_duration(3700)  -> "1 h"  (les minutes 1 sont arrondies hors)
        humanize_duration(3720)  -> "1 h 2 min"
        humanize_duration(86400) -> "1 j"
        humanize_duration(-5)    -> "—"
    """
    try:
        s_float = float(seconds)
    except (TypeError, ValueError):
        return "—"
    if s_float < 0:
        return "—"
    s = int(s_float)
    if s < 60:
        return f"{s} s"
    minutes, _ = divmod(s, 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h" if minutes == 0 else f"{hours} h {minutes} min"
    days, hours = divmod(hours, 24)
    return f"{days} j" if hours == 0 else f"{days} j {hours} h"


def uptime_from_started_at(started_at_iso: str) -> str:
    """Calcule l'uptime depuis un timestamp ISO Docker."""
    started = _parse_iso(started_at_iso)
    if started is None:
        return "—"
    delta = datetime.now(timezone.utc) - started
    return humanize_duration(delta.total_seconds())


# ─── KPI cards (top of overview) ─────────────────────────────────────────

def kpi_row(items: Iterable[Tuple[str, str, str]]) -> None:
    """Rend une rangee de KPIs (label, value, css_modifier).

    css_modifier : ``""`` (defaut), ``"running"``, ``"stopped"``.
    """
    parts = ['<div class="fm-kpi-row">']
    for label, value, modifier in items:
        css = "fm-kpi-value"
        if modifier:
            css += f" fm-kpi-value-{modifier}"
        parts.append(
            f'<div class="fm-kpi">'
            f'<div class="fm-kpi-label">{html.escape(label)}</div>'
            f'<div class="{css}">{html.escape(str(value))}</div>'
            f'</div>'
        )
    parts.append("</div>")
    st.markdown("".join(parts), unsafe_allow_html=True)


# ─── Instance card (overview) ────────────────────────────────────────────

def instance_card(inst: Instance) -> None:
    """Card complete pour une instance : titre + status + metrics + actions.

    UX :
      - Metrics scannables : port + uptime (cible non-technique)
      - Container + image masques par defaut (details techniques, jargon)
      - Bouton primaire "Ouvrir" = anchor HTML target=_blank
      - Anchor ``id`` pour permettre le scroll-to depuis la sidebar

    Tous les inputs (name, container, image) passent par html.escape.
    """
    name = html.escape(inst.name)
    container = html.escape(inst.container)
    image = html.escape(inst.image)
    port = str(inst.host_port) if inst.host_port else "—"
    uptime = (
        uptime_from_started_at(inst.started_at) if inst.started_at else "—"
    )
    anchor_id = f"fm-card-{html.escape(inst.name)}"

    dashboard_url = inst.dashboard_url
    if dashboard_url:
        open_btn = (
            f'<a href="{html.escape(dashboard_url)}" '
            f'target="_blank" rel="noopener noreferrer" '
            f'class="fm-btn-primary">'
            f'Ouvrir le dashboard'
            f'</a>'
        )
    else:
        open_btn = (
            '<span class="fm-btn-primary disabled" aria-disabled="true">'
            'Dashboard indisponible</span>'
        )

    # Metrics user-visibles : port + uptime (les seules vraiment utiles
    # pour une cible non-technique). On reserve container + image pour un
    # bloc "details techniques" visuellement secondaire.
    html_card = f"""
    <div class="fm-card" id="{anchor_id}">
      <div class="fm-card-header">
        <div class="fm-card-title">{name}</div>
        {status_pill(inst.status)}
      </div>
      <div class="fm-metrics">
        <div>
          <div class="fm-metric-label">Port</div>
          <div class="fm-metric-value">{port}</div>
        </div>
        <div>
          <div class="fm-metric-label">En ligne depuis</div>
          <div class="fm-metric-value">{uptime}</div>
        </div>
      </div>
      <div class="fm-card-actions">
        {open_btn}
      </div>
      <details class="fm-card-tech">
        <summary>Detail technique</summary>
        <div class="fm-tech-grid">
          <div>
            <div class="fm-metric-label">Container</div>
            <div class="fm-metric-value fm-mono">{container}</div>
          </div>
          <div>
            <div class="fm-metric-label">Image</div>
            <div class="fm-metric-value fm-mono">{image}</div>
          </div>
        </div>
      </details>
    </div>
    """
    st.markdown(html_card, unsafe_allow_html=True)


# ─── Empty state ─────────────────────────────────────────────────────────

def empty_state(
    title: str,
    body: str,
    code: Optional[str] = None,
) -> None:
    """Encart affiche quand il n'y a aucune instance a montrer.

    API safe : tous les champs sont html.escape automatiquement.
    Pas de HTML brut accepte (par design — anti-XSS, anti-erreur futur).

    Args:
        title: titre court de l'etat vide
        body: corps texte (paragraphe, escape applique)
        code: extrait code optionnel rendu en monospace inline-block
    """
    parts = [
        '<div class="fm-empty">',
        f'<div class="fm-empty-title">{html.escape(title)}</div>',
        f'<div class="fm-empty-body">{html.escape(body)}</div>',
    ]
    if code:
        parts.append(
            f'<div class="fm-empty-code">{html.escape(code)}</div>'
        )
    parts.append("</div>")
    st.markdown("".join(parts), unsafe_allow_html=True)


# ─── Page header ─────────────────────────────────────────────────────────

def page_header(title: str, subtitle: Optional[str] = None) -> None:
    """Titre de page en haut a gauche, hierarchie typo coherente."""
    st.markdown(
        f'<div class="fm-page-title">{html.escape(title)}</div>',
        unsafe_allow_html=True,
    )
    if subtitle:
        st.markdown(
            f'<div class="fm-page-subtitle">{html.escape(subtitle)}</div>',
            unsafe_allow_html=True,
        )
