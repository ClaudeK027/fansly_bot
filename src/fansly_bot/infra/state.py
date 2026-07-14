# src/fansly_bot/infra/state.py
"""Etat persistant SQLite : posts_seen, purge_runs, media_published.

Conception :
  - Une seule connexion partagee, mode WAL pour les ecritures concurrentes.
  - Tous les acces passent par des methodes typees (pas de SQL eparpille ailleurs).
  - sqlite3 est synchrone : on l'enveloppe via asyncio.to_thread() depuis les
    services async pour ne pas bloquer la boucle.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import structlog

from ..config import Settings

log = structlog.get_logger("infra.state")


# ---------- Schemas ----------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS posts_seen (
    post_id          TEXT PRIMARY KEY,
    first_seen_at    TEXT NOT NULL,
    post_created_at  TEXT,
    caption_excerpt  TEXT,
    last_examined_at TEXT NOT NULL,
    decision         TEXT NOT NULL,
    decision_reason  TEXT
);
CREATE INDEX IF NOT EXISTS idx_posts_seen_decision ON posts_seen(decision);
CREATE INDEX IF NOT EXISTS idx_posts_seen_examined ON posts_seen(last_examined_at);

CREATE TABLE IF NOT EXISTS purge_runs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at         TEXT NOT NULL,
    finished_at        TEXT NOT NULL,
    examined           INTEGER NOT NULL,
    candidates         INTEGER NOT NULL,
    deleted            INTEGER NOT NULL,
    skipped            INTEGER NOT NULL,
    scroll_cap_hit     INTEGER NOT NULL,
    max_deletions_hit  INTEGER NOT NULL,
    dry_run            INTEGER NOT NULL
);

-- Historique des publications. Cle composite (run_id, batch_name, cycle_number,
-- media_filename) : permet de distinguer plusieurs runs sur le meme lot,
-- plusieurs cycles dans un run, plusieurs medias dans un cycle. La clause
-- UNIQUE empeche les doublons en cas de retry du worker.
CREATE TABLE IF NOT EXISTS media_published (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           INTEGER,  -- = job_queue.id du job publish qui a produit cette ligne
    batch_name       TEXT,
    cycle_number     INTEGER,
    media_filename   TEXT NOT NULL,
    published_at     TEXT NOT NULL,
    caption_used     TEXT,
    fansly_post_id   TEXT,
    UNIQUE (run_id, batch_name, cycle_number, media_filename)
);
CREATE INDEX IF NOT EXISTS idx_media_published_run
    ON media_published(run_id, batch_name, cycle_number);

-- Table des "publications en cours" : ecrite AVANT le clic Post Fansly,
-- supprimee apres confirmation. Au demarrage du worker, les lignes residuelles
-- signalent un crash entre publication Fansly et confirmation locale — on les
-- traite comme deja publiees (mieux vaut un manque qu'un doublon).
CREATE TABLE IF NOT EXISTS publish_in_flight (
    run_id           INTEGER NOT NULL,
    batch_name       TEXT NOT NULL,
    cycle_number     INTEGER NOT NULL,
    media_filename   TEXT NOT NULL,
    caption_used     TEXT,
    started_at       TEXT NOT NULL,
    -- clicked : 0 = write-ahead ecrit mais le clic Post PAS ENCORE emis ;
    --           1 = le clic Post a ete emis (donc un post a PU etre cree).
    -- A la reprise : clicked=0 => aucun POST parti => republier en surete ;
    --               clicked=1 => post peut exister => JAMAIS republier.
    clicked          INTEGER NOT NULL DEFAULT 0,
    -- fansly_post_id : rempli DES la reponse 2xx de POST /api/v1/post (dans le
    -- listener, premier-gagne). Sa presence PROUVE que le post existe.
    fansly_post_id   TEXT,
    PRIMARY KEY (run_id, batch_name, cycle_number, media_filename)
);

-- Singleton (id = 1) representant le lot actif. Si absent : aucun lot actif.
CREATE TABLE IF NOT EXISTS active_batch (
    id                 INTEGER PRIMARY KEY CHECK (id = 1),
    name               TEXT NOT NULL,
    started_at         TEXT NOT NULL,
    max_cycles         INTEGER NOT NULL DEFAULT 0,
    current_cycle      INTEGER NOT NULL DEFAULT 1,
    published_in_cycle TEXT NOT NULL DEFAULT '[]',  -- JSON list de filenames
    total_published    INTEGER NOT NULL DEFAULT 0,
    playlist_order     TEXT NOT NULL DEFAULT '[]',  -- JSON list : ordre fige tire au 1er cycle,
                                                    -- conserve entre cycles (A1 : nouveaux fichiers
                                                    -- ajoutes a la fin ; B1 : fichiers manquants sautes).
    -- run_id : ancre STABLE du lot pour toute sa vie (survit aux re-queues et
    -- redemarrages). media_published.run_id et publish_in_flight.run_id valent
    -- ce run_id (et NON job.id, volatil). Ecrit par start_or_resume_batch ;
    -- jamais NULL en pratique (migration backfille les lots pre-existants).
    run_id             INTEGER
);

-- File d'attente de jobs (publication, purge).
-- Le worker lit cette table en boucle, prend le prochain `queued`, l'execute,
-- et marque le resultat.
CREATE TABLE IF NOT EXISTS job_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    type        TEXT NOT NULL,        -- 'publish' | 'purge'
    config      TEXT NOT NULL,        -- JSON
    status      TEXT NOT NULL,        -- queued|running|done|failed|cancelled|cancelling
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT,
    log_path    TEXT,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_jq_status ON job_queue(status);
CREATE INDEX IF NOT EXISTS idx_jq_created ON job_queue(created_at);
"""


# ---------- DTO ----------

