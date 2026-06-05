# src/fansly_dashboard/views/logs.py
"""Vue Logs : logs globaux du worker ou logs d'un job specifique."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import streamlit as st

from fansly_dashboard._lib import (
    get_settings,
    get_state,
    read_last_log_lines,
    render_worker_header,
)

st.title("Logs")

render_worker_header()


# ---------- helpers ----------

_LEVEL_ICON = {
    "debug":    ":material/code:",
    "info":     ":material/info:",
    "warning":  ":material/warning:",
    "error":    ":material/error:",
    "critical": ":material/dangerous:",
}

_HIDDEN_EVENTS = {
    "auth_check_starting", "scheduler_started", "media_watcher_started",
    "logging_initialized", "stealth_applied", "session_started",
    "session_starting", "session_context_closed", "playwright_stopped",
    "state_store_ready", "boot", "purger_loop_iter",
    "purger_post_not_detached_after_delete", "purger_progress",
    "purger_navigating", "uploader_submit_button_active",
    "uploader_composer_closed", "uploader_post_submitted",
    "media_permissions_modal_detected", "overlay_dismissed",
    "media_detected_moved", "media_detected",
}


def _read_log_file(path: Path, n: int = 200) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        with path.open("rb") as f:
            f.seek(0, io.SEEK_END)
            size = f.tell()
            chunk = min(size, 500_000)
            f.seek(size - chunk)
            raw = f.read().decode("utf-8", errors="ignore")
    except OSError:
        return []
    lines = raw.strip().split("\n")[-n:]
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            out.append({"event": "raw", "raw": line})
    return out


# ---------- choix de la source ----------

settings = get_settings()
state = get_state()

source_options = ["Worker (global)"]
job_paths: dict[str, str] = {}

# Liste les jobs recents qui ont un log_path
for j in state.list_jobs(limit=20, statuses=["running", "done", "failed", "cancelled"]):
    if j.log_path:
        label = f"Job n°{j.id} — {j.type} — {j.status}"
        source_options.append(label)
        job_paths[label] = j.log_path

# Pre-selection s'il y en a une depuis la vue Controle
preselect = st.session_state.get("selected_job_log")
default_index = 0
if preselect:
    for i, label in enumerate(source_options):
        if job_paths.get(label) == preselect:
            default_index = i
            break

source = st.selectbox("Source des logs", options=source_options, index=default_index)


# ---------- filtres ----------

c1, c2, c3 = st.columns([2, 3, 1])
with c1:
    n_lines = st.selectbox("Lignes", options=[20, 50, 100, 200], index=1)
with c2:
    levels = st.multiselect(
        "Niveaux",
        options=["info", "warning", "error", "critical"],
        default=["info", "warning", "error", "critical"],
    )
with c3:
    if st.button("Rafraîchir", icon=":material/refresh:", use_container_width=True):
        st.rerun()

hide_noise = st.toggle("Masquer les events techniques", value=True)

st.divider()


# ---------- lecture ----------

if source == "Worker (global)":
    lines = read_last_log_lines(n=n_lines * 4)
else:
    path = Path(job_paths[source])
    lines = _read_log_file(path, n=n_lines * 4)

filtered = [
    l for l in lines
    if (l.get("level", "info") or "info").lower() in levels
    and (not hide_noise or l.get("event") not in _HIDDEN_EVENTS)
]
filtered = filtered[-n_lines:]


# ---------- affichage ----------

if not filtered:
    st.caption("Aucune ligne à afficher.")
else:
    for entry in filtered:
        lvl = (entry.get("level") or "info").lower()
        ts = entry.get("timestamp", "")
        ev = entry.get("event", "?")
        icon = _LEVEL_ICON.get(lvl, ":material/circle:")
        extras = {
            k: v for k, v in entry.items()
            if k not in ("timestamp", "event", "level", "exc_info")
        }
        suffix = ""
        if extras:
            kv = "  ".join(f"`{k}={v}`" for k, v in list(extras.items())[:4])
            suffix = f"  {kv}"
        st.markdown(f"{icon} `{ts[11:19]}` **{ev}**{suffix}")
