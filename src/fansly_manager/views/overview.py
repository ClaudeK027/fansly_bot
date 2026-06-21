"""Vue d'ensemble : KPI cards en haut + grille de cards d'instances.

Recoit la liste d'instances en parametre (deja resolue + cachee dans
main.py) — pas de double-call Docker socket.
"""
from __future__ import annotations

import streamlit as st

from fansly_manager import components, instances as inst_mod


def _invalidate_cache() -> None:
    """Force le prochain render a re-fetch Docker (apres une action)."""
    from fansly_manager.main import _cached_list_instances

    _cached_list_instances.clear()


def _do_action(label: str, result: inst_mod.Result, instance_name: str) -> None:
    """Centralise le feedback erreur + invalidation cache + rerun."""
    if result.ok:
        st.toast(f"{label} : {instance_name}")
        _invalidate_cache()
        st.rerun()
    else:
        st.error(f"Echec ({label} {instance_name}) : {result.error}")


def render(instances: list[inst_mod.Instance]) -> None:
    components.page_header(
        "Vue d'ensemble",
        "Tous les bots Fansly sur cette machine",
    )

    if not instances:
        components.empty_state(
            "Aucune instance configuree",
            "Cree une premiere instance depuis ton terminal. "
            "Une fois creee, elle apparaitra ici automatiquement.",
            code="./scripts/new-instance.sh NAME",
        )
        return

    # KPI cards
    total = len(instances)
    running = sum(1 for i in instances if i.is_running)
    stopped = total - running
    components.kpi_row([
        ("Instances totales", str(total), ""),
        ("En fonctionnement", str(running), "running" if running > 0 else ""),
        ("Arretees", str(stopped), "stopped" if stopped > 0 else ""),
    ])

    # Cards
    for inst in instances:
        components.instance_card(inst)
        # Actions sous chaque card (boutons Streamlit pour le callback Python).
        # Le bouton "Ouvrir" reste DANS la card (anchor HTML, primaire visuel).
        action_cols = st.columns([1, 1, 4])
        if inst.is_running:
            with action_cols[0]:
                if st.button(
                    "Arreter",
                    key=f"stop_{inst.name}",
                    use_container_width=True,
                ):
                    with st.spinner(f"Arret de {inst.name}..."):
                        result = inst_mod.safe_stop_instance(inst.name)
                    _do_action("Arrete", result, inst.name)
            with action_cols[1]:
                if st.button(
                    "Redemarrer",
                    key=f"restart_{inst.name}",
                    use_container_width=True,
                ):
                    with st.spinner(f"Redemarrage de {inst.name}..."):
                        result = inst_mod.safe_restart_instance(inst.name)
                    _do_action("Redemarre", result, inst.name)
        else:
            with action_cols[0]:
                if st.button(
                    "Demarrer",
                    key=f"start_{inst.name}",
                    type="primary",
                    use_container_width=True,
                ):
                    with st.spinner(f"Demarrage de {inst.name}..."):
                        result = inst_mod.safe_start_instance(inst.name)
                    _do_action("Demarre", result, inst.name)

        st.markdown('<div style="height: 24px;"></div>', unsafe_allow_html=True)
