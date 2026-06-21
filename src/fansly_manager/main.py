"""Point d'entree Streamlit du Fansly Manager.

Architecture :
- ``main()`` charge UNE FOIS la liste des instances (cache 3s) puis la
  passe a la sidebar et a la vue. Evite les doubles round-trips Docker
  par render Streamlit.
- En cas d'echec Docker (daemon DOWN, socket EACCES), on affiche un
  ecran d'erreur clair plutot que crasher la page.
- La sidebar permet de scroll-to-anchor sur une card via ancre HTML
  (``#fm-card-NAME``) — pas de st.button qui ferait juste rerun pour
  rien.

Lance via : ``streamlit run src/fansly_manager/main.py --server.port=8500``
"""
from __future__ import annotations

from typing import Optional

import streamlit as st

from fansly_manager import instances as inst_mod
from fansly_manager import styles
from fansly_manager import wizard as wiz_mod
from fansly_manager.views import overview as overview_view
from fansly_manager.views import wizard as wizard_view


st.set_page_config(
    page_title="Fansly Manager",
    page_icon=None,
    layout="wide",
    initial_sidebar_state="expanded",
    menu_items={"Get Help": None, "Report a bug": None, "About": None},
)

styles.inject()


@st.cache_resource(show_spinner=False)
def _boot_cleanup() -> Optional[str]:
    """Garbage collector au demarrage du manager.

    @st.cache_resource garantit UNE seule execution par process (pas un
    par re-render). On supprime les containers fansly-novnc-* orphelins
    crees lors de wizards interrompus (fermeture onglet, crash manager).

    Retourne un message de log a afficher dans la sidebar si du cleanup
    a eu lieu, None sinon.
    """
    result = wiz_mod.cleanup_stale_novnc_containers()
    if result.ok and result.value:
        return f"GC : {len(result.value)} container(s) noVNC orphelin(s) supprime(s)."
    if not result.ok:
        # Daemon down -> on laisse le _render_docker_error main gerer
        return None
    return None


_BOOT_CLEANUP_MSG = _boot_cleanup()


@st.cache_data(ttl=3, show_spinner=False)
def _cached_list_instances() -> tuple[list[dict], Optional[str]]:
    """Cache 3s pour eviter les double-calls sidebar+vue.

    Retourne (instances_as_dicts, error_msg). On serialise en dict pour
    que st.cache_data puisse hash le resultat (les dataclass sont OK
    aussi mais cache_data prefere les types primitifs).
    """
    result = inst_mod.safe_list_instances()
    if not result.ok:
        return [], result.error
    return [_inst_to_dict(i) for i in (result.value or [])], None


def _inst_to_dict(i: inst_mod.Instance) -> dict:
    return {
        "name": i.name,
        "container": i.container,
        "status": i.status,
        "host_port": i.host_port,
        "image": i.image,
        "started_at": i.started_at,
    }


def _dict_to_inst(d: dict) -> inst_mod.Instance:
    return inst_mod.Instance(**d)


def _render_sidebar(instances: list[inst_mod.Instance]) -> None:
    """Sidebar minimaliste : branding + navigation rapide (anchor HTML)."""
    st.sidebar.title("Fansly Manager")
    st.sidebar.caption("Pilotage central des bots")

    if _BOOT_CLEANUP_MSG:
        st.sidebar.caption(_BOOT_CLEANUP_MSG)

    st.sidebar.divider()

    if not instances:
        st.sidebar.caption("Aucun bot configure pour le moment.")
        return

    # Tri par status : running en premier, puis stopped
    running = [i for i in instances if i.is_running]
    stopped = [i for i in instances if not i.is_running]

    def _list_block(title: str, items: list[inst_mod.Instance], color_class: str) -> None:
        if not items:
            return
        st.sidebar.markdown(
            f'<div class="fm-sidebar-section">{title} '
            f'<span class="fm-sidebar-count">{len(items)}</span></div>',
            unsafe_allow_html=True,
        )
        # Liens HTML qui scroll vers la card (pas des st.button : pas de
        # rerun inutile, pas d'effet menteur — l'ancre fait reellement
        # quelque chose de visible).
        anchor_html = []
        for inst in items:
            import html
            safe_name = html.escape(inst.name)
            anchor_html.append(
                f'<a class="fm-sidebar-link fm-sidebar-link-{color_class}" '
                f'href="#fm-card-{safe_name}">{safe_name}</a>'
            )
        st.sidebar.markdown(
            "".join(anchor_html),
            unsafe_allow_html=True,
        )

    _list_block("En fonctionnement", running, "running")
    _list_block("Arretes", stopped, "stopped")

    st.sidebar.divider()
    if st.sidebar.button(
        "Ajouter un compte",
        type="primary",
        use_container_width=True,
        key="nav_wizard",
    ):
        st.session_state["view"] = "wizard"
        st.rerun()
    st.sidebar.caption(
        "Wizard guide avec login Fansly integre via noVNC."
    )


def _render_docker_error(error_msg: str) -> None:
    """Ecran d'erreur quand le daemon Docker est injoignable."""
    st.markdown(
        '<div class="fm-page-title">Connexion Docker impossible</div>',
        unsafe_allow_html=True,
    )
    st.markdown(
        '<div class="fm-page-subtitle">'
        "Le manager ne peut pas joindre le daemon Docker. "
        "Verifie que :"
        "</div>",
        unsafe_allow_html=True,
    )
    st.markdown(
        "- Docker Desktop / le daemon docker tourne sur l'hote\n"
        "- Le socket `/var/run/docker.sock` est bien monte en bind dans "
        "le container manager (cf. `docker-compose.manager.yml`)\n"
        "- Le user du container a les permissions sur le socket"
    )
    import html
    st.markdown(
        f'<div class="fm-empty-code">Erreur Docker : {html.escape(error_msg)}</div>',
        unsafe_allow_html=True,
    )
    if st.button("Retenter", key="retry_docker"):
        _cached_list_instances.clear()
        st.rerun()


def main() -> None:
    instance_dicts, error = _cached_list_instances()
    if error:
        _render_sidebar([])
        _render_docker_error(error)
        return

    instances = [_dict_to_inst(d) for d in instance_dicts]
    _render_sidebar(instances)

    view = st.session_state.get("view", "overview")
    if view == "wizard":
        wizard_view.render()
    else:
        overview_view.render(instances)


main()
