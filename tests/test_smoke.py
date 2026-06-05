"""Smoke tests : couvrent les invariants critiques apres refonte.

Pas de dependance externe (pytest non installe) — on utilise unittest pour
rester executable avec `python -m unittest discover`. Pas de tests Playwright
(impossible sans browser + compte Fansly).

Couvre :
  - infra.names : validate_batch_name (regex + defense en profondeur)
  - infra.state : schema migration, record_media_published (idempotence,
    cle composite), publish_in_flight (lifecycle)
  - worker : acquire/release lock, resolve_orphan_in_flight
  - _lib : validation centralisee dans save_uploaded_file / delete_batch /
    write_caption_batch
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

# Avant d'importer config : variables d'env minimales requises par Pydantic
os.environ.setdefault("FANSLY_USERNAME", "test")
os.environ.setdefault("FANSLY_PASSWORD", "test")

# Permet de lancer les tests depuis n importe ou : on ajoute src/ au path.
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))


# =================== infra.names ===================

class TestBatchNameValidation(unittest.TestCase):
    def test_valid_names_accepted(self):
        from fansly_bot.infra.names import is_valid_batch_name, validate_batch_name

        for name in ["Test", "test_2026", "ete-2026", "ABC123", "a" * 64, "x"]:
            self.assertTrue(is_valid_batch_name(name), name)
            self.assertEqual(validate_batch_name(name), name.strip())

    def test_invalid_names_rejected(self):
        from fansly_bot.infra.names import (
            InvalidBatchNameError,
            is_valid_batch_name,
            validate_batch_name,
        )

        for name in [
            "", " ", "..", "../etc", "a/b", "a\\b", "_under",
            "-tiret", ".hidden", "a" * 65, "name with space",
            "name@home", "foo\x00bar", "cap..json",
        ]:
            self.assertFalse(is_valid_batch_name(name), name)
            with self.assertRaises(InvalidBatchNameError):
                validate_batch_name(name)

    def test_non_strings_rejected(self):
        from fansly_bot.infra.names import InvalidBatchNameError, validate_batch_name

        for bad in [None, 123, ["list"], {"k": "v"}]:
            with self.assertRaises(InvalidBatchNameError):
                validate_batch_name(bad)


# =================== infra.state ===================

class _TmpStateMixin:
    """Cree un StateStore isole pour chaque test."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        from fansly_bot.config import load_settings

        self.settings = load_settings()
        self.settings.paths.state_db = self.tmp / "state.db"
        self.settings.paths.state_db.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        if hasattr(self, "store"):
            self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestSchemaMigration(_TmpStateMixin, unittest.TestCase):
    def test_legacy_table_renamed(self):
        # Pre-cree l ancienne structure
        conn = sqlite3.connect(str(self.settings.paths.state_db))
        conn.execute(
            "CREATE TABLE media_published ("
            "  media_filename TEXT PRIMARY KEY,"
            "  published_at TEXT NOT NULL,"
            "  caption_used TEXT,"
            "  fansly_post_id TEXT,"
            "  batch_name TEXT,"
            "  cycle_number INTEGER"
            ")"
        )
        conn.execute(
            "INSERT INTO media_published VALUES (?, ?, ?, ?, ?, ?)",
            ("foo::B::cycle1", "2026-01-01T00:00:00+00:00", "c", None, "B", 1),
        )
        conn.commit()
        conn.close()

        from fansly_bot.infra.state import StateStore

        self.store = StateStore(self.settings)

        # Nouvelle table cree avec les bonnes colonnes
        conn = sqlite3.connect(str(self.settings.paths.state_db))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(media_published)")}
        self.assertIn("run_id", cols)
        self.assertIn("id", cols)

        # Ancienne table preservee en legacy_v1
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='media_published_legacy_v1'"
        )
        self.assertIsNotNone(cur.fetchone())

        # Donnees historiques conservees
        n = conn.execute(
            "SELECT COUNT(*) FROM media_published_legacy_v1"
        ).fetchone()[0]
        self.assertEqual(n, 1)
        conn.close()

    def test_fresh_install_no_legacy_table(self):
        from fansly_bot.infra.state import StateStore

        self.store = StateStore(self.settings)
        conn = sqlite3.connect(str(self.settings.paths.state_db))
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='media_published_legacy_v1'"
        )
        # Sur fresh install, pas de table legacy
        self.assertIsNone(cur.fetchone())
        conn.close()


