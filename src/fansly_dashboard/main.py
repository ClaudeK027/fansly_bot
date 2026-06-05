# src/fansly_dashboard/main.py
"""Point d'entree Streamlit. Lance via :
    streamlit run src/fansly_dashboard/main.py
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

st.set_page_config(
    page_title="Fansly Bot",
    page_icon=":material/smart_toy:",
    layout="centered",
)

_views_dir = Path(__file__).parent / "views"

pg = st.navigation(
    [
        st.Page(
            str(_views_dir / "controle.py"),
            title="Contrôle",
            icon=":material/dashboard:",
            default=True,
        ),
        st.Page(
            str(_views_dir / "medias.py"),
            title="Médias",
            icon=":material/folder:",
        ),
        st.Page(
            str(_views_dir / "logs.py"),
            title="Logs",
            icon=":material/description:",
        ),
    ]
)
pg.run()
