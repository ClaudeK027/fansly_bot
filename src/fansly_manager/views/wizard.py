"""Vue 'Ajouter un compte' — wizard 4 etapes avec noVNC integre.

Etat partage via ``st.session_state`` pour persister entre les rerun
Streamlit. Cles utilisees (prefix ``wiz_``) :

  - wiz_step      : int (1..4)  etape courante
  - wiz_name      : str         identifiant logique de l'instance
  - wiz_username  : str         email Fansly
  - wiz_password  : str         mot de passe Fansly
  - wiz_slug      : str         slug du profil
  - wiz_session   : NovncSession | None  container noVNC en cours
  - wiz_done      : bool        login confirme, instance demarree
  - wiz_error     : str | None  message d'erreur au cours du flow
"""
from __future__ import annotations

import html
import os

import streamlit as st

from fansly_manager import components, wizard as wiz


def _running_in_container() -> bool:
    """Detecte si le manager tourne dans un container Docker.

    Si oui ET que l'utilisateur accede au manager via tunnel SSH (cas
    nominal documente), l'iframe ``http://localhost:PORT`` du noVNC ne
    sera PAS joignable depuis son browser : le tunnel ne forward que le
    port 8500, pas la plage dynamique 6080-6180. Il faut afficher la
    commande SSH a executer pour ouvrir un 2eme tunnel.
    """
    return os.path.exists("/.dockerenv")


# ─── Helpers etat ────────────────────────────────────────────────────────


