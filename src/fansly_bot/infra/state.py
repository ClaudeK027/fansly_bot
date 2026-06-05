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
    total_published    INTEGER NOT NULL DEFAULT 0
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

    def list_orphan_in_flight(self) -> list[dict]:
        """Liste les publications 'en cours' restees orphelines (crash worker).

        Retourne une liste de dicts avec les 6 colonnes — le worker decidera
        quoi faire (typiquement : marquer published puis nettoyer).
        """
        cur = self._conn.execute(
            "SELECT run_id, batch_name, cycle_number, media_filename, "
            "caption_used, started_at FROM publish_in_flight"
        )
        return [dict(r) for r in cur.fetchall()]

    # ----- active_batch -----

    def get_active_batch(self) -> Optional[ActiveBatch]:
        cur = self._conn.execute("SELECT * FROM active_batch WHERE id = 1")
        row = cur.fetchone()
        if not row:
            return None
        return ActiveBatch(
            name=row["name"],
            started_at=datetime.fromisoformat(row["started_at"]),
            max_cycles=row["max_cycles"],
            current_cycle=row["current_cycle"],
            published_in_cycle=json.loads(row["published_in_cycle"]),
            total_published=row["total_published"],
        )

    def start_batch(self, name: str, max_cycles: int = 0) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO active_batch
                    (id, name, started_at, max_cycles, current_cycle,
                     published_in_cycle, total_published)
                VALUES (1, ?, ?, ?, 1, '[]', 0)
                """,
                (name, now, max_cycles),
            )

    def stop_batch(self) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM active_batch WHERE id = 1")

    def add_to_batch_published(self, filename: str) -> None:
        """Ajoute un fichier a la liste des publies du cycle courant, incremente le total."""
        batch = self.get_active_batch()
        if not batch:
            return
        published = batch.published_in_cycle
        if filename not in published:
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