@dataclass
class PostRecord:
    post_id: str
    first_seen_at: datetime
    post_created_at: Optional[datetime]
    caption_excerpt: Optional[str]
    last_examined_at: datetime
    decision: str
    decision_reason: Optional[str]


@dataclass
class PurgeRunReport:
    started_at: datetime
    finished_at: datetime
    examined: int
    candidates: int
    deleted: int
    skipped: int
    scroll_cap_hit: bool
    max_deletions_hit: bool
    dry_run: bool


@dataclass
class ActiveBatch:
    name: str
    started_at: datetime
    max_cycles: int                 # 0 = infini
    current_cycle: int
    published_in_cycle: list[str]   # noms de fichiers deja publies dans le cycle courant
    total_published: int
    playlist_order: list[str]       # Ordre fige tire au 1er cycle. Se conserve entre cycles.
                                    # Vide tant que le 1er tirage n'a pas eu lieu.
    run_id: Optional[int] = None    # Ancre stable du lot (=id du job qui l'a demarre).
                                    # Utilise pour media_published.run_id + publish_in_flight.


@dataclass
class Job:
    id: int
    type: str                        # 'publish' | 'purge'
    config: dict                     # parse de la colonne JSON
    status: str                      # queued|running|done|failed|cancelled|cancelling
    created_at: datetime
    started_at: Optional[datetime]
    finished_at: Optional[datetime]
    log_path: Optional[str]
    error: Optional[str]


# ---------- StateStore ----------

