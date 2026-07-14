# src/fansly_dashboard/views/controle.py
"""Vue principale : lance le worker, enfile des jobs (publication / purge),
suit la queue."""

from __future__ import annotations

from datetime import datetime, timezone

import streamlit as st

from fansly_dashboard._lib import (
    enqueue_publish_job,
    enqueue_purge_job,
    get_state,
    list_batches,
    list_caption_batches,
    render_worker_header,
    worker_is_alive,
)


# ---------- helpers d'affichage ----------

def _short_config(job) -> str:
    c = job.config
    if job.type == "publish":
        return (
            f"lot={c.get('batch_name','?')} · "
            f"cycles={c.get('max_cycles','?') or '∞'} · "
            f"median={c.get('interval_median_minutes','?')}min"
        )
    if job.type == "purge":
        return (
            f"keywords={','.join(c.get('keywords', []))} · "
            f"age>{c.get('age_threshold_days','?')}j · "
            f"{'dry-run' if c.get('dry_run') else 'réel'}"
        )
    return str(c)[:80]


def _as_utc(dt: datetime | None) -> datetime | None:
    """Si dt est naive (données historiques pré-fix), on suppose UTC.
    Sinon on le retourne tel quel. Évite tout mismatch naive/aware."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _format_duration(job) -> str:
    started = _as_utc(job.started_at)
    finished = _as_utc(job.finished_at)
    if started and finished:
        d = (finished - started).total_seconds()
        return f"Durée : {int(d)}s"
    if started and job.status == "running":
        d = (datetime.now(timezone.utc) - started).total_seconds()
        return f"En cours depuis {int(d)}s"
    return ""


# ---------- page ----------

st.title("Contrôle")

render_worker_header()

state = get_state()
running = state.list_jobs(limit=1, statuses=["running"])
queued = state.list_jobs(limit=20, statuses=["queued"])

if running:
    j = running[0]
    st.markdown(
        f":material/play_arrow: **Job #{j.id} en cours** — {j.type} — "
        f"{_short_config(j)}"
    )
elif queued:
    st.markdown(f":material/pending: {len(queued)} job(s) en attente")
else:
    st.caption("Aucun job en cours, queue vide.")

_worker_active = worker_is_alive()
if not _worker_active:
    st.info(
        "**Worker inactif** — clique sur **Lancer** en haut à droite pour "
        "pouvoir enfiler des jobs.",
        icon=":material/info:",
    )

st.divider()


# Message d'erreur d'enfilage (consommé au premier affichage)
_enqueue_error = st.session_state.pop("enqueue_error", None)
if _enqueue_error:
    st.error(_enqueue_error, icon=":material/error:")

# Navigation par segmented_control. Streamlit interdit de modifier la clé du
# widget APRÈS son instanciation dans le même rerun. Pour forcer un onglet
# depuis un submit handler ou une autre vue, on passe par un flag intermédiaire
# (_pending_tab_switch) qu'on consomme AVANT le rendu du widget au rerun
# suivant. C'est le pattern recommandé Streamlit.
_TAB_OPTIONS = ["Publication", "Purge", "Queue"]
if "_pending_tab_switch" in st.session_state:
    st.session_state["controle_tab_widget"] = st.session_state.pop(
        "_pending_tab_switch"
    )
if "controle_tab_widget" not in st.session_state:
    st.session_state["controle_tab_widget"] = "Publication"

_active_tab = st.segmented_control(
    label="Section",
    options=_TAB_OPTIONS,
    label_visibility="collapsed",
    key="controle_tab_widget",
) or "Publication"


# ============== Publication ==============
if _active_tab == "Publication":
    st.caption("Enfile un job de publication dans la queue.")
    batches = list_batches()
    if not batches:
        st.info("Aucun lot disponible. Crée-en un dans la vue Médias.")
    else:
        # ----- Presets de rythme -----
        _PRESETS = {
            "Personnalisé": None,
            "Express test (~15s médiane)": {
                "unit": "secondes", "min": 5.0, "med": 15.0, "max": 60.0,
                "sigma": 0.5, "cycles": 1,
            },
            "Démo réaliste (~8min médiane)": {
                "unit": "minutes", "min": 2.0, "med": 8.0, "max": 30.0,
                "sigma": 0.7, "cycles": 1,
            },
            "Production - Léger (~12h médiane)": {
                "unit": "heures", "min": 4.0, "med": 12.0, "max": 36.0,
                "sigma": 0.6, "cycles": 3,
            },
            "Production - Standard (~4h médiane)": {
                "unit": "heures", "min": 1.0, "med": 4.0, "max": 12.0,
                "sigma": 0.5, "cycles": 3,
            },
            "Production - Intensif (~90min médiane)": {
                "unit": "minutes", "min": 30.0, "med": 90.0, "max": 240.0,
                "sigma": 0.5, "cycles": 3,
            },
        }
        preset_choice = st.selectbox(
            "Préset de rythme",
            options=list(_PRESETS.keys()),
            index=4,  # "Production - Standard" par défaut
            key="pub_preset",
            help=(
                "Choisis un préset pour pré-remplir les variables ci-dessous, "
                "puis ajuste si besoin. 'Personnalisé' = saisie libre."
            ),
        )
        preset = _PRESETS[preset_choice]

        # ----- Unité (override-able si preset != Personnalisé) -----
        _UNIT_OPTIONS = ["secondes", "minutes", "heures", "jours"]
        if preset is not None:
            unit_index = _UNIT_OPTIONS.index(preset["unit"])
        else:
            unit_index = 1
        unit = st.selectbox(
            "Unité des intervalles",
            options=_UNIT_OPTIONS,
            index=unit_index,
            key=f"pub_unit_{preset_choice}",  # clé conditionnelle = re-init au switch
        )
        # Defaults par unité (utilisés si Personnalisé)
        _DEFAULTS = {
            "secondes": (10.0, 30.0, 120.0, 1.0),
            "minutes":  (15.0, 90.0, 360.0, 1.0),
            "heures":   (0.5,  1.5,  6.0,   0.25),
            "jours":    (0.1,  1.0,  7.0,   0.25),
        }
        _TO_MINUTES = {
            "secondes": 1.0 / 60.0,
            "minutes":  1.0,
            "heures":   60.0,
            "jours":    60.0 * 24.0,
        }
        if preset is not None:
            min_def, med_def, max_def = preset["min"], preset["med"], preset["max"]
            sigma_def = preset["sigma"]
            cycles_def = preset["cycles"]
            step = _DEFAULTS[unit][3]
        else:
            min_def, med_def, max_def, step = _DEFAULTS[unit]
            sigma_def = 0.5
            cycles_def = 1

        # Clé commune qui change quand le preset change (pour re-init les widgets)
        _preset_key = f"{preset_choice}_{unit}"

        # Lots de légendes disponibles (lots JSON)
        caption_batches = list_caption_batches()
        _NONE_CAPTION_LABEL = "(Aucun — utiliser les .txt à la racine)"
        caption_options = [_NONE_CAPTION_LABEL] + [
            f"{b['name']} ({b['size']} légendes)" for b in caption_batches
        ]
        caption_map = {
            f"{b['name']} ({b['size']} légendes)": b['name']
            for b in caption_batches
        }

        # Pré-sélection si l'utilisateur arrive depuis la vue Médias
        # (consommée une seule fois : pop dans session_state)
        _preselected_batch = st.session_state.pop("preselected_batch", None)
        _preselected_cap_batch = st.session_state.pop("preselected_caption_batch", None)
        if _preselected_batch or _preselected_cap_batch:
            msgs = []
            if _preselected_batch:
                msgs.append(f"lot médias **{_preselected_batch}**")
            if _preselected_cap_batch:
                msgs.append(f"lot légendes **{_preselected_cap_batch}**")
            st.info(
                "Pré-sélection depuis la vue Médias : " + " · ".join(msgs),
                icon=":material/info:",
            )

        with st.form("enqueue_publish", border=False):
            batch_options = [f"{name} ({n} médias)" for name, n in batches]
            batch_map = {f"{name} ({n} médias)": name for name, n in batches}

            # Sélecteur lot médias — pré-sélectionné si redirection depuis Médias
            default_batch_idx = 0
            if _preselected_batch:
                for i, opt in enumerate(batch_options):
                    if batch_map.get(opt) == _preselected_batch:
                        default_batch_idx = i
                        break
            sel = st.selectbox(
                "Lot à publier",
                options=batch_options,
                index=default_batch_idx,
            )

            # Sélecteur lot de légendes — pré-sélectionné si redirection,
            # sinon "default" s'il existe
            target_caption = _preselected_cap_batch or "default"
            default_caption_idx = 0
            for i, opt in enumerate(caption_options):
                if caption_map.get(opt) == target_caption:
                    default_caption_idx = i
                    break
            sel_captions = st.selectbox(
                "Lot de légendes",
                options=caption_options,
                index=default_caption_idx,
                help="Le bot piochera au hasard une légende du lot à chaque publication.",
            )

            c1, c2 = st.columns(2)
            with c1:
                max_cycles = st.number_input(
                    "Cycles max", min_value=0, value=cycles_def,
                    help="0 = infini",
                    key=f"pub_cycles_{_preset_key}",
                )
                interval_min = st.number_input(
                    f"Intervalle min ({unit})",
                    min_value=0.001, value=min_def, step=step, format="%.3f",
                    key=f"pub_min_{_preset_key}",
                )
            with c2:
                interval_median = st.number_input(
                    f"Intervalle médian ({unit})",
                    min_value=0.001, value=med_def, step=step, format="%.3f",
                    key=f"pub_med_{_preset_key}",
                )
                interval_max = st.number_input(
                    f"Intervalle max ({unit})",
                    min_value=0.001, value=max_def, step=step, format="%.3f",
                    key=f"pub_max_{_preset_key}",
                )
            interval_sigma = st.slider(
                "Variabilité (sigma)", min_value=0.1, max_value=1.5,
                value=sigma_def, step=0.05,
                help="Plus élevé = plus de variation entre publications.",
                key=f"pub_sigma_{_preset_key}",
            )

            # Rotation per-media : comportement systématique, plus de toggle.
            # Avant chaque publication d'un media, sa version précédente
            # (du cycle d'avant) est supprimée via son permalien Fansly.
            st.caption(
                ":material/sync: **Rotation per-media** active : à chaque "
                "republication d'un media, son ancienne version est supprimée "
                "avant. Ton profil garde toujours les versions les plus "
                "récentes (signature `#fyp` requise comme garde-fou)."
            )
            submitted_pub = st.form_submit_button(
                "Enfiler dans la queue",
                icon=":material/playlist_add:",
                use_container_width=True,
                type="primary",
                disabled=not _worker_active,
            )
            if not _worker_active:
                st.caption(
                    ":material/lock: Démarre d'abord le worker (bouton **Lancer** en haut)."
                )
            if submitted_pub:
                if interval_min > interval_median or interval_median > interval_max:
                    st.session_state["enqueue_error"] = "Min <= Médian <= Max requis."
                    st.rerun()
                else:
                    factor = _TO_MINUTES[unit]
                    captions_batch_name = (
                        caption_map.get(sel_captions)
                        if sel_captions and sel_captions != _NONE_CAPTION_LABEL
                        else None
                    )
                    try:
                        job_id = enqueue_publish_job(
                            batch_name=batch_map[sel],
                            max_cycles=int(max_cycles),
                            interval_median_minutes=interval_median * factor,
                            interval_min=interval_min * factor,
                            interval_max=interval_max * factor,
                            interval_sigma=interval_sigma,
                            captions_batch_name=captions_batch_name,
                        )
                    except Exception as e:  # noqa: BLE001
                        st.session_state["enqueue_error"] = (
                            f"Impossible d'enfiler le job : {e}"
                        )
                        st.rerun()
                    else:
                        st.toast(
                            f"Job #{job_id} enfilé (lot={batch_map[sel]})",
                            icon=":material/check_circle:",
                        )
                        # Flag intermédiaire : appliqué AVANT le widget au rerun suivant
                        st.session_state["_pending_tab_switch"] = "Queue"
                        st.rerun()


# ============== Purge ==============
elif _active_tab == "Purge":
    st.caption(
        "Enfile un job de purge. Un post est supprimé SI il dépasse le seuil d'âge "
        "ET sa légende contient un mot-clé."
    )

    # Mode de filtre temporel — hors du form pour réactiver l'UI en live
    time_mode = st.radio(
        "Filtre temporel",
        options=["Plus vieux que (âge minimum)", "Entre deux dates"],
        index=0,
        horizontal=True,
        key="purge_time_mode",
    )
    age_unit_default = "jours"
    if time_mode == "Plus vieux que (âge minimum)":
        age_unit = st.selectbox(
            "Unité du seuil d'âge",
            options=["jours", "semaines", "mois"],
            index=0,
            key="purge_age_unit",
        )
        _AGE_DEFAULTS = {"jours": 30, "semaines": 4, "mois": 1}
    _AGE_TO_DAYS = {"jours": 1, "semaines": 7, "mois": 30}

    with st.form("enqueue_purge", border=False):
        kws_label = (
            "Mots-clés (optionnel en mode 'Entre deux dates')"
            if time_mode == "Entre deux dates"
            else "Mots-clés"
        )
        kws_selected = st.multiselect(
            kws_label,
            options=["promo", "ancien", "archive", "test"],
            default=[],
            accept_new_options=True,
            placeholder="Tape un mot puis Entrée pour l'ajouter",
            help=(
                "Tape un mot-clé puis Entrée → il s'ajoute comme un badge. "
                "Ajoute autant que tu veux. "
                "En mode 'Entre deux dates', tu peux laisser vide pour "
                "supprimer **tous** les posts de la fenêtre temporelle."
            ),
        )

        # Champs temporels selon le mode
        age_days_value = 0
        start_dt = None
        end_dt = None

        if time_mode == "Plus vieux que (âge minimum)":
            ca, cb = st.columns(2)
            with ca:
                age = st.number_input(
                    f"Seuil d'âge ({age_unit})",
                    min_value=0,
                    value=_AGE_DEFAULTS[age_unit],
                )
            with cb:
                st.caption(
                    f"Un post doit être strictement plus vieux que ce seuil "
                    f"({_AGE_TO_DAYS[age_unit]} jours par {age_unit[:-1] if age_unit.endswith('s') else age_unit})."
                )
            age_days_value = int(age) * _AGE_TO_DAYS[age_unit]
        else:
            from datetime import date as _date, datetime as _dt, time as _time, timezone as _tz
            today = _dt.now(_tz.utc).date()
            ca, cb = st.columns(2)
            with ca:
                d_start = st.date_input(
                    "Date de début (UTC)",
                    value=today.replace(day=1),
                    help="Posts publiés à partir de ce jour (00:00 UTC)",
                )
            with cb:
                d_end = st.date_input(
                    "Date de fin (UTC)",
                    value=today,
                    help="Posts publiés jusqu'à ce jour (23:59 UTC)",
                )
            if d_start > d_end:
                st.error("La date de début doit être <= date de fin.")
            start_dt = _dt.combine(d_start, _time.min, tzinfo=_tz.utc)
            end_dt = _dt.combine(d_end, _time.max, tzinfo=_tz.utc)

        c1, c2 = st.columns(2)
        with c1:
            max_del = st.number_input(
                "Suppressions max", min_value=1, max_value=200, value=20,
            )
            match_mode = st.selectbox(
                "Mode de match", options=["any", "all"], index=0,
                help="any = un seul suffit. all = tous requis.",
            )
        with c2:
            scroll_cap = st.number_input(
                "Posts max à examiner", min_value=10, max_value=2000, value=500,
            )

        dry_run = st.toggle(
            "Mode test (n'effectue aucune suppression réelle)",
            value=True,
        )
        submitted_purge = st.form_submit_button(
            "Enfiler dans la queue",
            icon=":material/playlist_add:",
            use_container_width=True,
            type="primary",
            disabled=not _worker_active,
        )
        if not _worker_active:
            st.caption(
                ":material/lock: Démarre d'abord le worker (bouton **Lancer** en haut)."
            )
        # Affichage en live (hors submit) d'un warning quand keywords vide
        # ET mode "Entre deux dates" : explicite le risque avant action.
        _kws_preview = [
            k.strip() for k in (kws_selected or []) if k and k.strip()
        ]
        if time_mode == "Entre deux dates" and not _kws_preview:
            st.warning(
                ":material/warning: **Aucun mot-clé** : tous les posts compris "
                "dans la fenêtre temporelle seront candidats, y compris les "
                "posts publiés manuellement.",
            )

        if submitted_purge:
            kws = [k.strip() for k in (kws_selected or []) if k and k.strip()]
            # Mode "Plus vieux que" → mot-clé obligatoire (sécurité stricte).
            # Mode "Entre deux dates" → mot-clé optionnel (purge temporelle pure).
            if not kws and time_mode != "Entre deux dates":
                st.session_state["enqueue_error"] = (
                    "Au moins un mot-clé requis (sauf en mode 'Entre deux dates')."
                )
                st.rerun()
            elif time_mode == "Entre deux dates" and start_dt and end_dt and start_dt > end_dt:
                st.session_state["enqueue_error"] = (
                    "La date de début doit être <= date de fin."
                )
                st.rerun()
            else:
                kwargs = dict(
                    keywords=kws,
                    keyword_match_mode=match_mode,
                    dry_run=bool(dry_run),
                    max_deletions=int(max_del),
                    scroll_cap=int(scroll_cap),
                )
                if time_mode == "Entre deux dates" and start_dt and end_dt:
                    kwargs["start_date_iso"] = start_dt.isoformat()
                    kwargs["end_date_iso"] = end_dt.isoformat()
                    desc = f"fenêtre {start_dt.date()} → {end_dt.date()}"
                else:
                    kwargs["age_threshold_days"] = age_days_value
                    desc = f"âge>{age_days_value}j"
                try:
                    job_id = enqueue_purge_job(**kwargs)
                except Exception as e:  # noqa: BLE001
                    st.session_state["enqueue_error"] = (
                        f"Impossible d'enfiler le job : {e}"
                    )
                    st.rerun()
                else:
                    st.toast(
                        f"Job #{job_id} enfilé (purge {'test' if dry_run else 'réelle'}, {desc})",
                        icon=":material/check_circle:",
                    )
                    # Flag intermédiaire : appliqué AVANT le widget au rerun suivant
                    st.session_state["_pending_tab_switch"] = "Queue"
                    st.rerun()


# ============== Queue ==============
elif _active_tab == "Queue":

    # run_every=30s (et non 5s) : chaque tick declenche un rerun serveur
    # complet du fragment qui alloue une nuee de petits objets protobuf/Delta
    # non rendus a l'OS par glibc. A 5s = ~17k rerun/jour/onglet ouvert = moteur
    # principal du ratchet memoire qui menait a l'OOM. 30s divise le churn par 6.
    # Le bouton "Rafraichir" ci-dessous permet un refresh manuel immediat.
    @st.fragment(run_every="30s")
    def _queue_panel() -> None:
        state_local = get_state()
        c_refresh, _ = st.columns([1, 5])
        with c_refresh:
            if st.button(
                "Rafraîchir",
                icon=":material/refresh:",
                key="queue_refresh_btn",
                use_container_width=True,
            ):
                st.rerun()

        # Message "log sélectionné" affiché une seule fois après clic
        flash = st.session_state.pop("_flash_log_selected", None)
        if flash:
            st.info(
                f"Log sélectionné : `{flash}` — va dans l'onglet Logs pour le consulter.",
                icon=":material/description:",
            )

        jobs = state_local.list_jobs(limit=40)

        # Détection : nb de jobs actifs (running ou queued ou cancelling)
        active_count = sum(1 for j in jobs if j.status in ("running", "queued", "cancelling"))
        if active_count > 0:
            st.caption(
                f":material/sync: Auto-rafraîchissement actif (toutes les 5s) — "
                f"{active_count} job(s) en cours/en attente."
            )

        # Mémoire des statuts pour détecter une transition vers 'done/failed/cancelled'
        prev_statuses = st.session_state.get("_queue_prev_statuses", {})
        cur_statuses = {j.id: j.status for j in jobs}
        terminal = {"done", "failed", "cancelled"}
        for jid, status in cur_statuses.items():
            prev = prev_statuses.get(jid)
            if prev and prev not in terminal and status in terminal:
                st.toast(
                    f"Job #{jid} terminé ({status})",
                    icon=":material/check_circle:" if status == "done" else ":material/error:",
                )
        st.session_state["_queue_prev_statuses"] = cur_statuses

        if not jobs:
            st.caption("Aucun job enregistré.")
            return

        for j in jobs:
            icon = {
                "queued":     ":material/pending:",
                "running":    ":material/play_arrow:",
                "cancelling": ":material/cancel:",
                "done":       ":material/check_circle:",
                "failed":     ":material/error:",
                "cancelled":  ":material/block:",
            }.get(j.status, ":material/circle:")

            cols = st.columns([4, 2, 2])
            with cols[0]:
                st.markdown(
                    f"{icon} **#{j.id}** — {j.type} — "
                    f"`{j.status}` — {_short_config(j)}"
                )
                duration = _format_duration(j)
                if duration:
                    st.caption(duration)
                if j.error:
                    st.caption(f"Erreur : {j.error[:200]}")
            with cols[1]:
                if j.log_path and st.button(
                    "Voir log",
                    key=f"log_{j.id}",
                    icon=":material/description:",
                    use_container_width=True,
                ):
                    st.session_state["selected_job_log"] = j.log_path
                    st.session_state["_flash_log_selected"] = j.log_path
                    st.rerun()
            with cols[2]:
                if j.status in ("queued", "running"):
                    if st.button(
                        "Annuler",
                        key=f"cancel_{j.id}",
                        icon=":material/cancel:",
                        use_container_width=True,
                    ):
                        state_local.request_job_cancel(j.id)
                        st.toast(f"Job #{j.id} → annulation demandée", icon=":material/cancel:")
                        st.rerun()

    _queue_panel()
