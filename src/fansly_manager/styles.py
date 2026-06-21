"""CSS injecte une seule fois au demarrage du Streamlit. Conventions :

- variables CSS dans `:root` pour les couleurs et radius
- classes BEM-like (`.fm-card`, `.fm-status`)
- pas de `!important` sauf si Streamlit override agressif
- prefix `fm-` (fansly manager) pour eviter les conflits
"""
from __future__ import annotations

import streamlit as st


_CSS = """
:root {
    --fm-bg: #0e1117;
    --fm-bg-card: #1a1f2e;
    --fm-bg-card-hover: #20253a;
    --fm-border: #2a3142;
    --fm-text: #e8ebf2;
    --fm-text-muted: #8b93a8;
    --fm-text-dim: #5b6275;
    --fm-radius: 8px;
    --fm-radius-sm: 4px;

    /* status palette */
    --fm-green: #4ade80;
    --fm-green-bg: rgba(74, 222, 128, 0.12);
    --fm-red: #f87171;
    --fm-red-bg: rgba(248, 113, 113, 0.12);
    --fm-amber: #fbbf24;
    --fm-amber-bg: rgba(251, 191, 36, 0.12);
    --fm-gray: #8b93a8;
    --fm-gray-bg: rgba(139, 147, 168, 0.12);

    --fm-accent: #60a5fa;
    --fm-accent-bg: rgba(96, 165, 250, 0.12);
}

/* ---- Card container ---- */
.fm-card {
    background: var(--fm-bg-card);
    border: 1px solid var(--fm-border);
    border-radius: var(--fm-radius);
    padding: 24px;
    margin-bottom: 16px;
    transition: border-color 0.15s ease;
}
.fm-card:hover {
    border-color: var(--fm-text-dim);
}

.fm-card-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 20px;
    padding-bottom: 16px;
    border-bottom: 1px solid var(--fm-border);
}
.fm-card-title {
    font-size: 1.25rem;
    font-weight: 600;
    color: var(--fm-text);
    margin: 0;
    letter-spacing: -0.01em;
}

/* ---- Status pill (color-coded badge) ---- */
.fm-status {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 4px 12px;
    border-radius: 999px;
    font-size: 0.75rem;
    font-weight: 600;
    letter-spacing: 0.05em;
    text-transform: uppercase;
}
/* Glyphe geometrique typographique (accessibilite daltoniens) */
.fm-status-glyph {
    font-size: 0.85em;
    line-height: 1;
    display: inline-flex;
    align-items: center;
    text-shadow: 0 0 8px currentColor;
}
.fm-status-running  { color: var(--fm-green);  background: var(--fm-green-bg); }
.fm-status-stopped  { color: var(--fm-red);    background: var(--fm-red-bg); }
.fm-status-restart  { color: var(--fm-amber);  background: var(--fm-amber-bg); }
.fm-status-unknown  { color: var(--fm-gray);   background: var(--fm-gray-bg); }

/* ---- Metric grid (label/value pairs) ---- */
.fm-metrics {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: 16px 24px;
    margin-bottom: 20px;
}
.fm-metric-label {
    font-size: 0.7rem;
    font-weight: 600;
    color: var(--fm-text-muted);
    letter-spacing: 0.08em;
    text-transform: uppercase;
    margin-bottom: 4px;
}
.fm-metric-value {
    font-size: 0.95rem;
    font-weight: 500;
    color: var(--fm-text);
    font-variant-numeric: tabular-nums;
    word-break: break-all;
}
.fm-metric-value-dim {
    color: var(--fm-text-dim);
    font-style: italic;
}

/* ---- Card actions row ---- */
.fm-card-actions {
    display: flex;
    gap: 12px;
    flex-wrap: wrap;
    align-items: center;
}

/* ---- Card "Detail technique" expand ---- */
.fm-card-tech {
    margin-top: 20px;
    padding-top: 16px;
    border-top: 1px solid var(--fm-border);
}
.fm-card-tech summary {
    font-size: 0.72rem;
    font-weight: 600;
    color: var(--fm-text-muted);
    letter-spacing: 0.08em;
    text-transform: uppercase;
    cursor: pointer;
    user-select: none;
    list-style: none;
    display: inline-flex;
    align-items: center;
    gap: 8px;
}
.fm-card-tech summary::-webkit-details-marker { display: none; }
.fm-card-tech summary::before {
    content: "+";
    font-family: monospace;
    font-size: 1.1em;
    width: 12px;
    text-align: center;
    color: var(--fm-text-dim);
}
.fm-card-tech[open] summary::before { content: "−"; }
.fm-card-tech summary:hover { color: var(--fm-text); }
.fm-tech-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 12px 24px;
    margin-top: 12px;
}
.fm-mono {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 0.85rem;
}

/* ---- Primary action button (open dashboard, new tab) ---- */
.fm-btn-primary {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    padding: 10px 20px;
    border-radius: var(--fm-radius-sm);
    background: var(--fm-accent);
    color: #0a1428 !important;
    font-size: 0.9rem;
    font-weight: 600;
    text-decoration: none;
    transition: filter 0.15s ease, transform 0.05s ease;
}
.fm-btn-primary:hover { filter: brightness(1.1); text-decoration: none; }
.fm-btn-primary:active { transform: translateY(1px); }
.fm-btn-primary.disabled,
.fm-btn-primary[aria-disabled="true"] {
    background: var(--fm-border);
    color: var(--fm-text-dim) !important;
    pointer-events: none;
}

/* ---- KPI cards (top of overview) ---- */
.fm-kpi-row {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 16px;
    margin-bottom: 32px;
}
.fm-kpi {
    background: var(--fm-bg-card);
    border: 1px solid var(--fm-border);
    border-radius: var(--fm-radius);
    padding: 20px 24px;
}
.fm-kpi-label {
    font-size: 0.72rem;
    font-weight: 600;
    color: var(--fm-text-muted);
    letter-spacing: 0.08em;
    text-transform: uppercase;
    margin-bottom: 8px;
}
/* KPI value : taille < page-title pour preserver la hierarchie typo */
.fm-kpi-value {
    font-size: 1.5rem;
    font-weight: 700;
    color: var(--fm-text);
    line-height: 1;
    font-variant-numeric: tabular-nums;
}
.fm-kpi-value-running { color: var(--fm-green); }
.fm-kpi-value-stopped { color: var(--fm-red); }

/* ---- Page header ---- */
.fm-page-title {
    font-size: 1.75rem;
    font-weight: 700;
    color: var(--fm-text);
    letter-spacing: -0.02em;
    margin-bottom: 4px;
}
.fm-page-subtitle {
    font-size: 0.95rem;
    color: var(--fm-text-muted);
    margin-bottom: 32px;
}

/* ---- Empty state ---- */
.fm-empty {
    background: var(--fm-bg-card);
    border: 1px dashed var(--fm-border);
    border-radius: var(--fm-radius);
    padding: 48px 24px;
    text-align: center;
}
.fm-empty-title {
    font-size: 1.1rem;
    font-weight: 600;
    color: var(--fm-text);
    margin-bottom: 8px;
}
.fm-empty-body {
    font-size: 0.9rem;
    color: var(--fm-text-muted);
    line-height: 1.5;
}
.fm-empty-code {
    display: inline-block;
    margin-top: 16px;
    padding: 8px 16px;
    background: var(--fm-bg);
    border: 1px solid var(--fm-border);
    border-radius: var(--fm-radius-sm);
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 0.85em;
    color: var(--fm-text);
}

/* ---- Sidebar polish ---- */
[data-testid="stSidebar"] h1 {
    font-size: 1.05rem;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--fm-text);
}

.fm-sidebar-section {
    font-size: 0.7rem;
    font-weight: 600;
    color: var(--fm-text-muted);
    letter-spacing: 0.08em;
    text-transform: uppercase;
    margin: 20px 0 8px 0;
    display: flex;
    align-items: center;
    gap: 6px;
}
.fm-sidebar-count {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 18px;
    height: 18px;
    padding: 0 6px;
    border-radius: 999px;
    background: var(--fm-border);
    color: var(--fm-text);
    font-size: 0.7rem;
    font-weight: 600;
    letter-spacing: 0;
    text-transform: none;
}

.fm-sidebar-link {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 8px 12px;
    margin-bottom: 4px;
    border-radius: var(--fm-radius-sm);
    color: var(--fm-text);
    text-decoration: none;
    font-size: 0.9rem;
    transition: background 0.12s ease;
    border-left: 3px solid transparent;
}
.fm-sidebar-link:hover {
    background: var(--fm-bg-card);
    text-decoration: none;
}
.fm-sidebar-link::before {
    content: "";
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: currentColor;
    opacity: 0.85;
    flex-shrink: 0;
}
.fm-sidebar-link-running {
    color: var(--fm-text);
    border-left-color: var(--fm-green);
}
.fm-sidebar-link-running::before { background: var(--fm-green); }
.fm-sidebar-link-stopped {
    color: var(--fm-text-muted);
    border-left-color: var(--fm-red);
}
.fm-sidebar-link-stopped::before { background: var(--fm-red); opacity: 0.6; }

/* ---- Headings (Streamlit-emitted) ---- */
h1, h2, h3 { letter-spacing: -0.015em; }
"""


def inject() -> None:
    """A appeler une fois au tout debut du main Streamlit."""
    st.markdown(f"<style>{_CSS}</style>", unsafe_allow_html=True)