class StateStore:
    """Facade synchrone autour de sqlite3 ; les services async appellent via to_thread()."""

    def __init__(self, settings: Settings) -> None:
        self._path: Path = settings.paths.state_db
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Mode par defaut : sqlite3 gere les transactions implicitement,
        # `with self._conn:` ou `self._conn.commit()` valide.
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        # busy_timeout=5000 : si une autre connexion (UI Streamlit, autre
        # processus) detient un write lock sur le WAL, SQLite attend jusqu'a
        # 5s pour acquerir le lock plutot que de retourner immediatement
        # SQLITE_BUSY. Avant ce fix, busy_timeout=0 par defaut pouvait causer
        # un blocage indefini sur lock contention (worker en epoll_wait pendant
        # 2h+ apres publish_waiting_next, audit hang job 49).
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._lock = asyncio.Lock()
        self._init_schema()
        log.info("state_store_ready", path=str(self._path))

    # ----- init / lifecycle -----

    def _init_schema(self) -> None:
        # Etape 1 : detecter une eventuelle ancienne version de media_published
        # (PK sur media_filename avec cle concatenee "filename::batch::cycleN").
        # Si presente, on la renomme avant que CREATE TABLE IF NOT EXISTS ne
        # cree la nouvelle. L'ancien historique reste consultable mais n'est
        # plus utilise par le code.
        cur = self._conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='media_published'"
        )
        table_exists = cur.fetchone() is not None
        needs_migration = False
        if table_exists:
            cur = self._conn.execute("PRAGMA table_info(media_published)")
            cols = {row["name"] for row in cur.fetchall()}
            # Marqueurs de la nouvelle structure : presence de `id` + `run_id`
            # ET clause UNIQUE sur (run_id, batch_name, cycle_number, media_filename).
            # Si l'un des deux marqueurs manque, on migre.
            if "run_id" not in cols or "id" not in cols:
                needs_migration = True

        if needs_migration:
            # On renomme l'ancienne table en _legacy_v1 et on cree la nouvelle.
            # L'historique reste accessible via SELECT direct si besoin de debug
            # mais le code applicatif ne le lit plus.
            log.warning(
                "media_published_schema_migration",
                action="renaming_legacy_table",
                target="media_published_legacy_v1",
            )
            # Drop d'une eventuelle table _legacy d'une migration anterieure
            self._conn.execute("DROP TABLE IF EXISTS media_published_legacy_v1")
            self._conn.execute(
                "ALTER TABLE media_published RENAME TO media_published_legacy_v1"
            )

        # Etape 2 : applique le schema complet (CREATE TABLE IF NOT EXISTS donc
        # idempotent — la nouvelle media_published est creee si elle n'existait
        # pas ou vient d'etre renommee).
        self._conn.executescript(_SCHEMA)

        # Etape 3 : migrations legeres pour DBs pre-existantes.
        # active_batch.playlist_order : ajoute si absent. Sans cette clause,
        # CREATE TABLE IF NOT EXISTS ne rajoute PAS la colonne aux tables deja
        # creees dans une version anterieure.
        cur = self._conn.execute("PRAGMA table_info(active_batch)")
        active_batch_cols = {row["name"] for row in cur.fetchall()}
        if active_batch_cols and "playlist_order" not in active_batch_cols:
            log.info("state_schema_migration", table="active_batch", add_column="playlist_order")
            self._conn.execute(
                "ALTER TABLE active_batch ADD COLUMN playlist_order TEXT NOT NULL DEFAULT '[]'"
            )

        # publish_in_flight : colonnes clicked + fansly_post_id (crash-resume).
        cur = self._conn.execute("PRAGMA table_info(publish_in_flight)")
        pif_cols = {row["name"] for row in cur.fetchall()}
        if pif_cols and "clicked" not in pif_cols:
            log.info("state_schema_migration", table="publish_in_flight", add_column="clicked")
            self._conn.execute(
                "ALTER TABLE publish_in_flight ADD COLUMN clicked INTEGER NOT NULL DEFAULT 0"
            )
        if pif_cols and "fansly_post_id" not in pif_cols:
            log.info("state_schema_migration", table="publish_in_flight", add_column="fansly_post_id")
            self._conn.execute(
                "ALTER TABLE publish_in_flight ADD COLUMN fansly_post_id TEXT"
            )

        # active_batch.run_id : ancre stable du lot. MIGRATION VERROUILLEE —
        # un run_id NULL casserait a la fois l'idempotence UNIQUE de
        # media_published (NULL != NULL en SQLite => ON CONFLICT ne se declenche
        # jamais) ET la rotation (ValueError sur run_id None). Donc :
        #   - si un active_batch pre-existe sans run_id : on le backfille depuis
        #     l'id du dernier job publish connu, et on REECRIT media_published.
        #     run_id de ce batch pour rester coherent ;
        #   - si aucun job publish trouvable : on supprime l'active_batch (stop
        #     propre) plutot que de laisser un run_id indefini remonter.
        if active_batch_cols and "run_id" not in active_batch_cols:
            log.info("state_schema_migration", table="active_batch", add_column="run_id")
            self._conn.execute("ALTER TABLE active_batch ADD COLUMN run_id INTEGER")
            row = self._conn.execute(
                "SELECT name FROM active_batch WHERE id = 1"
            ).fetchone()
            if row is not None:
                batch_name = row["name"]
                job = self._conn.execute(
                    "SELECT id FROM job_queue WHERE type = 'publish' "
                    "ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if job is not None:
                    backfill_run_id = job["id"]
                    self._conn.execute(
                        "UPDATE active_batch SET run_id = ? WHERE id = 1",
                        (backfill_run_id,),
                    )
                    # Coherence : toutes les lignes media_published de ce batch
                    # doivent porter le meme run_id (sinon UNIQUE/rotation KO).
                    self._conn.execute(
                        "UPDATE media_published SET run_id = ? WHERE batch_name = ?",
                        (backfill_run_id, batch_name),
                    )
                    log.warning(
                        "active_batch_run_id_backfilled",
                        batch=batch_name, run_id=backfill_run_id,
                    )
                else:
                    # Aucun job publish : active_batch orphelin, stop propre.
                    self._conn.execute("DELETE FROM active_batch WHERE id = 1")
                    log.warning(
                        "active_batch_dropped_no_run_id",
                        batch=batch_name,
                        rationale="migration_run_id_sans_job_publish",
                    )

        self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception as e:  # noqa: BLE001
            log.warning("state_store_close_error", error=str(e))

    # ----- posts_seen -----

    def upsert_post(
        self,
        post_id: str,
        decision: str,
        reason: str | None,
        post_created_at: datetime | None,
        caption_excerpt: str | None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO posts_seen
                    (post_id, first_seen_at, post_created_at, caption_excerpt,
                     last_examined_at, decision, decision_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(post_id) DO UPDATE SET
                    post_created_at  = COALESCE(excluded.post_created_at, post_created_at),
                    caption_excerpt  = COALESCE(excluded.caption_excerpt, caption_excerpt),
                    last_examined_at = excluded.last_examined_at,
                    decision         = excluded.decision,
                    decision_reason  = excluded.decision_reason
                """,
                (
                    post_id,
                    now,
                    post_created_at.isoformat() if post_created_at else None,
                    caption_excerpt,
                    now,
                    decision,
                    reason,
                ),
            )

    def get_post(self, post_id: str) -> Optional[PostRecord]:
        cur = self._conn.execute("SELECT * FROM posts_seen WHERE post_id = ?", (post_id,))
        row = cur.fetchone()
        return _row_to_post(row) if row else None

    def mark_post_deleted(self, post_id: str, reason: str) -> None:
        self.upsert_post(post_id, "DELETED", reason, None, None)

    # ----- purge_runs -----

    def record_purge_run(self, report: PurgeRunReport) -> int:
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO purge_runs
                    (started_at, finished_at, examined, candidates, deleted,
                     skipped, scroll_cap_hit, max_deletions_hit, dry_run)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report.started_at.isoformat(),
                    report.finished_at.isoformat(),
                    report.examined,
                    report.candidates,
                    report.deleted,
                    report.skipped,
                    int(report.scroll_cap_hit),
                    int(report.max_deletions_hit),
                    int(report.dry_run),
                ),
            )
            return int(cur.lastrowid or 0)

    # ----- media_published -----

    def is_media_published(self, filename: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM media_published WHERE media_filename = ?", (filename,)
        )
        return cur.fetchone() is not None

    def record_media_published(
        self,
        filename: str,
        caption: str,
        fansly_post_id: str | None = None,
        batch_name: str | None = None,
        cycle_number: int | None = None,
        run_id: int | None = None,
    ) -> None:
        """Enregistre une publication.

        La cle d unicite est (run_id, batch_name, cycle_number, media_filename).
        Si la ligne existe deja (cas d un retry idempotent), on ignore le doublon
        au lieu d ecraser.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO media_published
                    (run_id, batch_name, cycle_number, media_filename,
                     published_at, caption_used, fansly_post_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (run_id, batch_name, cycle_number, media_filename)
                DO NOTHING
                """,
                (
                    run_id, batch_name, cycle_number, filename,
                    now, caption, fansly_post_id,
                ),
            )

    def get_fansly_post_id_for_previous_cycle(
        self,
        run_id: int,
        batch_name: str,
        media_filename: str,
        current_cycle: int,
    ) -> str | None:
        """Cherche l'ID Fansly du meme media publie dans un cycle ANTERIEUR
        du MEME run/batch. Utilise par le CycleRotator pour identifier le
        post precedent a supprimer avant de republier ce media au cycle
        courant.

        Strategie : on prend le cycle le plus recent strictement inferieur
        a `current_cycle`. Si plusieurs lignes existent (idempotence non
        garantie sur d'anciens runs), on garde celle avec l'id BDD le plus
        grand (= la plus recente inseree).

        SECURITE : `run_id` est OBLIGATOIRE (raise ValueError si None). Une
        recherche cross-run pourrait remonter l'id d'un post publie par un
        autre run sur le meme batch — risque de suppression accidentelle.
        L'isolation par run_id est la SEULE garantie de coherence.

        Retourne None si :
          - aucun cycle precedent n'a publie ce media (ex. premier cycle)
          - le cycle precedent existe mais sans fansly_post_id (capture
            ratee) — pas de rotation possible pour ce media-ci.
        """
        if run_id is None:
            raise ValueError(
                "run_id is required for cross-cycle lookup "
                "(cross-run lookup is unsafe and disabled)"
            )
        cur = self._conn.execute(
            """
            SELECT fansly_post_id FROM media_published
            WHERE run_id = ?
              AND batch_name = ?
              AND media_filename = ?
              AND cycle_number < ?
              AND fansly_post_id IS NOT NULL
            ORDER BY cycle_number DESC, id DESC
            LIMIT 1
            """,
            (run_id, batch_name, media_filename, current_cycle),
        )
        row = cur.fetchone()
        return row[0] if row else None

    def get_unconfirmed_media(self, run_id: int) -> set[str]:
        """Medias de CE run publies SANS fansly_post_id capture (zombies).

        Un 'zombie' nait quand un crash survient dans la fenetre clic Post ->
        capture de l'id : reconcile marque le media publie avec
        fansly_post_id=NULL (branche clicked=1 sans id). Le post EXISTE peut-etre
        sur Fansly mais son id est inconnu, donc :
          - il n'est PAS rotable (get_fansly_post_id_for_previous_cycle exige
            fansly_post_id IS NOT NULL) ;
          - le republier au cycle suivant creerait un DOUBLON permanent que la
            rotation ne pourra jamais nettoyer.

        GARANTIE ANTI-DOUBLON : la selection de media (uploader) EXCLUT ces
        fichiers des cycles suivants -> on ne republie jamais un media dont on
        n'a pas pu confirmer/roter le post precedent. Scope run_id (stable a
        travers les re-queues) : n'affecte que le run courant, pas un autre lot.

        Cette liste est aussi la source de donnees de la future file de
        verification manuelle (WS8) : chaque entree = 'un post a peut-etre ete
        cree mais non confirme, a verifier sur le compte'.
        """
        cur = self._conn.execute(
            """
            SELECT DISTINCT media_filename FROM media_published
            WHERE run_id = ? AND fansly_post_id IS NULL
            """,
            (run_id,),
        )
        return {row[0] for row in cur.fetchall()}

    def get_published_media_in_cycle(self, batch_name: str, cycle_number: int) -> set[str]:
        """Medias deja publies dans (batch, cycle) — TOUS runs confondus.

        Source durable = media_published (persistant), independante de
        active_batch.published_in_cycle qui, lui, est PAR RUN et remis a zero au
        demarrage d'un nouveau run.

        ANTI-DOUBLON cancel->restart : quand un job publish est annule puis
        relance, le nouveau run repart avec published_in_cycle vide alors que le
        run precedent a deja publie des medias DANS CE CYCLE. Sans cette source
        cross-run, ces medias seraient re-selectionnes et republies (doublon
        in-cycle non rotable, car la rotation ne cible que les cycles ANTERIEURS).
        La selection exclut donc l'union (published_in_cycle | ce set).

        Scope cycle_number STRICT : en cycle N+1, les publications du cycle N
        ne bloquent pas (republication cyclique voulue, avec rotation).
        """
        cur = self._conn.execute(
            """
            SELECT DISTINCT media_filename FROM media_published
            WHERE batch_name = ? AND cycle_number = ?
            """,
            (batch_name, cycle_number),
        )
        return {row[0] for row in cur.fetchall()}

    def get_published_post_ids_in_window(
        self, start_iso: str, end_iso: str
    ) -> list[tuple[str, str, str]]:
        """Renvoie les posts publies par le bot dans une fenetre de dates,
        avec leur fansly_post_id STABLE (capture a la publication).

        Utilise par la purge "par IDs stockes" : au lieu de scroller le feed
        profil (fragile pour la suppression), on supprime directement chaque
        post via son permalien fansly.com/post/<id> — methode fiable et
        eprouvee (identique au CycleRotator).

        Retour : liste de (fansly_post_id, media_filename, published_at),
        triee par date de publication. Seuls les posts AVEC un fansly_post_id
        non-null sont inclus (les rares captures ratees ne sont pas ciblables
        par permalien). Deduplique par fansly_post_id (garde la 1ere occurrence).
        """
        # Comparaison sur DATE() (et non chaine ISO brute) : robuste aux
        # differences de fuseau/precision dans published_at (avec ou sans
        # +00:00, microsecondes variables). SQLite DATE() parse l'ISO8601 et
        # renvoie 'YYYY-MM-DD'. Fenetre INCLUSIVE des deux jours bornes.
        cur = self._conn.execute(
            """
            SELECT fansly_post_id, media_filename, published_at
            FROM media_published
            WHERE DATE(published_at) >= DATE(?)
              AND DATE(published_at) <= DATE(?)
              AND fansly_post_id IS NOT NULL
              AND fansly_post_id != ''
            ORDER BY published_at
            """,
            (start_iso, end_iso),
        )
        seen: set[str] = set()
        out: list[tuple[str, str, str]] = []
        for pid, media, pub_at in cur.fetchall():
            if pid in seen:
                continue
            seen.add(pid)
            out.append((pid, media, pub_at))
        return out

    # ----- publish_in_flight -----

    def mark_publish_in_flight(
        self,
        run_id: int,
        batch_name: str,
        cycle_number: int,
        media_filename: str,
        caption: str,
    ) -> None:
        """Inscrit une publication 'en cours' AVANT le clic Post Fansly.

        Sera nettoyee une fois la publication confirmee
        (clear_publish_in_flight). Si une ligne identique existe deja (retry
        ideompotent), on ne fait rien.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO publish_in_flight
                    (run_id, batch_name, cycle_number, media_filename,
                     caption_used, started_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (run_id, batch_name, cycle_number, media_filename)
                DO NOTHING
                """,
                (run_id, batch_name, cycle_number, media_filename, caption, now),
            )

    def has_publish_in_flight(
        self,
        run_id: int,
        batch_name: str,
        cycle_number: int,
        media_filename: str,
    ) -> bool:
        """Vrai si une publication 'en cours' existe deja pour ce tuple.

        Utilise par l uploader pour decider si un retry doit republier ou
        considerer la premiere tentative comme deja partie cote Fansly.
        """
        cur = self._conn.execute(
            "SELECT 1 FROM publish_in_flight "
            "WHERE run_id = ? AND batch_name = ? AND cycle_number = ? "
            "AND media_filename = ?",
            (run_id, batch_name, cycle_number, media_filename),
        )
        return cur.fetchone() is not None

    def clear_publish_in_flight(
        self,
        run_id: int,
        batch_name: str,
        cycle_number: int,
        media_filename: str,
    ) -> None:
        with self._conn:
            self._conn.execute(
                "DELETE FROM publish_in_flight "
                "WHERE run_id = ? AND batch_name = ? AND cycle_number = ? "
                "AND media_filename = ?",
                (run_id, batch_name, cycle_number, media_filename),
            )

    def set_in_flight_post_id(
        self,
        run_id: int,
        batch_name: str,
        cycle_number: int,
        media_filename: str,
        fansly_post_id: str,
    ) -> None:
        """Persiste le fansly_post_id d'une publication en cours DES la reponse
        2xx (premier-gagne : on n'ecrase pas un id deja pose). Rend durable la
        preuve de creation le plus tot possible => reduit la fenetre de crash
        'post existe mais aucune trace en base'."""
        with self._conn:
            self._conn.execute(
                "UPDATE publish_in_flight SET fansly_post_id = ? "
                "WHERE run_id = ? AND batch_name = ? AND cycle_number = ? "
                "AND media_filename = ? AND fansly_post_id IS NULL",
                (fansly_post_id, run_id, batch_name, cycle_number, media_filename),
            )

    def mark_in_flight_clicked(
        self,
        run_id: int,
        batch_name: str,
        cycle_number: int,
        media_filename: str,
    ) -> None:
        """Marque qu'un clic Post a ete emis pour cette publication (clicked=1).
        A appeler JUSTE avant hover_then_click(submit), commit avant le clic."""
        with self._conn:
            self._conn.execute(
                "UPDATE publish_in_flight SET clicked = 1 "
                "WHERE run_id = ? AND batch_name = ? AND cycle_number = ? "
                "AND media_filename = ?",
                (run_id, batch_name, cycle_number, media_filename),
            )

    def get_in_flight(
        self,
        run_id: int,
        batch_name: str,
        cycle_number: int,
        media_filename: str,
    ) -> Optional[dict]:
        """Retourne la ligne publish_in_flight (dict) ou None."""
        cur = self._conn.execute(
            "SELECT run_id, batch_name, cycle_number, media_filename, "
            "caption_used, started_at, clicked, fansly_post_id "
            "FROM publish_in_flight WHERE run_id = ? AND batch_name = ? "
            "AND cycle_number = ? AND media_filename = ?",
            (run_id, batch_name, cycle_number, media_filename),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def list_orphan_in_flight(self) -> list[dict]:
        """Liste les publications 'en cours' restees orphelines (crash worker).

        Retourne une liste de dicts avec clicked + fansly_post_id — la
        reconciliation decidera : clicked=0 => republier ; clicked=1+id =>
        publie confirme ; clicked=1 sans id => ambigu (skip + alerte).
        """
        cur = self._conn.execute(
            "SELECT run_id, batch_name, cycle_number, media_filename, "
            "caption_used, started_at, clicked, fansly_post_id "
            "FROM publish_in_flight"
        )
        return [dict(r) for r in cur.fetchall()]

    # ----- active_batch -----

    def get_active_batch(self) -> Optional[ActiveBatch]:
        cur = self._conn.execute("SELECT * FROM active_batch WHERE id = 1")
        row = cur.fetchone()
        if not row:
            return None
        # playlist_order / run_id optionnels (DB pre-migration) : fallback
        try:
            playlist_raw = row["playlist_order"]
        except (IndexError, KeyError):
            playlist_raw = "[]"
        try:
            run_id_val = row["run_id"]
        except (IndexError, KeyError):
            run_id_val = None
        return ActiveBatch(
            name=row["name"],
            started_at=datetime.fromisoformat(row["started_at"]),
            max_cycles=row["max_cycles"],
            current_cycle=row["current_cycle"],
            published_in_cycle=json.loads(row["published_in_cycle"]),
            total_published=row["total_published"],
            playlist_order=json.loads(playlist_raw or "[]"),
            run_id=run_id_val,
        )

    def start_batch(self, name: str, max_cycles: int = 0) -> None:
        # Un nouveau batch demarre TOUJOURS avec une playlist vide : elle sera
        # tiree aleatoirement au 1er appel de publish_next (cf uploader.py).
        # Consequence attendue : chaque nouveau batch a un nouvel ordre aleatoire.
        now = datetime.now(timezone.utc).isoformat()
        with self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO active_batch
                    (id, name, started_at, max_cycles, current_cycle,
                     published_in_cycle, total_published, playlist_order)
                VALUES (1, ?, ?, ?, 1, '[]', 0, '[]')
                """,
                (name, now, max_cycles),
            )

    def start_or_resume_batch(
        self, name: str, max_cycles: int, run_id: int
    ) -> int:
        """Demarre un lot OU le reprend s'il existe deja (crash-resume).

        - active_batch de MEME nom deja present (avec run_id) => RESUME : on
          preserve current_cycle, published_in_cycle, playlist_order,
          total_published et le run_id EXISTANT (on ignore le run_id fourni).
          Retourne le run_id existant. La position dans le cycle est intacte.
        - sinon => DEMARRAGE FRAIS : INSERT OR REPLACE (cycle 1, listes vides)
          avec le run_id fourni comme ancre stable. Retourne ce run_id.

        Le worker distingue reprise vs (re)demarrage voulu par la SEULE presence
        d'un active_batch de meme nom : pour repartir de zero, l'utilisateur
        stoppe d'abord le lot (stop_batch supprime l'active_batch).
        """
        existing = self.get_active_batch()
        if existing is not None and existing.name == name and existing.run_id is not None:
            log.info(
                "batch_resume",
                batch=name, cycle=existing.current_cycle,
                published=len(existing.published_in_cycle), run_id=existing.run_id,
            )
            return existing.run_id
        now = datetime.now(timezone.utc).isoformat()
        with self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO active_batch
                    (id, name, started_at, max_cycles, current_cycle,
                     published_in_cycle, total_published, playlist_order, run_id)
                VALUES (1, ?, ?, ?, 1, '[]', 0, '[]', ?)
                """,
                (name, now, max_cycles, run_id),
            )
        log.info("batch_start_fresh", batch=name, run_id=run_id)
        return run_id

    def commit_publication(
        self,
        *,
        run_id: int,
        batch_name: str,
        cycle_number: int,
        media_filename: str,
        caption: Optional[str],
        fansly_post_id: Optional[str],
        add_to_current_cycle: bool,
    ) -> None:
        """Valide une publication de maniere ATOMIQUE : media_published +
        published_in_cycle (si le media appartient au cycle courant) + clear de
        publish_in_flight, le tout dans UNE seule transaction.

        Les 3 ecritures sont INLINEES (pas d'appel aux helpers, dont les propres
        `with self._conn:` casseraient l'atomicite par commit anticipe -> CP4).
        Idempotent : ON CONFLICT DO NOTHING + garde 'not in published'.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO media_published
                    (run_id, batch_name, cycle_number, media_filename,
                     published_at, caption_used, fansly_post_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (run_id, batch_name, cycle_number, media_filename)
                DO NOTHING
                """,
                (run_id, batch_name, cycle_number, media_filename, now, caption, fansly_post_id),
            )
            if add_to_current_cycle:
                row = self._conn.execute(
                    "SELECT published_in_cycle FROM active_batch WHERE id = 1"
                ).fetchone()
                if row is not None:
                    published = json.loads(row["published_in_cycle"] or "[]")
                    if media_filename not in published:
                        published.append(media_filename)
                        self._conn.execute(
                            "UPDATE active_batch SET published_in_cycle = ?, "
                            "total_published = total_published + 1 WHERE id = 1",
                            (json.dumps(published),),
                        )
            self._conn.execute(
                "DELETE FROM publish_in_flight WHERE run_id = ? AND batch_name = ? "
                "AND cycle_number = ? AND media_filename = ?",
                (run_id, batch_name, cycle_number, media_filename),
            )

    def reconcile_publish_in_flight(
        self, run_id_filter: Optional[int] = None
    ) -> dict:
        """Reconcilie les publications 'en cours' orphelines. Appele au boot du
        worker (run_id_filter=None : toutes) ET au top de publish_next
        (run_id_filter=run_id courant : couvre le failsafe-timeout in-session).

        Regle NON NEGOCIABLE (anti-doublon) : une orpheline ne mene JAMAIS a
        'republier' sauf preuve durable qu'aucun POST n'est parti (clicked=0).
          - clicked=0            => aucun clic => rien parti => clear (republiera)
          - clicked=1 + id       => post confirme cree => commit_publication
          - clicked=1 + id NULL  => AMBIGU => skip conservateur (marque publie) +
                                     log CRITICAL (zombie potentiel non rotable)

        Chaque orpheline est traitee independamment (try/except) : une orpheline
        pourrie ou un active_batch absent ne doivent JAMAIS empecher le boot.
        """
        counts = {"republish": 0, "confirmed": 0, "ambiguous_zombie": 0, "errors": 0}
        batch = self.get_active_batch()
        for o in self.list_orphan_in_flight():
            if run_id_filter is not None and o["run_id"] != run_id_filter:
                continue
            try:
                same_cycle = (
                    batch is not None
                    and o["batch_name"] == batch.name
                    and o["cycle_number"] == batch.current_cycle
                )
                if not o["clicked"]:
                    self.clear_publish_in_flight(
                        o["run_id"], o["batch_name"], o["cycle_number"], o["media_filename"]
                    )
                    counts["republish"] += 1
                    log.info(
                        "reconcile_orphan_republish",
                        media=o["media_filename"], run_id=o["run_id"],
                        rationale="clicked=0 => aucun POST parti",
                    )
                elif o["fansly_post_id"]:
                    self.commit_publication(
                        run_id=o["run_id"], batch_name=o["batch_name"],
                        cycle_number=o["cycle_number"], media_filename=o["media_filename"],
                        caption=o.get("caption_used"), fansly_post_id=o["fansly_post_id"],
                        add_to_current_cycle=same_cycle,
                    )
                    counts["confirmed"] += 1
                    log.info(
                        "reconcile_orphan_confirmed",
                        media=o["media_filename"], fansly_post_id=o["fansly_post_id"],
                    )
                else:
                    # clicked=1 sans id : le post EXISTE peut-etre. On ne republie
                    # PAS (doublon interdit). On marque publie + alerte CRITICAL.
                    self.commit_publication(
                        run_id=o["run_id"], batch_name=o["batch_name"],
                        cycle_number=o["cycle_number"], media_filename=o["media_filename"],
                        caption=o.get("caption_used"), fansly_post_id=None,
                        add_to_current_cycle=same_cycle,
                    )
                    counts["ambiguous_zombie"] += 1
                    log.critical(
                        "reconcile_orphan_ambiguous_zombie",
                        media=o["media_filename"], run_id=o["run_id"],
                        hint="POST peut-etre parti sans id capture => post potentiellement "
                             "cree mais NON rotable (id inconnu) => verifier le compte et "
                             "supprimer manuellement si doublon",
                    )
            except Exception as e:  # noqa: BLE001 — une orpheline ne bloque pas le boot
                counts["errors"] += 1
                log.error(
                    "reconcile_orphan_failed",
                    media=o.get("media_filename"), error=str(e),
                )
        if any(counts.values()):
            log.warning("reconcile_publish_in_flight_summary", **counts)
        return counts

    def set_batch_playlist_order(self, playlist: list[str]) -> None:
        """Ecrit l'ordre de playlist du batch actif.

        Utilise :
          - au 1er cycle : shuffle initial des medias du dossier
          - au fil de l'eau : append des nouveaux fichiers detectes (A1)

        Ne DOIT PAS etre appelee entre les cycles pour "reset" — la playlist
        est PAR CONSTRUCTION preservee entre cycles.
        """
        with self._conn:
            self._conn.execute(
                "UPDATE active_batch SET playlist_order = ? WHERE id = 1",
                (json.dumps(playlist),),
            )

    def stop_batch(self) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM active_batch WHERE id = 1")

    def add_to_batch_published(self, filename: str) -> None:
        """Ajoute un fichier aux publies du cycle courant + incremente le total.

        IDEMPOTENT : si le fichier est deja dans published_in_cycle, no-op TOTAL
        (y compris total_published, qui ne doit PAS etre double-compte lors
        d'une reapplication de la reconciliation — cf. CP4)."""
        batch = self.get_active_batch()
        if not batch:
            return
        published = batch.published_in_cycle
        if filename in published:
            return  # deja compte : no-op strict (pas de double-comptage)
        published.append(filename)
        with self._conn:
            self._conn.execute(
                """
                UPDATE active_batch
                SET published_in_cycle = ?,
                    total_published = total_published + 1
                WHERE id = 1
                """,
                (json.dumps(published),),
            )

    def advance_batch_cycle(self, new_cycle: int) -> None:
        """Passe au cycle suivant : vide published_in_cycle et met a jour current_cycle."""
        with self._conn:
            self._conn.execute(
                """
                UPDATE active_batch
                SET current_cycle = ?, published_in_cycle = '[]'
                WHERE id = 1
                """,
                (new_cycle,),
            )

    # ----- job_queue -----

    def enqueue_job(self, type_: str, config: dict) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO job_queue (type, config, status, created_at)
                VALUES (?, ?, 'queued', ?)
                """,
                (type_, json.dumps(config), now),
            )
            return int(cur.lastrowid or 0)

    def get_next_queued_job(self) -> Optional[Job]:
        cur = self._conn.execute(
            "SELECT * FROM job_queue WHERE status = 'queued' "
            "ORDER BY id ASC LIMIT 1"
        )
        row = cur.fetchone()
        return _row_to_job(row) if row else None

    def get_job(self, job_id: int) -> Optional[Job]:
        cur = self._conn.execute("SELECT * FROM job_queue WHERE id = ?", (job_id,))
        row = cur.fetchone()
        return _row_to_job(row) if row else None

    def list_jobs(
        self, limit: int = 50, statuses: Optional[list[str]] = None
    ) -> list[Job]:
        if statuses:
            placeholders = ",".join("?" * len(statuses))
            cur = self._conn.execute(
                f"SELECT * FROM job_queue WHERE status IN ({placeholders}) "
                f"ORDER BY id DESC LIMIT ?",
                (*statuses, limit),
            )
        else:
            cur = self._conn.execute(
                "SELECT * FROM job_queue ORDER BY id DESC LIMIT ?", (limit,)
            )
        return [_row_to_job(r) for r in cur.fetchall()]

    def mark_job_running(self, job_id: int, log_path: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn:
            self._conn.execute(
                "UPDATE job_queue SET status = 'running', started_at = ?, log_path = ? "
                "WHERE id = ? AND status = 'queued'",
                (now, log_path, job_id),
            )

    def mark_job_done(self, job_id: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn:
            self._conn.execute(
                "UPDATE job_queue SET status = 'done', finished_at = ? WHERE id = ?",
                (now, job_id),
            )

    def mark_job_failed(self, job_id: int, error: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn:
            self._conn.execute(
                "UPDATE job_queue SET status = 'failed', finished_at = ?, error = ? "
                "WHERE id = ?",
                (now, error[:2000], job_id),
            )

    def mark_job_cancelled(self, job_id: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn:
            self._conn.execute(
                "UPDATE job_queue SET status = 'cancelled', finished_at = ? "
                "WHERE id = ?",
                (now, job_id),
            )

    def request_job_cancel(self, job_id: int) -> str:
        """Annule un job. Si `queued` → cancelled direct. Si `running` → cancelling
        (le worker verra le flag et stoppera proprement).
        Retourne le nouveau status."""
        job = self.get_job(job_id)
        if job is None:
            return "not_found"
        if job.status == "queued":
            self.mark_job_cancelled(job_id)
            return "cancelled"
        if job.status == "running":
            with self._conn:
                self._conn.execute(
                    "UPDATE job_queue SET status = 'cancelling' WHERE id = ?",
                    (job_id,),
                )
            return "cancelling"
        return job.status

    def is_cancellation_requested(self, job_id: int) -> bool:
        cur = self._conn.execute(
            "SELECT status FROM job_queue WHERE id = ?", (job_id,)
        )
        row = cur.fetchone()
        return bool(row) and row["status"] == "cancelling"

    def recover_stale_jobs(self) -> int:
        """Marque comme `failed` les jobs `running`/`cancelling` orphelins
        (worker tue brutalement). A appeler au demarrage du worker.
        Retourne le nombre de jobs nettoyes."""
        now = datetime.now(timezone.utc).isoformat()
        with self._conn:
            cur = self._conn.execute(
                "UPDATE job_queue SET status = 'failed', finished_at = ?, "
                "error = COALESCE(error, '') || ' [recovered_from_crash]' "
                "WHERE status IN ('running', 'cancelling')",
                (now,),
            )
            return cur.rowcount

    def requeue_job(self, job_id: int) -> bool:
        """Remet un job 'running' en 'queued' pour qu'il soit REPRIS (au lieu
        de le marquer failed). Garde WHERE status='running' : no-op si le job a
        change d'etat entre-temps (ex: annulation concurrente). Retourne True si
        effectivement re-queue."""
        with self._conn:
            cur = self._conn.execute(
                "UPDATE job_queue SET status = 'queued', started_at = NULL, "
                "finished_at = NULL WHERE id = ? AND status = 'running'",
                (job_id,),
            )
            return cur.rowcount > 0

    def reconcile_stale_jobs(self) -> dict:
        """Remplace recover_stale_jobs pour la reprise-apres-crash. Au boot :
          - publish 'running'    => REQUEUE (le lot sera repris, position intacte)
          - publish 'cancelling' => cancelled (l'utilisateur avait demande l'arret)
          - purge   running/canc => failed [recovered_from_crash] (pas de resume fin)
        Retourne un dict de comptage.
        """
        counts = {"publish_requeued": 0, "publish_cancelled": 0, "purge_failed": 0}
        now = datetime.now(timezone.utc).isoformat()
        rows = self._conn.execute(
            "SELECT id, type, status FROM job_queue "
            "WHERE status IN ('running', 'cancelling')"
        ).fetchall()
        for r in rows:
            jid, jtype, jstatus = r["id"], r["type"], r["status"]
            with self._conn:
                if jtype == "publish" and jstatus == "running":
                    self._conn.execute(
                        "UPDATE job_queue SET status = 'queued', started_at = NULL, "
                        "finished_at = NULL WHERE id = ?", (jid,),
                    )
                    counts["publish_requeued"] += 1
                elif jtype == "publish" and jstatus == "cancelling":
                    self._conn.execute(
                        "UPDATE job_queue SET status = 'cancelled', finished_at = ? "
                        "WHERE id = ?", (now, jid),
                    )
                    counts["publish_cancelled"] += 1
                else:  # purge (ou tout autre) : pas de reprise fine
                    self._conn.execute(
                        "UPDATE job_queue SET status = 'failed', finished_at = ?, "
                        "error = COALESCE(error, '') || ' [recovered_from_crash]' "
                        "WHERE id = ?", (now, jid),
                    )
                    counts["purge_failed"] += 1
        if any(counts.values()):
            log.warning("reconcile_stale_jobs_summary", **counts)
        return counts

    def sweep_stale_cancelling(self) -> int:
        """Convertit tout job encore 'cancelling' en 'cancelled' (aucun worker
        ne le finira). Ferme la fenetre TOCTOU boot ou un cancel UI concurrent
        laisse un job coince en 'cancelling'. Retourne le nombre nettoye."""
        now = datetime.now(timezone.utc).isoformat()
        with self._conn:
            cur = self._conn.execute(
                "UPDATE job_queue SET status = 'cancelled', finished_at = ? "
                "WHERE status = 'cancelling'", (now,),
            )
            return cur.rowcount

    def cleanup_orphan_active_batch(self) -> Optional[str]:
        """Supprime un active_batch residuel d un crash precedent.

        Un active_batch ne devrait jamais exister sans un job publish
        running ou queued. Apres recover_stale_jobs (qui marque les jobs
        running comme failed), un active_batch persistant est forcement
        orphelin. Cette routine le supprime et retourne son nom pour
        loguer ce qui a ete nettoye. A appeler APRES recover_stale_jobs.
        """
        cur = self._conn.execute(
            "SELECT name FROM active_batch WHERE id = 1"
        )
        row = cur.fetchone()
        if row is None:
            return None
        # Y a-t-il un job publish actif (queued ou running) ?
        cur = self._conn.execute(
            "SELECT 1 FROM job_queue "
            "WHERE type = 'publish' AND status IN ('queued', 'running')"
        )
        if cur.fetchone() is not None:
            # Un job va prendre le relai — on laisse active_batch tel quel.
            return None
        # active_batch orphelin : on le supprime
        name = row["name"]
        with self._conn:
            self._conn.execute("DELETE FROM active_batch WHERE id = 1")
        return name


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        type=row["type"],
        config=json.loads(row["config"]),
        status=row["status"],
        created_at=datetime.fromisoformat(row["created_at"]),
        started_at=(
            datetime.fromisoformat(row["started_at"]) if row["started_at"] else None
        ),
        finished_at=(
            datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None
        ),
        log_path=row["log_path"],
        error=row["error"],
    )


def _row_to_post(row: sqlite3.Row) -> PostRecord:
    return PostRecord(
        post_id=row["post_id"],
        first_seen_at=datetime.fromisoformat(row["first_seen_at"]),
        post_created_at=(
            datetime.fromisoformat(row["post_created_at"]) if row["post_created_at"] else None
        ),
        caption_excerpt=row["caption_excerpt"],
        last_examined_at=datetime.fromisoformat(row["last_examined_at"]),
        decision=row["decision"],
        decision_reason=row["decision_reason"],
    )