class TestRecordMediaPublished(_TmpStateMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        from fansly_bot.infra.state import StateStore

        self.store = StateStore(self.settings)

    def test_idempotent_on_same_run(self):
        # Deux record identiques sur le meme (run, batch, cycle, media)
        self.store.record_media_published(
            "m1.jpg", "c1", batch_name="B", cycle_number=1, run_id=42
        )
        self.store.record_media_published(
            "m1.jpg", "c1", batch_name="B", cycle_number=1, run_id=42
        )
        conn = sqlite3.connect(str(self.settings.paths.state_db))
        n = conn.execute(
            "SELECT COUNT(*) FROM media_published WHERE run_id = 42"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(n, 1)  # idempotence : 1 seule ligne

    def test_different_runs_create_separate_rows(self):
        self.store.record_media_published(
            "m1.jpg", "c1", batch_name="B", cycle_number=1, run_id=42
        )
        self.store.record_media_published(
            "m1.jpg", "c1", batch_name="B", cycle_number=1, run_id=43
        )
        conn = sqlite3.connect(str(self.settings.paths.state_db))
        n = conn.execute(
            "SELECT COUNT(*) FROM media_published"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(n, 2)


class TestPublishInFlight(_TmpStateMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        from fansly_bot.infra.state import StateStore

        self.store = StateStore(self.settings)

    def test_mark_has_clear_lifecycle(self):
        # Pas de marker au depart
        self.assertFalse(
            self.store.has_publish_in_flight(1, "B", 1, "m.jpg")
        )

        # Mark, then has
        self.store.mark_publish_in_flight(1, "B", 1, "m.jpg", "c")
        self.assertTrue(
            self.store.has_publish_in_flight(1, "B", 1, "m.jpg")
        )

        # Re-mark idempotent
        self.store.mark_publish_in_flight(1, "B", 1, "m.jpg", "c")
        orphans = self.store.list_orphan_in_flight()
        self.assertEqual(len(orphans), 1)

        # Clear
        self.store.clear_publish_in_flight(1, "B", 1, "m.jpg")
        self.assertFalse(
            self.store.has_publish_in_flight(1, "B", 1, "m.jpg")
        )
        self.assertEqual(self.store.list_orphan_in_flight(), [])

    def test_list_orphan_returns_dicts_with_all_fields(self):
        self.store.mark_publish_in_flight(7, "Batch", 2, "m.jpg", "caption")
        orphans = self.store.list_orphan_in_flight()
        self.assertEqual(len(orphans), 1)
        o = orphans[0]
        for key in (
            "run_id", "batch_name", "cycle_number",
            "media_filename", "caption_used", "started_at",
        ):
            self.assertIn(key, o)
        self.assertEqual(o["run_id"], 7)
        self.assertEqual(o["batch_name"], "Batch")
        self.assertEqual(o["media_filename"], "m.jpg")


# =================== worker ===================

class TestWorkerLock(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        from fansly_bot.config import load_settings

        self.settings = load_settings()
        self.settings.paths.state_db = self.tmp / "state.db"
        self.settings.paths.state_db.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        from fansly_bot.worker import release_worker_lock

        release_worker_lock()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_acquire_then_release(self):
        from fansly_bot.worker import (
            acquire_worker_lock,
            release_worker_lock,
        )
        import fansly_bot.worker as w

        acquire_worker_lock(self.settings)
        self.assertIsNotNone(w._LOCK_FD)
        release_worker_lock()
        self.assertIsNone(w._LOCK_FD)

    def test_second_acquire_from_another_fd_fails(self):
        import fcntl
        from fansly_bot.worker import (
            acquire_worker_lock,
            lock_file_path,
            release_worker_lock,
        )

        acquire_worker_lock(self.settings)
        try:
            fd2 = os.open(str(lock_file_path(self.settings)), os.O_RDWR)
            try:
                with self.assertRaises((BlockingIOError, OSError)):
                    fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd2)
        finally:
            release_worker_lock()


class TestCleanupOrphanActiveBatch(_TmpStateMixin, unittest.TestCase):
    def test_no_orphan_when_active_batch_absent(self):
        from fansly_bot.infra.state import StateStore

        self.store = StateStore(self.settings)
        self.assertIsNone(self.store.cleanup_orphan_active_batch())

    def test_keeps_active_batch_when_publish_job_queued(self):
        from fansly_bot.infra.state import StateStore

        self.store = StateStore(self.settings)
        self.store.start_batch("MonLot", max_cycles=2)
        # Un job publish queued sur ce lot -> on garde active_batch
        self.store.enqueue_job("publish", {"batch_name": "MonLot"})
        result = self.store.cleanup_orphan_active_batch()
        self.assertIsNone(result)
        self.assertIsNotNone(self.store.get_active_batch())

    def test_cleans_when_no_active_job(self):
        from fansly_bot.infra.state import StateStore

        self.store = StateStore(self.settings)
        self.store.start_batch("Orphelin", max_cycles=2)
        # Job publish ancien deja failed -> active_batch est residuel
        jid = self.store.enqueue_job("publish", {"batch_name": "Orphelin"})
        self.store.mark_job_running(jid, "/tmp/log")
        self.store.mark_job_failed(jid, "crash")
        result = self.store.cleanup_orphan_active_batch()
        self.assertEqual(result, "Orphelin")
        self.assertIsNone(self.store.get_active_batch())


class TestResolveOrphanInFlight(_TmpStateMixin, unittest.TestCase):
    def test_resolves_and_clears(self):
        from fansly_bot.infra.state import StateStore
        from fansly_bot.worker import Worker

        # Cree 2 orphelins en BDD
        store = StateStore(self.settings)
        store.mark_publish_in_flight(50, "B", 1, "m1.jpg", "c1")
        store.mark_publish_in_flight(50, "B", 1, "m2.jpg", "c2")
        store.close()

        # Le worker doit les resoudre au demarrage
        worker = Worker(self.settings)
        worker._resolve_orphan_in_flight()

        # Apres : zero orphan, 2 lignes dans media_published
        self.assertEqual(worker._state.list_orphan_in_flight(), [])
        conn = sqlite3.connect(str(self.settings.paths.state_db))
        n = conn.execute(
            "SELECT COUNT(*) FROM media_published WHERE run_id = 50"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(n, 2)
        worker._state.close()


# =================== _lib (dashboard) — defense en profondeur ===================

class TestLibValidation(unittest.TestCase):
    """Verifie que les helpers _lib refusent les noms / fichiers suspects."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        from fansly_bot.config import load_settings

        self.settings = load_settings()
        self.settings.paths.media_folder = self.tmp / "Medias"
        self.settings.paths.caption_folder = self.tmp / "Captions"
        self.settings.paths.media_folder.mkdir(parents=True)
        self.settings.paths.caption_folder.mkdir(parents=True)
        # On bypass le cache Streamlit en remplacant get_settings.
        import fansly_dashboard._lib as L

        L.get_settings.clear()  # type: ignore[attr-defined]
        self._orig_get_settings = L.get_settings
        L.get_settings = lambda: self.settings
        self._L = L

    def tearDown(self):
        # Restaure le get_settings d origine
        self._L.get_settings = self._orig_get_settings
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_delete_batch_rejects_traversal(self):
        from fansly_bot.infra.names import InvalidBatchNameError

        with self.assertRaises(InvalidBatchNameError):
            self._L.delete_batch("..")
        with self.assertRaises(InvalidBatchNameError):
            self._L.delete_batch("/etc/passwd")

    def test_save_uploaded_file_rejects_traversal_filename(self):
        with self.assertRaises(ValueError):
            self._L.save_uploaded_file("validlot", "../evil.jpg", b"d")
        with self.assertRaises(ValueError):
            self._L.save_uploaded_file("validlot", "a/b.jpg", b"d")
        with self.assertRaises(ValueError):
            self._L.save_uploaded_file("validlot", ".env", b"d")

    def test_caption_batch_path_rejects_invalid(self):
        from fansly_bot.infra.names import InvalidBatchNameError

        with self.assertRaises(InvalidBatchNameError):
            self._L._caption_batch_path("..")

    def test_write_caption_batch_rejects_invalid(self):
        from fansly_bot.infra.names import InvalidBatchNameError

        with self.assertRaises(InvalidBatchNameError):
            self._L.write_caption_batch("../escape", ["c"])

    def test_happy_path_create_then_save(self):
        self._L.create_batch("MonLot")
        p = self._L.save_uploaded_file("MonLot", "photo.jpg", b"img")
        self.assertEqual(p.read_bytes(), b"img")

    def test_caption_batch_roundtrip(self):
        self._L.write_caption_batch(
            "ete_2026", ["Hello", "World"], description="lot ete"
        )
        d = self._L.read_caption_batch("ete_2026")
        self.assertEqual(d["captions"], ["Hello", "World"])
        self.assertEqual(d["description"], "lot ete")


# =================== entry point ===================

if __name__ == "__main__":
    unittest.main()
