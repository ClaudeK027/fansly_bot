"""Point d'entree Streamlit du Fansly Manager.

Lance via : streamlit run src/fansly_manager/main.py --server.port=8500
"""
from __future__ import annotations

import streamlit as st

from fansly_manager import instances as inst_mod

st.set_page_config(
    page_title="Fansly Manager",
    page_icon="🎛️",
    layout="wide",
    initial_sidebar_state="expanded",
)


def _render_sidebar() -> str | None:
    """Sidebar : selecteur d'instance + actions par instance.

    Retourne le nom de l'instance courante (None si vue d'ensemble)."""
    st.sidebar.title("🎛️ Fansly Manager")
    st.sidebar.caption("Gestion multi-comptes (instances Docker isolees)")

    instances = inst_mod.list_instances()

    if not instances:
        st.sidebar.info(
            "Aucune instance n'existe encore.\n\n"
            "Pour en créer une, lance dans ton terminal :\n\n"
            "```bash\n./scripts/new-instance.sh NAME\n```"
        )
        return None

    # Selecteur : "Vue d'ensemble" + liste des instances
    options = ["📊 Vue d'ensemble"] + [
        f"{'🟢' if i.is_running else '🔴'} {i.name}" for i in instances
    ]
    choice = st.sidebar.radio(
        "Compte actif",
        options,
        key="current_choice",
        label_visibility="collapsed",
    )

    if choice.startswith("📊"):
        return None

    # Extrait le nom de l'option formatee
    chosen_name = choice.split(" ", 1)[1]

    # Actions sur le compte selectionne
    st.sidebar.divider()
    current = inst_mod.get_instance(chosen_name)
    if current is None:
        st.sidebar.error("Instance disparue.")
        return None

    if current.is_running:
        st.sidebar.success(f"🟢 {current.name} — running")
        if current.dashboard_url:
            st.sidebar.markdown(
                f"**Dashboard** : [{current.dashboard_url}]({current.dashboard_url})"
            )
        c1, c2 = st.sidebar.columns(2)
        with c1:
            if st.button("⏹ Stopper", key="btn_stop", use_container_width=True):
                inst_mod.stop_instance(current.name)
                st.rerun()
        with c2:
            if st.button("🔄 Redémarrer", key="btn_restart", use_container_width=True):
                inst_mod.restart_instance(current.name)
                st.rerun()
    else:
        st.sidebar.warning(f"🔴 {current.name} — {current.status}")
        if st.sidebar.button("▶️ Démarrer", key="btn_start", use_container_width=True):
            inst_mod.start_instance(current.name)
            st.rerun()

    st.sidebar.divider()
    st.sidebar.caption(
        "**Ajouter un compte** :\n\n"
        "```bash\n./scripts/new-instance.sh NEW_NAME\n```\n\n"
        "_(le wizard noVNC arrive en Phase 3.)_"
    )

    return chosen_name


def _render_overview() -> None:
    """Vue d'ensemble : tableau de toutes les instances avec leur status."""
    st.title("📊 Vue d'ensemble — Toutes les instances")

    instances = inst_mod.list_instances()
    if not instances:
        st.info(
            "Aucune instance pour l'instant.\n\n"
            "Crée-en une avec `./scripts/new-instance.sh NAME` puis recharge cette page."
        )
        return

    # KPIs en haut
    running = sum(1 for i in instances if i.is_running)
    total = len(instances)
    col1, col2, col3 = st.columns(3)
    col1.metric("Instances totales", total)
    col2.metric("Running", running)
    col3.metric("Arrêtées", total - running)

    st.divider()

    # Tableau detaille
    rows = []
    for i in instances:
        rows.append({
            "Compte": i.name,
            "Status": "🟢 running" if i.is_running else f"🔴 {i.status}",
            "Port": i.host_port if i.host_port else "—",
            "Dashboard": i.dashboard_url if i.dashboard_url else "—",
            "Container": i.container,
        })
    st.dataframe(rows, use_container_width=True, hide_index=True)


def _render_instance_dashboard(name: str) -> None:
    """Vue d'une instance : iframe vers son dashboard Streamlit."""
    inst = inst_mod.get_instance(name)
    if inst is None:
        st.error(f"Instance {name} introuvable.")
        return

    st.title(f"🎯 Dashboard — {inst.name}")
    if not inst.is_running:
        st.error(
            f"L'instance **{inst.name}** est actuellement **{inst.status}**.\n\n"
            "Démarre-la depuis la sidebar pour accéder à son dashboard."
        )
        return

    if inst.dashboard_url is None:
        st.warning(
            "Aucun port host mappe pour cette instance. "
            "Verifie la config docker-compose."
        )
        return

    # Iframe vers le dashboard de l'instance. L'utilisateur accede au manager
    # via tunnel SSH sur 8500, et le dashboard de l'instance est sur 8501+.
    # L'iframe pointe vers localhost:<port> qui resout cote browser de l'user
    # (donc passe par le tunnel SSH si on est sur VPS).
    st.caption(f"Dashboard intégré depuis {inst.dashboard_url}")
    st.components.v1.iframe(
        src=inst.dashboard_url,
        height=900,
        scrolling=True,
    )


def main() -> None:
    chosen = _render_sidebar()
    if chosen is None:
        _render_overview()
    else:
        _render_instance_dashboard(chosen)


main()