def _init_state() -> None:
    defaults = {
        "wiz_step": 1,
        "wiz_name": "",
        "wiz_username": "",
        "wiz_password": "",
        "wiz_slug": "",
        "wiz_session": None,
        "wiz_done": False,
        "wiz_error": None,
        "wiz_bot_port": None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def _reset() -> None:
    """Reinitialise le wizard (apres succes ou abandon)."""
    for k in [
        "wiz_step", "wiz_name", "wiz_username", "wiz_password",
        "wiz_slug", "wiz_session", "wiz_done", "wiz_error", "wiz_bot_port",
    ]:
        st.session_state.pop(k, None)
    _init_state()


def _go(step: int) -> None:
    st.session_state["wiz_step"] = step
    st.rerun()


# ─── Etapes ──────────────────────────────────────────────────────────────


def _step1_name() -> None:
    components.page_header(
        "Ajouter un compte",
        "Etape 1 sur 4 — Nom de l'instance",
    )

    st.markdown(
        '<div class="fm-empty-body" style="margin-bottom:24px;">'
        "Choisis un identifiant court pour cette instance. C'est ce nom "
        "qui apparaitra dans le tableau de bord, et qui sera utilise pour "
        "isoler les donnees de ce compte (dossier <code>data-NAME/</code>)."
        "</div>",
        unsafe_allow_html=True,
    )

    with st.form("wiz_step1"):
        name = st.text_input(
            "Identifiant (1-32 caracteres, lettres/chiffres/underscore)",
            value=st.session_state.get("wiz_name", ""),
            placeholder="ex: marie_camille",
            max_chars=32,
        )
        c1, c2 = st.columns([1, 1])
        with c1:
            cancel = st.form_submit_button("Annuler", use_container_width=True)
        with c2:
            submit = st.form_submit_button("Suivant", type="primary", use_container_width=True)

    if cancel:
        _reset()
        st.session_state["view"] = "overview"
        st.rerun()
    if submit:
        err = wiz.validate_instance_name(name)
        if err:
            st.error(err)
            return
        exists_result = wiz.instance_already_exists_or_cleanup(name)
        if not exists_result.ok:
            st.error(exists_result.error)
            return

        # Build des images Docker MAINTENANT (avant que l'user investisse
        # du temps a saisir ses credentials). Build long (~3-5 min au cold
        # start) mais a ce stade le password n'est pas encore en memoire.
        with st.spinner(
            "Preparation de l'environnement Docker "
            "(~3-5 min au tout premier ajout d'instance)..."
        ):
            bot_img_result = wiz.ensure_bot_image_built()
            if not bot_img_result.ok:
                st.error(
                    f"Build image bot Fansly impossible : {bot_img_result.error}. "
                    "Verifie le Dockerfile et la connexion internet."
                )
                return
            novnc_img_result = wiz.ensure_novnc_image_built()
            if not novnc_img_result.ok:
                st.error(
                    f"Build image noVNC impossible : {novnc_img_result.error}."
                )
                return

        st.session_state["wiz_name"] = name
        _go(2)


def _step2_credentials() -> None:
    components.page_header(
        f"Ajouter un compte : {st.session_state['wiz_name']}",
        "Etape 2 sur 4 — Identifiants Fansly",
    )

    st.markdown(
        '<div class="fm-empty-body" style="margin-bottom:24px;">'
        "Renseigne les identifiants du compte Fansly. Ils seront utilises "
        "uniquement pour pre-remplir le formulaire de login dans la fenetre "
        "qui s'ouvrira a l'etape suivante — tu pourras toujours les corriger "
        "manuellement. Le mot de passe n'est jamais loggue."
        "</div>",
        unsafe_allow_html=True,
    )

    with st.form("wiz_step2"):
        username = st.text_input(
            "Email Fansly",
            value=st.session_state.get("wiz_username", ""),
            placeholder="email@exemple.com",
        )
        password = st.text_input(
            "Mot de passe Fansly",
            value=st.session_state.get("wiz_password", ""),
            type="password",
        )
        slug = st.text_input(
            "Slug du profil Fansly",
            value=st.session_state.get("wiz_slug", ""),
            placeholder="ex: Mon_Pseudo (la partie apres fansly.com/)",
            help="La partie publique de ton URL : fansly.com/<slug>/posts",
        )
        c1, c2, c3 = st.columns([1, 1, 2])
        with c1:
            back = st.form_submit_button("Precedent", use_container_width=True)
        with c2:
            submit = st.form_submit_button(
                "Authentifier",
                type="primary",
                use_container_width=True,
            )

    if back:
        _go(1)
    if submit:
        if not username or not password or not slug:
            st.error("Les trois champs sont obligatoires.")
            return
        st.session_state["wiz_username"] = username
        st.session_state["wiz_password"] = password
        st.session_state["wiz_slug"] = slug.strip("/")

        # Les images Docker ont deja ete buildees au step 1 — on lance
        # directement le container noVNC.
        with st.spinner(
            "Demarrage de la fenetre d'authentification (~10-30s)..."
        ):
            cfg = wiz.WizardConfig(
                instance_name=st.session_state["wiz_name"],
                fansly_username=username,
                fansly_password=password,
                fansly_profile_slug=st.session_state["wiz_slug"],
            )
            session_result = wiz.start_auth_container(cfg)
            if not session_result.ok:
                st.error(f"Echec lancement container : {session_result.error}")
                return
        st.session_state["wiz_session"] = session_result.value
        _go(3)


def _step3_authenticate() -> None:
    session = st.session_state.get("wiz_session")
    if session is None:
        st.error("Session noVNC manquante. Reprends a l'etape 2.")
        if st.button("Retour"):
            _go(2)
        return

    components.page_header(
        f"Authentification : {session.instance_name}",
        "Etape 3 sur 4 — Login Fansly dans la fenetre integree",
    )

    # Verifie l'etat du container : peut etre running (en cours), exited 0
    # (login OK, prêt pour step 4), ou exited >0 (echec/timeout).
    status_result = wiz.poll_auth_status(session.container_id)
    if not status_result.ok:
        st.error(f"Impossible de joindre le container : {status_result.error}")
        return

    state = status_result.value
    container_status = state["status"]
    exit_code = state.get("exit_code")

    if container_status != "running":
        if exit_code == 0:
            # Login confirme — passe a l'etape 4 (provisioning)
            _go(4)
            return
        else:
            st.error(
                f"Le container d'authentification s'est arrete (exit code {exit_code}). "
                "Verifie le mot de passe et reessaye."
            )
            wiz.cleanup_auth_container(session.container_id)
            if st.button("Retour aux identifiants"):
                st.session_state["wiz_session"] = None
                _go(2)
            return

    # Le container tourne — affiche l'iframe noVNC
    novnc_url = (
        f"http://localhost:{session.host_port}/vnc.html"
        "?autoconnect=1&resize=scale&reconnect=1"
    )

    # Si le manager est containerise (cas nominal VPS), l'URL localhost
    # n'est pas joignable depuis le browser de l'user via le tunnel SSH
    # existant (-L 8500 ne forward QUE 8500). On affiche la commande a
    # executer pour ouvrir un 2eme tunnel sur le port dynamique alloue.
    if _running_in_container():
        tunnel_cmd = (
            f"ssh -L {session.host_port}:localhost:{session.host_port} "
            f"<user>@<vps>"
        )
        st.info(
            "**Acces a l'iframe noVNC en mode VPS** : si tu connectes le "
            "manager via tunnel SSH (`-L 8500:localhost:8500`), tu dois "
            "ouvrir UN SECOND TUNNEL pour le port dynamique de cette "
            "session :\n\n"
            f"```bash\n{tunnel_cmd}\n```\n"
            "Garde-le ouvert le temps du login. Si tu es deja sur la meme "
            "machine que le manager (acces local), ignore ce message — "
            "l'iframe se chargera directement."
        )

    st.markdown(
        '<div class="fm-empty-body" style="margin-bottom:16px;">'
        "Fais ton login Fansly dans la fenetre ci-dessous "
        "(Cloudflare et 2FA fonctionnent normalement). Une fois sur la "
        "page d'accueil Fansly, le wizard passera automatiquement a "
        "l'etape suivante."
        "</div>",
        unsafe_allow_html=True,
    )

    # iframe noVNC. Hauteur generuese pour confort de login.
    safe_url = html.escape(novnc_url)
    st.markdown(
        f'<iframe src="{safe_url}" '
        f'style="width:100%;height:720px;border:1px solid var(--fm-border);'
        f'border-radius:var(--fm-radius);background:#000;"></iframe>',
        unsafe_allow_html=True,
    )

    st.caption(
        "L'iframe se reconnecte automatiquement si la connexion se perd. "
        "Si elle reste noire >10s, recharge la page entiere."
    )

    # Auto-refresh : Streamlit n'a pas de poll natif. On utilise un bouton
    # "Verifier" et on documente. Une alternative serait st_autorefresh.
    c1, c2 = st.columns([1, 4])
    with c1:
        if st.button("Verifier l'avancement", use_container_width=True):
            st.rerun()
    with c2:
        if st.button("Annuler et nettoyer", type="secondary"):
            wiz.cleanup_auth_container(session.container_id)
            _reset()
            st.session_state["view"] = "overview"
            st.rerun()


def _step4_provision() -> None:
    session = st.session_state.get("wiz_session")
    if session is None:
        st.error("Session manquante. Reprends a l'etape 2.")
        return

    components.page_header(
        f"Finalisation : {session.instance_name}",
        "Etape 4 sur 4 — Demarrage du bot",
    )

    if st.session_state.get("wiz_done"):
        # Deja provisionne — affiche le succes
        st.success(
            f"Instance **{session.instance_name}** demarree avec succes. "
            f"Dashboard : http://localhost:{st.session_state['wiz_bot_port']}"
        )
        st.markdown(
            f'<a href="http://localhost:{st.session_state["wiz_bot_port"]}" '
            f'target="_blank" rel="noopener" class="fm-btn-primary">'
            f'Ouvrir le dashboard</a>',
            unsafe_allow_html=True,
        )
        if st.button("Retour a la vue d'ensemble"):
            _reset()
            st.session_state["view"] = "overview"
            st.rerun()
        return

    # Provisionning transactionnel : si une etape echoue apres write_env_file,
    # on ROLLBACK (supprime .env.NAME) pour eviter de laisser un fichier de
    # credentials orphelin sur disque. Le cleanup du container noVNC est
    # garanti dans tous les cas via try/finally.
    cfg = wiz.WizardConfig(
        instance_name=st.session_state["wiz_name"],
        fansly_username=st.session_state["wiz_username"],
        fansly_password=st.session_state["wiz_password"],
        fansly_profile_slug=st.session_state["wiz_slug"],
    )

    bot_result = None
    error_msg = None
    env_file_written = False
    try:
        with st.spinner(
            "Sauvegarde des credentials et demarrage du container bot..."
        ):
            env_result = wiz.write_env_file(cfg)
            if not env_result.ok:
                error_msg = f"Echec ecriture .env : {env_result.error}"
            else:
                env_file_written = True
                bot_result = wiz.start_bot_instance(cfg)
                if not bot_result.ok:
                    error_msg = f"Echec demarrage du bot : {bot_result.error}"
    finally:
        # Cleanup garanti du container noVNC, meme en cas d'erreur
        # (sinon container orphelin avec creds en clair indefiniment).
        wiz.cleanup_auth_container(session.container_id)
        # Rollback : si on a ecrit le .env mais que la suite a echoue,
        # on le supprime (sinon fichier de creds orphelin lisible).
        if env_file_written and (bot_result is None or not bot_result.ok):
            wiz.delete_env_file(cfg.instance_name)

    if error_msg:
        st.error(error_msg)
        # IMPORTANT : meme si on a supprime .env.NAME ci-dessus, le dossier
        # data-NAME/browser_profile/ contient les cookies de session Fansly
        # ecrits par le container noVNC pendant le login (auth_token,
        # localStorage, IndexedDB). Equivalent en valeur a .env.NAME pour
        # une compromission de compte. Le manager ne peut PAS le supprimer
        # directement (pas de bind mount data/ — hardening Phase 5).
        # On previent l'user pour qu'il decide de cleanup manuel.
        if env_file_written:
            st.warning(
                f"**Attention** : le dossier `data-{cfg.instance_name}/browser_profile/` "
                "contient les cookies de session Fansly de la tentative qui vient "
                "d'echouer. Si tu n'essaies pas immediatement de relancer le wizard, "
                "supprime-le manuellement sur le VPS :\n\n"
                f"```bash\nsudo rm -rf data-{cfg.instance_name}/\n```"
            )
        c1, c2 = st.columns([1, 1])
        with c1:
            if st.button("Reessayer", key="step4_retry"):
                # On revient au step 2 pour relancer un container noVNC
                # avec les memes credentials (deja en session_state).
                _go(2)
        with c2:
            if st.button("Annuler le wizard", key="step4_cancel"):
                _reset()
                st.session_state["view"] = "overview"
                st.rerun()
        return

    # Invalidate cache pour que la vue d'ensemble voit la nouvelle instance
    try:
        from fansly_manager.main import _cached_list_instances
        _cached_list_instances.clear()
    except Exception:  # noqa: BLE001
        pass

    st.session_state["wiz_done"] = True
    st.session_state["wiz_bot_port"] = bot_result.value["host_port"]
    st.rerun()


# ─── Dispatcher ──────────────────────────────────────────────────────────


def render() -> None:
    _init_state()
    step = st.session_state["wiz_step"]
    if step == 1:
        _step1_name()
    elif step == 2:
        _step2_credentials()
    elif step == 3:
        _step3_authenticate()
    elif step == 4:
        _step4_provision()
    else:
        st.error(f"Etape inconnue : {step}")
        _reset()
