# src/fansly_dashboard/views/medias.py
"""Vue de gestion des médias : lots et légendes."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

# Chemin vers la vue Contrôle, utilisé pour les redirections via st.switch_page
_CONTROLE_PAGE_PATH = str((Path(__file__).parent / "controle.py").resolve())

from fansly_bot.infra.names import InvalidBatchNameError, validate_batch_name

from fansly_dashboard._lib import (
    confirm_destructive_action,
    create_batch,
    delete_batch,
    delete_caption,
    delete_caption_batch,
    get_state,
    list_batches,
    list_caption_batches,
    list_captions,
    list_media_in_batch,
    parse_captions_text,
    read_caption,
    read_caption_batch,
    render_worker_header,
    save_uploaded_file,
    serialize_captions_text,
    worker_is_alive,
    write_caption,
    write_caption_batch,
)

st.title("Médias")

render_worker_header()

state = get_state()
active = state.get_active_batch()


tab_lots, tab_caps = st.tabs(["Lots", "Légendes"])


# ============== Onglet Lots ==============
with tab_lots:
    # Lot actif (bandeau) — distingue 3 états :
    #  1. Cycle en cours piloté par un job publish actif → boutons annulation
    #  2. Cycle en cours sans job actif (worker tourne mais entre 2 publishes)
    #     ou sans worker (kill brutal) → "progression résiduelle"
    #  3. Pas de lot actif → message neutre
    if active:
        running_publish_jobs = [
            j for j in state.list_jobs(limit=10, statuses=["running"])
            if j.type == "publish" and j.config.get("batch_name") == active.name
        ]
        queued_publish_jobs = [
            j for j in state.list_jobs(limit=10, statuses=["queued"])
            if j.type == "publish" and j.config.get("batch_name") == active.name
        ]
        has_running_job = bool(running_publish_jobs)
        has_pending_job = bool(running_publish_jobs or queued_publish_jobs)
        # Résiduel = active_batch présent mais aucun job publish actif. Au prochain
        # démarrage du worker, cleanup_orphan_active_batch s'en occupera, mais
        # tant que l'utilisateur ne relance pas le worker, on l'aide à nettoyer
        # manuellement.
        is_residual = not has_pending_job
        worker_alive = worker_is_alive()

        c1, c2 = st.columns([3, 2])
        with c1:
            if is_residual:
                st.markdown(
                    f":material/warning: **{active.name}** — progression résiduelle "
                    f"d'un cycle interrompu (cycle {active.current_cycle}/"
                    f"{active.max_cycles or '∞'}, "
                    f"{len(active.published_in_cycle)} publiés / "
                    f"{active.total_published} total)"
                )
                if not worker_alive:
                    st.caption(
                        ":material/info: Aucun worker actif. Au prochain démarrage "
                        "du worker, cette progression sera nettoyée automatiquement. "
                        "Tu peux aussi l'effacer manuellement ci-contre."
                    )
                else:
                    st.caption(
                        ":material/info: Le worker tourne mais aucun job publish ne "
                        "pilote ce lot. Effacement recommandé."
                    )
            else:
                st.markdown(
                    f":material/play_circle: **{active.name}** — "
                    f"cycle {active.current_cycle}/{active.max_cycles or '∞'} — "
                    f"{len(active.published_in_cycle)} publiés / {active.total_published} total"
                )
                if has_running_job:
                    jobs_ids = ", ".join(f"#{j.id}" for j in running_publish_jobs)
                    st.caption(
                        f":material/sync: Job(s) publish en cours sur ce lot : {jobs_ids}"
                    )
        with c2:
            if has_running_job:
                # Cas 1 : un job publish tourne -> on annule le job (propre)
                confirmed = confirm_destructive_action(
                    button_label="Annuler le job en cours",
                    confirm_message=(
                        "Le job publish actif sera annulé. Le worker finit sa "
                        "tâche courante (publication ou suppression en cours) "
                        "puis s'arrêtera proprement."
                    ),
                    state_key=f"cancel_publish_job_{active.name}",
                    button_help=(
                        "Annulation gracieuse via le pipeline du worker. "
                        "Utilise 'Forcer' dans le bandeau du worker si tu veux "
                        "interrompre immédiatement."
                    ),
                    icon=":material/cancel:",
                )
                if confirmed:
                    for j in running_publish_jobs:
                        state.request_job_cancel(j.id)
                    st.toast(
                        "Annulation demandée — le worker terminera sa tâche "
                        "courante puis s'arrêtera.",
                        icon=":material/check_circle:",
                    )
                    st.rerun()
            elif is_residual:
                # Cas 3 : active_batch résiduel d'un kill brutal précédent
                confirmed = confirm_destructive_action(
                    button_label="Effacer la progression",
                    confirm_message=(
                        f"Effacer la progression résiduelle du lot "
                        f"**{active.name}** ? Aucun job ne pilote ce lot — "
                        f"l'état est orphelin et sera nettoyé au prochain "
                        f"démarrage du worker de toute façon."
                    ),
                    state_key=f"clear_residual_{active.name}",
                    icon=":material/delete_sweep:",
                )
                if confirmed:
                    state.stop_batch()
                    st.toast(
                        f"Progression résiduelle de '{active.name}' effacée.",
                        icon=":material/check_circle:",
                    )
                    st.rerun()
            else:
                # Cas 2 : un job queued mais pas encore running, ou autre état
                # transitoire -> on peut stop_batch directement avec confirmation.
                confirmed = confirm_destructive_action(
                    button_label="Arrêter ce cycle",
                    confirm_message=(
                        f"Arrêter le cycle en cours du lot **{active.name}** ? "
                        f"L'état de progression "
                        f"(cycle {active.current_cycle}, "
                        f"{len(active.published_in_cycle)} publiés dans ce cycle) "
                        f"sera perdu."
                    ),
                    state_key=f"stop_batch_{active.name}",
                    icon=":material/stop:",
                )
                if confirmed:
                    state.stop_batch()
                    st.toast(
                        f"Cycle du lot '{active.name}' arrêté.",
                        icon=":material/stop:",
                    )
                    st.rerun()
    else:
        st.caption("Aucun lot actif.")

    st.divider()

    # Création + upload en une seule étape
    with st.expander("Créer un nouveau lot", icon=":material/add:"):
        with st.form("create_batch", clear_on_submit=True, border=False):
            new_name = st.text_input(
                "Nom du lot",
                placeholder="ete_2026",
                help="Lettres, chiffres, '_' et '-' uniquement.",
            )
            new_medias = st.file_uploader(
                "Médias à importer dans ce lot (optionnel)",
                accept_multiple_files=True,
                help="Tu peux laisser vide et uploader plus tard depuis la liste.",
            )
            if st.form_submit_button("Créer le lot", icon=":material/folder_managed:"):
                new_name = (new_name or "").strip()
                # Validation centralisée : remplace la vérification inline
                # alors qu'elle peut déjà avoir été contournée ailleurs.
                try:
                    new_name = validate_batch_name(new_name)
                    valid = True
                except InvalidBatchNameError as e:
                    st.error(str(e))
                    valid = False
                if valid:
                    create_batch(new_name)
                    n_uploaded = 0
                    for f in (new_medias or []):
                        save_uploaded_file(new_name, f.name, f.getbuffer())
                        n_uploaded += 1
                    if n_uploaded:
                        st.toast(
                            f"Lot '{new_name}' créé avec {n_uploaded} média(s).",
                            icon=":material/check_circle:",
                        )
                    else:
                        st.toast(
                            f"Lot '{new_name}' créé (vide).",
                            icon=":material/check_circle:",
                        )
                    st.rerun()

    # Liste des lots
    batches = list_batches()
    if not batches:
        st.info("Aucun lot pour l'instant.", icon=":material/info:")
    else:
        for name, n in batches:
            is_active = active is not None and active.name == name
            label_icon = ":material/play_circle:" if is_active else ":material/folder:"
            with st.expander(f"{name} — {n} médias", icon=label_icon):
                medias = list_media_in_batch(name)
                if medias:
                    # Avertissement si on est en train d'éditer un lot actif :
                    # supprimer/ajouter un média peut faire trébucher le worker
                    # qui pourrait sélectionner ce fichier au prochain upload.
                    if is_active:
                        st.caption(
                            ":material/info: Ce lot est actif. Les modifications "
                            "(suppression/ajout de médias) peuvent perturber le "
                            "cycle en cours."
                        )
                    for m in medias:
                        ml, mr = st.columns([6, 2])
                        ml.text(f"{m.name}  ({m.stat().st_size // 1024} Ko)")
                        with mr:
                            # Confirmation 2-clics même pour un média individuel
                            # (incohérence relevée par l'audit : tous les autres
                            # boutons destructifs demandent confirmation).
                            if confirm_destructive_action(
                                button_label="Supprimer",
                                confirm_message=(
                                    f"Supprimer **{m.name}** du lot **{name}** ?"
                                ),
                                state_key=f"del_media_{name}_{m.name}",
                                icon=":material/delete:",
                            ):
                                m.unlink()
                                st.toast(
                                    f"{m.name} supprimé.",
                                    icon=":material/delete:",
                                )
                                st.rerun()
                else:
                    st.caption("(aucun média)")

                # Upload — englobé dans un form clear_on_submit pour éviter
                # le replay du file_uploader à chaque rerun.
                with st.form(
                    f"upload_form_{name}",
                    clear_on_submit=True,
                    border=False,
                ):
                    new_files = st.file_uploader(
                        "Ajouter des médias",
                        accept_multiple_files=True,
                        key=f"upload_widget_{name}",
                    )
                    if st.form_submit_button(
                        "Ajouter au lot",
                        icon=":material/add:",
                        type="primary",
                    ):
                        if not new_files:
                            st.warning(
                                "Aucun fichier sélectionné.",
                                icon=":material/warning:",
                            )
                        else:
                            for f in new_files:
                                save_uploaded_file(name, f.name, f.getbuffer())
                            st.toast(
                                f"{len(new_files)} fichier(s) ajouté(s) au lot.",
                                icon=":material/check_circle:",
                            )
                            st.rerun()

                # Actions lot
                if not is_active:
                    st.divider()
                    c1, c2 = st.columns([3, 2])
                    with c1:
                        if st.button(
                            "Configurer une publication avec ce lot",
                            key=f"start_{name}",
                            icon=":material/play_arrow:",
                            disabled=n == 0,
                            use_container_width=True,
                            type="primary",
                            help=(
                                "Redirige vers Contrôle > Publication avec ce "
                                "lot présélectionné."
                            ),
                        ):
                            st.session_state["preselected_batch"] = name
                            st.session_state["controle_tab_widget"] = "Publication"
                            st.switch_page(_CONTROLE_PAGE_PATH)
                    with c2:
                        if confirm_destructive_action(
                            button_label="Supprimer le lot",
                            confirm_message=(
                                f"Confirmer la suppression du lot **{name}** "
                                f"et de ses {n} média(s) ?"
                            ),
                            state_key=f"del_batch_{name}",
                            icon=":material/delete_forever:",
                        ):
                            delete_batch(name)
                            st.toast(
                                f"Lot '{name}' supprimé",
                                icon=":material/delete:",
                            )
                            st.rerun()


# ============== Onglet Légendes (lots JSON) ==============
with tab_caps:
    st.caption(
        "Un **lot de légendes** = un fichier qui contient plusieurs textes. Lors "
        "d'une publication, le bot pioche au hasard une légende du lot choisi."
    )

    def _sync_caps_state(state_key: str) -> list[str]:
        """Synchronise la liste session_state[state_key] avec les widgets text_area."""
        result = []
        for j in range(len(st.session_state[state_key])):
            result.append(st.session_state.get(f"{state_key}_cap_{j}", ""))
        st.session_state[state_key] = result
        return result

    def _cleanup_caps_state(state_key: str) -> None:
        """Supprime toutes les clés de widgets associées au state d'un lot."""
        keys_to_del = [k for k in list(st.session_state.keys())
                       if k.startswith(f"{state_key}_cap_")]
        for k in keys_to_del:
            del st.session_state[k]
        if state_key in st.session_state:
            del st.session_state[state_key]

    batches = list_caption_batches()

    # ----- Création d'un nouveau lot -----
    NEW_STATE = "caps_new_state"
    with st.expander("Créer un nouveau lot de légendes", icon=":material/add:"):
        if NEW_STATE not in st.session_state:
            st.session_state[NEW_STATE] = [""]  # une légende vide par défaut

        new_name = st.text_input(
            "Nom du lot",
            placeholder="ete_2026",
            help="Lettres, chiffres, '_' et '-' uniquement.",
            key="new_batch_name",
        )
        new_description = st.text_input(
            "Description (optionnelle)",
            placeholder="Lot d'été teasing soft",
            key="new_batch_desc",
        )

        st.markdown("**Légendes du lot :**")
        for i in range(len(st.session_state[NEW_STATE])):
            cap_cols = st.columns([8, 1])
            cap_key = f"{NEW_STATE}_cap_{i}"
            # Init via session_state (pattern cohérent avec l'édition)
            if cap_key not in st.session_state:
                st.session_state[cap_key] = st.session_state[NEW_STATE][i]
            with cap_cols[0]:
                st.text_area(
                    f"Légende {i + 1}",
                    height=110,
                    key=cap_key,
                    label_visibility="collapsed",
                    placeholder=f"Légende {i + 1} avec hashtags inclus",
                )
            with cap_cols[1]:
                if len(st.session_state[NEW_STATE]) > 1:
                    if st.button(
                        "",
                        key=f"{NEW_STATE}_del_{i}",
                        icon=":material/delete:",
                        use_container_width=True,
                        help="Supprimer cette légende",
                    ):
                        current = _sync_caps_state(NEW_STATE)
                        current.pop(i)
                        _cleanup_caps_state(NEW_STATE)
                        st.session_state[NEW_STATE] = current
                        st.rerun()

        ca, cb = st.columns([1, 1])
        if ca.button(
            "Ajouter une légende",
            icon=":material/add:",
            use_container_width=True,
            key=f"{NEW_STATE}_add_btn",
        ):
            current = _sync_caps_state(NEW_STATE)
            current.append("")
            _cleanup_caps_state(NEW_STATE)
            st.session_state[NEW_STATE] = current
            st.rerun()

        if cb.button(
            "Créer le lot",
            icon=":material/folder_managed:",
            type="primary",
            use_container_width=True,
            key=f"{NEW_STATE}_save_btn",
        ):
            captions = _sync_caps_state(NEW_STATE)
            captions = [c for c in captions if c and c.strip()]
            name_raw = (new_name or "").strip()
            try:
                name = validate_batch_name(name_raw)
                name_ok = True
            except InvalidBatchNameError as e:
                st.error(str(e))
                name_ok = False
                name = ""
            if name_ok and not captions:
                st.error("Au moins une légende non vide est requise.")
            elif name_ok and captions:
                write_caption_batch(name, captions, description=new_description.strip())
                _cleanup_caps_state(NEW_STATE)
                # Reset les champs nom/desc
                if "new_batch_name" in st.session_state:
                    del st.session_state["new_batch_name"]
                if "new_batch_desc" in st.session_state:
                    del st.session_state["new_batch_desc"]
                st.toast(
                    f"Lot '{name}' créé ({len(captions)} légende(s)).",
                    icon=":material/check_circle:",
                )
                st.rerun()

    st.divider()

    # ----- Liste des lots existants -----
    if not batches:
        st.info("Aucun lot de légendes. Crée-en un ci-dessus.", icon=":material/info:")
    else:
        for b in batches:
            batch_name = b["name"]
            ESK = f"caps_state_{batch_name}"

            with st.expander(
                f"{batch_name} — {b['size']} légende(s)" +
                (f" · {b['description']}" if b['description'] else ""),
                icon=":material/article:",
            ):
                full = read_caption_batch(batch_name)
                if full is None:
                    st.error("Erreur de lecture du fichier JSON.")
                    continue

                # Init session_state depuis fichier (première ouverture du lot)
                if ESK not in st.session_state:
                    st.session_state[ESK] = list(full.get("captions", []))

                desc = st.text_input(
                    "Description",
                    value=full.get("description", ""),
                    key=f"desc_{batch_name}",
                )

                # ----- Liste des légendes (chacune dans son container) -----
                st.markdown(f"**{len(st.session_state[ESK])} légende(s) dans ce lot :**")
                for i in range(len(st.session_state[ESK])):
                    cap_key = f"{ESK}_cap_{i}"
                    if cap_key not in st.session_state:
                        st.session_state[cap_key] = st.session_state[ESK][i]

                    with st.container(border=True):
                        cap_cols = st.columns([7, 1])
                        with cap_cols[0]:
                            st.text_area(
                                f"Légende #{i + 1}",
                                height=100,
                                key=cap_key,
                                label_visibility="visible",
                                placeholder="Texte de la légende avec hashtags inclus...",
                            )
                        with cap_cols[1]:
                            st.write("")  # spacer pour aligner verticalement
                            if len(st.session_state[ESK]) > 1:
                                if st.button(
                                    "",
                                    key=f"{ESK}_del_{i}",
                                    icon=":material/delete:",
                                    use_container_width=True,
                                    help="Supprimer cette légende (auto-save)",
                                ):
                                    # Auto-save : on lit les éditions courantes,
                                    # on supprime l'index, on écrit sur disque
                                    current = _sync_caps_state(ESK)
                                    current.pop(i)
                                    current = [c for c in current if c and c.strip()]
                                    write_caption_batch(
                                        batch_name, current, description=desc.strip()
                                    )
                                    _cleanup_caps_state(ESK)
                                    st.toast(
                                        "Légende supprimée.",
                                        icon=":material/delete:",
                                    )
                                    st.rerun()

                st.divider()

                # ----- Ajouter une légende -----
                add_key = f"{ESK}_add_text"
                if add_key not in st.session_state:
                    st.session_state[add_key] = ""

                with st.expander("Ajouter une légende", icon=":material/add:"):
                    st.text_area(
                        "Nouvelle légende",
                        height=100,
                        key=add_key,
                        placeholder="Tape la nouvelle légende avec hashtags...",
                        label_visibility="collapsed",
                    )
                    if st.button(
                        "Ajouter au lot",
                        key=f"{ESK}_add_save",
                        icon=":material/add:",
                        type="primary",
                        use_container_width=True,
                    ):
                        new_text = st.session_state[add_key].strip()
                        if not new_text:
                            st.error("La nouvelle légende ne peut pas être vide.")
                        else:
                            current = _sync_caps_state(ESK)
                            current.append(new_text)
                            current = [c for c in current if c and c.strip()]
                            write_caption_batch(
                                batch_name, current, description=desc.strip()
                            )
                            _cleanup_caps_state(ESK)
                            st.session_state[add_key] = ""
                            st.toast(
                                "Légende ajoutée.",
                                icon=":material/check_circle:",
                            )
                            st.rerun()

                st.divider()

                # ----- Actions sur le lot -----
                c1, c2, c3 = st.columns([3, 2, 1])

                # Bouton 1 : Configurer une publication avec ce lot (redirection)
                if c1.button(
                    "Configurer une publication avec ce lot",
                    key=f"{ESK}_use_in_pub",
                    icon=":material/play_arrow:",
                    use_container_width=True,
                    type="primary",
                    help=(
                        "Redirige vers Contrôle > Publication avec ce lot de "
                        "légendes présélectionné."
                    ),
                ):
                    # Sauvegarde implicite des modifs textuelles avant redirection
                    current = _sync_caps_state(ESK)
                    current = [c for c in current if c and c.strip()]
                    if current:
                        write_caption_batch(
                            batch_name, current, description=desc.strip()
                        )
                    st.session_state["preselected_caption_batch"] = batch_name
                    st.session_state["controle_tab_widget"] = "Publication"
                    st.switch_page(_CONTROLE_PAGE_PATH)

                # Bouton 2 : Enregistrer les modifications textuelles
                if c2.button(
                    "Enregistrer les modifs",
                    key=f"{ESK}_save_btn",
                    icon=":material/save:",
                    use_container_width=True,
                    help="Sauvegarde les éditions de texte. (Ajout/suppression sont auto-sauvées.)",
                ):
                    captions = _sync_caps_state(ESK)
                    captions = [c for c in captions if c and c.strip()]
                    if not captions:
                        st.error("Au moins une légende non vide est requise.")
                    else:
                        write_caption_batch(
                            batch_name, captions, description=desc.strip()
                        )
                        _cleanup_caps_state(ESK)
                        st.toast(
                            f"Lot '{batch_name}' enregistré ({len(captions)} légende(s)).",
                            icon=":material/check_circle:",
                        )
                        st.rerun()

                # Bouton 3 : Supprimer le lot entier (helper centralisé)
                with c3:
                    if confirm_destructive_action(
                        button_label="",
                        confirm_message=(
                            f"Confirmer la suppression du lot "
                            f"**'{batch_name}'** et ses {b['size']} "
                            f"légende{'s' if b['size'] > 1 else ''} ?"
                        ),
                        state_key=f"del_caption_batch_{batch_name}",
                        button_help="Supprimer le lot entier",
                        icon=":material/delete_forever:",
                    ):
                        delete_caption_batch(batch_name)
                        _cleanup_caps_state(ESK)
                        st.toast(
                            f"Lot '{batch_name}' supprimé.",
                            icon=":material/delete:",
                        )
                        st.rerun()

    # ----- Legacy : fichiers .txt à la racine -----
    legacy = list_captions()
    if legacy:
        st.divider()
        with st.expander(f"Anciennes légendes .txt ({len(legacy)})", icon=":material/history:"):
            st.caption(
                "Ces fichiers sont utilisés uniquement si tu ne choisis aucun lot "
                "JSON lors d'une publication (fallback)."
            )
            for p in legacy:
                cols = st.columns([5, 2])
                with cols[0]:
                    st.text(p.name)
                with cols[1]:
                    if confirm_destructive_action(
                        button_label="Supprimer",
                        confirm_message=f"Supprimer **{p.name}** ?",
                        state_key=f"legacy_del_{p.name}",
                        icon=":material/delete:",
                    ):
                        from fansly_dashboard._lib import delete_caption as _del
                        _del(p.name)
                        st.toast(
                            f"{p.name} supprimé.",
                            icon=":material/delete:",
                        )
                        st.rerun()
