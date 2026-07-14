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
import random
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


# =================== Phase A : capture fansly_post_id ===================


class _FakeResponse:
    """Mock minimal d'une Response Playwright pour tester
    UploaderService._capture_fansly_post_id. Si `slow_ms` est > 0, le
    `text()` simule une latence reseau (utile pour tester les races)."""

    def __init__(self, body_text: str, slow_ms: int = 0) -> None:
        self._body = body_text
        self._slow_ms = slow_ms

    async def text(self) -> str:
        if self._slow_ms > 0:
            import asyncio as _aio

            await _aio.sleep(self._slow_ms / 1000.0)
        return self._body


def _make_uploader_stub():
    """Construit un UploaderService minimal sans dependances reseau,
    suffisant pour appeler _capture_fansly_post_id() et _mark_published()."""
    from fansly_bot.services.uploader import UploaderService

    stub = UploaderService.__new__(UploaderService)  # bypass __init__
    stub._run_id = 99
    stub._capture_miss_streak = 0
    return stub


class TestCaptureFanslyPostId(unittest.IsolatedAsyncioTestCase):
    """Verifie que _capture_fansly_post_id extrait correctement l'ID
    sous les differents formats de reponse JSON de l'API Fansly,
    et qu'il respecte le premier-gagne / les redacts PII."""

    async def test_format_object_response(self):
        stub = _make_uploader_stub()
        captured: dict = {"value": None}
        body = '{"success": true, "response": {"id": "789012345", "x": 1}}'
        await stub._capture_fansly_post_id(_FakeResponse(body), captured)
        self.assertEqual(captured["value"], "789012345")

    async def test_format_array_response(self):
        stub = _make_uploader_stub()
        captured: dict = {"value": None}
        body = '{"success": true, "response": [{"id": "111", "y": 2}]}'
        await stub._capture_fansly_post_id(_FakeResponse(body), captured)
        self.assertEqual(captured["value"], "111")

    async def test_format_root_id(self):
        stub = _make_uploader_stub()
        captured: dict = {"value": None}
        body = '{"id": "42"}'
        await stub._capture_fansly_post_id(_FakeResponse(body), captured)
        self.assertEqual(captured["value"], "42")

    async def test_numeric_id_coerced_to_string(self):
        stub = _make_uploader_stub()
        captured: dict = {"value": None}
        body = '{"response": {"id": 555}}'
        await stub._capture_fansly_post_id(_FakeResponse(body), captured)
        self.assertEqual(captured["value"], "555")

    async def test_fallback_when_response_block_has_no_id(self):
        # Cas adversarial : response est un dict sans id, mais id est a
        # la racine. Le fallback doit s'appliquer (non exclusif).
        stub = _make_uploader_stub()
        captured: dict = {"value": None}
        body = '{"response": {"meta": "x"}, "id": "root-123"}'
        await stub._capture_fansly_post_id(_FakeResponse(body), captured)
        self.assertEqual(captured["value"], "root-123")

    async def test_toplevel_list_response(self):
        stub = _make_uploader_stub()
        captured: dict = {"value": None}
        body = '[{"id": "list-7"}]'
        await stub._capture_fansly_post_id(_FakeResponse(body), captured)
        self.assertEqual(captured["value"], "list-7")

    async def test_first_wins_does_not_overwrite(self):
        # Si captured["value"] est deja set, une 2e capture (autre 2xx
        # ou tentative tenacity ulterieure) ne doit PAS ecraser.
        stub = _make_uploader_stub()
        captured: dict = {"value": "first-id"}
        body = '{"response": {"id": "second-id"}}'
        await stub._capture_fansly_post_id(_FakeResponse(body), captured)
        self.assertEqual(captured["value"], "first-id")

    async def test_first_wins_under_concurrent_tasks(self):
        # Cas adversarial reel : 2 tasks _capture_fansly_post_id en
        # parallele (par ex. 2 reponses 2xx /api/v1/post pendant le
        # meme upload). Les deux peuvent passer la garde initiale puis
        # yield sur response.text(). Sans re-check ATOMIQUE avant
        # l'ecriture, le dernier-a-finir ecrase le premier-a-ecrire.
        # Verifie que c'est bien la task plus rapide (slow_ms=2) qui
        # gagne, pas celle qui finit en dernier.
        import asyncio as _aio

        stub = _make_uploader_stub()
        captured: dict = {"value": None}
        fast = _FakeResponse('{"response": {"id": "fast"}}', slow_ms=2)
        slow = _FakeResponse('{"response": {"id": "slow"}}', slow_ms=40)
        await _aio.gather(
            stub._capture_fansly_post_id(fast, captured),
            stub._capture_fansly_post_id(slow, captured),
        )
        self.assertEqual(captured["value"], "fast")

    async def test_buffer_persists_across_attempts(self):
        # Simule le retry tenacity : tentative 1 capture l'ID, tentative
        # 2 (re-entree dans _do_upload) ne doit PAS ecraser car captured
        # est cree UNE SEULE FOIS dans publish_next, hors de la boucle.
        # Premier-gagne strict valide cross-attempt.
        stub = _make_uploader_stub()
        captured: dict = {"value": None}
        await stub._capture_fansly_post_id(
            _FakeResponse('{"response": {"id": "attempt1-id"}}'), captured,
        )
        self.assertEqual(captured["value"], "attempt1-id")
        # Tentative 2 : si Fansly avait re-emis un 2xx avec un autre ID
        # (par ex. doublon cote serveur), il ne doit PAS ecraser le 1er.
        await stub._capture_fansly_post_id(
            _FakeResponse('{"response": {"id": "attempt2-id"}}'), captured,
        )
        self.assertEqual(captured["value"], "attempt1-id")

    async def test_missing_id_leaves_buffer_none(self):
        stub = _make_uploader_stub()
        captured: dict = {"value": None}
        body = '{"success": false, "error": "rejected"}'
        await stub._capture_fansly_post_id(_FakeResponse(body), captured)
        self.assertIsNone(captured["value"])

    async def test_miss_streak_increments_then_resets_on_success(self):
        stub = _make_uploader_stub()
        # 3 echecs consecutifs → streak = 3 (et log.error en interne)
        for _ in range(3):
            captured: dict = {"value": None}
            await stub._capture_fansly_post_id(
                _FakeResponse('{"unknown_shape": true}'), captured,
            )
        self.assertEqual(stub._capture_miss_streak, 3)
        # Une capture reussie reset le streak a 0
        captured = {"value": None}
        await stub._capture_fansly_post_id(
            _FakeResponse('{"response": {"id": "ok"}}'), captured,
        )
        self.assertEqual(stub._capture_miss_streak, 0)

    async def test_invalid_json_does_not_raise(self):
        stub = _make_uploader_stub()
        captured: dict = {"value": None}
        await stub._capture_fansly_post_id(_FakeResponse("not json {{{"), captured)
        self.assertIsNone(captured["value"])

    async def test_empty_body_does_not_raise(self):
        stub = _make_uploader_stub()
        captured: dict = {"value": None}
        await stub._capture_fansly_post_id(_FakeResponse(""), captured)
        self.assertIsNone(captured["value"])


class TestIsPostCreationUrl(unittest.TestCase):
    """Verifie que _is_post_creation_url filtre correctement les URLs :
    seul POST /api/v1/post EXACT doit declencher la capture, pas les
    sous-paths ni les prefixes accidentels."""

    def test_accepts_exact_path(self):
        from fansly_bot.services.uploader import UploaderService

        for url in [
            "https://apiv3.fansly.com/api/v1/post",
            "https://apiv3.fansly.com/api/v1/post/",
            "https://apiv3.fansly.com/api/v1/post?ngsw-bypass=true",
            "https://apiv3.fansly.com/api/v1/post?foo=bar&baz=qux",
        ]:
            self.assertTrue(
                UploaderService._is_post_creation_url(url), msg=url,
            )

    def test_rejects_action_subpaths(self):
        from fansly_bot.services.uploader import UploaderService

        for url in [
            "https://apiv3.fansly.com/api/v1/post/12345",
            "https://apiv3.fansly.com/api/v1/post/12345/like",
            "https://apiv3.fansly.com/api/v1/post/12345/delete",
            "https://apiv3.fansly.com/api/v1/post/12345/pin",
        ]:
            self.assertFalse(
                UploaderService._is_post_creation_url(url), msg=url,
            )

    def test_rejects_accidental_prefixes(self):
        from fansly_bot.services.uploader import UploaderService

        for url in [
            "https://apiv3.fansly.com/api/v1/repost",
            "https://apiv3.fansly.com/api/v2/api/v1/post",
            "https://apiv3.fansly.com/api/v1/post-comment",
            "https://apiv3.fansly.com/post",
        ]:
            self.assertFalse(
                UploaderService._is_post_creation_url(url), msg=url,
            )

    def test_handles_malformed_url(self):
        from fansly_bot.services.uploader import UploaderService

        # Pas censee throw, juste retourner False sur des entrees bizarres
        self.assertFalse(UploaderService._is_post_creation_url(""))
        self.assertFalse(UploaderService._is_post_creation_url("not-a-url"))


class TestMarkPublishedPersistsFanslyId(_TmpStateMixin, unittest.TestCase):
    """Verifie que _mark_published propage bien le fansly_post_id (recu en
    parametre) a record_media_published, qui le persiste en BDD."""

    def setUp(self):
        super().setUp()
        from fansly_bot.infra.state import StateStore

        self.store = StateStore(self.settings)

    def _stub_with_state(self):
        from fansly_bot.services.uploader import UploaderService

        stub = UploaderService.__new__(UploaderService)
        stub._run_id = 77
        stub._capture_miss_streak = 0
        stub._state = self.store
        return stub

    def test_persists_id_when_captured(self):
        stub = self._stub_with_state()
        media = Path("IMG_0001.mp4")
        stub._mark_published(
            media, "caption #fyp", batch_name="B", cycle=2,
            fansly_post_id="abc123",
        )

        conn = sqlite3.connect(str(self.settings.paths.state_db))
        row = conn.execute(
            "SELECT fansly_post_id FROM media_published "
            "WHERE run_id=77 AND media_filename='IMG_0001.mp4'"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "abc123")

    def test_persists_null_when_not_captured(self):
        stub = self._stub_with_state()
        media = Path("IMG_0002.mp4")
        stub._mark_published(
            media, "caption #fyp", batch_name="B", cycle=2,
            fansly_post_id=None,
        )

        conn = sqlite3.connect(str(self.settings.paths.state_db))
        row = conn.execute(
            "SELECT fansly_post_id FROM media_published "
            "WHERE run_id=77 AND media_filename='IMG_0002.mp4'"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertIsNone(row[0])


# =================== Phase B : rotation per-media ===================


class TestGetFanslyPostIdForPreviousCycle(_TmpStateMixin, unittest.TestCase):
    """Verifie la requete SQL qui sert au CycleRotator pour identifier
    le post Fansly du meme media au cycle precedent."""

    def setUp(self):
        super().setUp()
        from fansly_bot.infra.state import StateStore

        self.store = StateStore(self.settings)

    def test_returns_id_from_previous_cycle(self):
        # Cycle 1 publie m.jpg avec fansly_post_id="abc"
        self.store.record_media_published(
            "m.jpg", "c1",
            batch_name="B", cycle_number=1, run_id=10,
            fansly_post_id="abc",
        )
        result = self.store.get_fansly_post_id_for_previous_cycle(
            run_id=10, batch_name="B", media_filename="m.jpg",
            current_cycle=2,
        )
        self.assertEqual(result, "abc")

    def test_returns_none_when_no_previous_cycle(self):
        # Pas d'historique
        result = self.store.get_fansly_post_id_for_previous_cycle(
            run_id=10, batch_name="B", media_filename="m.jpg",
            current_cycle=2,
        )
        self.assertIsNone(result)

    def test_returns_none_when_previous_cycle_has_null_id(self):
        # Cycle 1 publie m.jpg SANS fansly_post_id (capture ratee)
        self.store.record_media_published(
            "m.jpg", "c1",
            batch_name="B", cycle_number=1, run_id=10,
            fansly_post_id=None,
        )
        result = self.store.get_fansly_post_id_for_previous_cycle(
            run_id=10, batch_name="B", media_filename="m.jpg",
            current_cycle=2,
        )
        self.assertIsNone(result)

    def test_returns_most_recent_when_multiple_previous_cycles(self):
        # Cycles 1 et 2 publient m.jpg avec ids differents.
        # Pour current_cycle=3 on doit recuperer l'id du cycle 2.
        self.store.record_media_published(
            "m.jpg", "c1",
            batch_name="B", cycle_number=1, run_id=10,
            fansly_post_id="id-cycle-1",
        )
        self.store.record_media_published(
            "m.jpg", "c2",
            batch_name="B", cycle_number=2, run_id=10,
            fansly_post_id="id-cycle-2",
        )
        result = self.store.get_fansly_post_id_for_previous_cycle(
            run_id=10, batch_name="B", media_filename="m.jpg",
            current_cycle=3,
        )
        self.assertEqual(result, "id-cycle-2")

    def test_isolates_by_run_id(self):
        # Run 10 et Run 11 publient le meme m.jpg dans le meme batch.
        # Pour current_cycle=2/run=10 on doit ignorer le run 11.
        self.store.record_media_published(
            "m.jpg", "c1",
            batch_name="B", cycle_number=1, run_id=10,
            fansly_post_id="id-run-10",
        )
        self.store.record_media_published(
            "m.jpg", "c1",
            batch_name="B", cycle_number=1, run_id=11,
            fansly_post_id="id-run-11",
        )
        result = self.store.get_fansly_post_id_for_previous_cycle(
            run_id=10, batch_name="B", media_filename="m.jpg",
            current_cycle=2,
        )
        self.assertEqual(result, "id-run-10")

    def test_isolates_by_batch_name(self):
        self.store.record_media_published(
            "m.jpg", "c1",
            batch_name="A", cycle_number=1, run_id=10,
            fansly_post_id="id-batch-A",
        )
        self.store.record_media_published(
            "m.jpg", "c1",
            batch_name="B", cycle_number=1, run_id=10,
            fansly_post_id="id-batch-B",
        )
        result = self.store.get_fansly_post_id_for_previous_cycle(
            run_id=10, batch_name="B", media_filename="m.jpg",
            current_cycle=2,
        )
        self.assertEqual(result, "id-batch-B")

    def test_isolates_by_filename(self):
        # Autres medias publies, m.jpg jamais
        self.store.record_media_published(
            "other.jpg", "c1",
            batch_name="B", cycle_number=1, run_id=10,
            fansly_post_id="other-id",
        )
        result = self.store.get_fansly_post_id_for_previous_cycle(
            run_id=10, batch_name="B", media_filename="m.jpg",
            current_cycle=2,
        )
        self.assertIsNone(result)

    def test_run_id_none_raises_value_error(self):
        # Defense en profondeur Blocker 4 : run_id=None doit lever
        # car une recherche cross-run pourrait remonter un post d'un
        # autre job et causer une suppression accidentelle.
        with self.assertRaises(ValueError):
            self.store.get_fansly_post_id_for_previous_cycle(
                run_id=None,
                batch_name="B", media_filename="m.jpg", current_cycle=2,
            )

    def test_ignores_current_and_future_cycles(self):
        # Le cycle 2 a deja publie m.jpg (re-execution partielle ?). Pour
        # current_cycle=2 on doit chercher STRICTEMENT < 2, donc rien.
        self.store.record_media_published(
            "m.jpg", "c2",
            batch_name="B", cycle_number=2, run_id=10,
            fansly_post_id="id-cycle-2",
        )
        result = self.store.get_fansly_post_id_for_previous_cycle(
            run_id=10, batch_name="B", media_filename="m.jpg",
            current_cycle=2,
        )
        self.assertIsNone(result)


class _FakeStateForRotator:
    """StateStore stub minimal pour tester CycleRotator sans BDD reelle.
    Sert a verifier les branches : skip premier-cycle, no_previous_id, db_error."""

    def __init__(self, lookup_result=None, raise_on_lookup=False):
        self._lookup_result = lookup_result
        self._raise_on_lookup = raise_on_lookup

    def get_fansly_post_id_for_previous_cycle(self, **kwargs):
        if self._raise_on_lookup:
            raise RuntimeError("simulated db error")
        return self._lookup_result


class TestCycleRotator(unittest.IsolatedAsyncioTestCase):
    """Verifie la logique d'orchestration de CycleRotator pour les branches
    qui n'engagent pas Playwright (skip / lookup / db_error). Les branches
    qui touchent le DOM (deleted/not_found/guard_fyp) ne sont pas couvertes
    ici — elles requierent une vraie session Playwright."""

    def _make_rotator(self, state):
        from fansly_bot.services.cycle_rotator import CycleRotator

        rot = CycleRotator.__new__(CycleRotator)
        rot._state = state
        rot._settings = None
        rot._session = None
        rot._humanizer = None
        rot._auth = None
        rot._retries = None
        rot._purger = None  # pas utilise dans les branches first_cycle/no_id/db_err
        return rot

    async def test_first_cycle_skipped(self):
        rot = self._make_rotator(_FakeStateForRotator())
        # page=None passe ok car les branches first_cycle_skip
        # /no_previous_id /db_error ne touchent jamais la page.
        result = await rot.rotate_before_publish(
            page=None, run_id=1, batch_name="B",
            current_cycle=1, media_filename="m.jpg",
        )
        self.assertEqual(result["status"], "first_cycle_skip")
        self.assertEqual(result["media"], "m.jpg")

    async def test_no_previous_id_skipped(self):
        rot = self._make_rotator(_FakeStateForRotator(lookup_result=None))
        result = await rot.rotate_before_publish(
            page=None, run_id=1, batch_name="B",
            current_cycle=3, media_filename="m.jpg",
        )
        self.assertEqual(result["status"], "no_previous_id")

    async def test_db_error_does_not_raise(self):
        rot = self._make_rotator(_FakeStateForRotator(raise_on_lookup=True))
        result = await rot.rotate_before_publish(
            page=None, run_id=1, batch_name="B",
            current_cycle=3, media_filename="m.jpg",
        )
        self.assertEqual(result["status"], "db_error")
        self.assertIn("simulated db error", result["error"])


class TestPurgerHrefStrictMatch(unittest.TestCase):
    """Verifie que le regex de matching href dans _relocate_by_id rejette
    bien les substring (Blocker Major 5 du review v1)."""

    def _segment(self, href):
        import re

        m = re.search(r"/post/([^/?#]+)", href)
        return m.group(1) if m else None

    def test_strict_match_accepted(self):
        self.assertEqual(self._segment("https://fansly.com/post/12345"), "12345")
        self.assertEqual(self._segment("/post/12345?foo=bar"), "12345")
        self.assertEqual(self._segment("/post/12345#anchor"), "12345")
        self.assertEqual(self._segment("/post/12345/edit"), "12345")

    def test_no_substring_collision(self):
        # value='123' doit PAS matcher '/post/91234567' (substring naive)
        segment = self._segment("https://fansly.com/post/91234567")
        self.assertEqual(segment, "91234567")
        self.assertNotEqual(segment, "123")

    def test_returns_none_when_no_post_path(self):
        self.assertIsNone(self._segment("https://fansly.com/profile/x"))


# =================== Phase 0 : fix worker hang ===================


class TestSQLiteBusyTimeout(_TmpStateMixin, unittest.TestCase):
    """Verifie que PRAGMA busy_timeout=5000 est actif sur la connexion
    StateStore. Avant le fix worker hang : busy_timeout=0 (default) =
    blocage indefini sur lock contention WAL avec UI Streamlit."""

    def test_busy_timeout_is_5000ms(self):
        from fansly_bot.infra.state import StateStore

        self.store = StateStore(self.settings)
        cur = self.store._conn.execute("PRAGMA busy_timeout")
        value = cur.fetchone()[0]
        self.assertEqual(
            value, 5000,
            f"PRAGMA busy_timeout doit etre 5000ms, got {value}ms",
        )


class TestAuthSessionCheckCache(unittest.IsolatedAsyncioTestCase):
    """Verifie que ensure_logged_in() utilise un cache base sur
    session_check_interval_minutes. Avant ce fix, ensure_logged_in faisait
    un page.goto(/home) a chaque publish_next() — explosion de la surface
    de hang sur SPA Angular zombie (job 49)."""

    def _make_auth(self, interval_minutes: float = 60):
        from fansly_bot.services.auth import AuthService

        # Stub Settings minimal avec les seuls champs lus par ensure_logged_in
        class _Auth:
            base_url = "https://fansly.com"
            home_path = "/home"
            session_check_interval_minutes = interval_minutes

        class _Browser:
            navigation_timeout_ms = 30000

        class _Settings:
            auth = _Auth()
            browser = _Browser()

        svc = AuthService.__new__(AuthService)  # bypass __init__
        svc._settings = _Settings()
        svc._session = None
        svc._humanizer = None
        svc._last_check_monotonic = 0.0
        return svc

    async def test_second_call_within_interval_is_skipped(self):
        # Premier appel : on simule un check reussi en settant manuellement
        # le timestamp comme si la 1ere goto venait juste de reussir.
        import time as _time

        svc = self._make_auth(interval_minutes=60)
        svc._last_check_monotonic = _time.monotonic()
        # Le 2e appel doit skip et NE PAS toucher au session (qui est None
        # donc planterait si appele). Si ca passe sans exception : cache OK.
        await svc.ensure_logged_in()
        # Toujours dans la fenetre de cache
        self.assertGreater(svc._last_check_monotonic, 0.0)

    async def test_force_bypasses_cache(self):
        # Avec force=True, le check est tente meme dans la fenetre de cache.
        # Comme svc._session est None, page() levera AttributeError -> on
        # capture pour confirmer qu'on a bien essaye d'aller au-dela du cache.
        import time as _time

        svc = self._make_auth(interval_minutes=60)
        svc._last_check_monotonic = _time.monotonic()
        with self.assertRaises(AttributeError):
            await svc.ensure_logged_in(force=True)

    async def test_zero_interval_disables_cache(self):
        # interval=0 -> le check est tente a chaque appel (utile pour debug).
        import time as _time

        svc = self._make_auth(interval_minutes=0)
        svc._last_check_monotonic = _time.monotonic()
        with self.assertRaises(AttributeError):
            await svc.ensure_logged_in()


# =================== Phase 2 : manager UI (instances.py) ==================


class TestManagerInstanceRegex(unittest.TestCase):
    """Verifie le pattern _INSTANCE_NAME_RE qui extrait le nom d'une
    instance depuis le nom de son container (fansly-bot-NAME)."""

    def test_extracts_valid_names(self):
        from fansly_manager.instances import _INSTANCE_NAME_RE

        for container, expected in [
            ("fansly-bot-marie", "marie"),
            ("fansly-bot-camille_2", "camille_2"),
            ("fansly-bot-LolaTest", "LolaTest"),
            ("fansly-bot-default", "default"),
            ("fansly-bot-instance_42", "instance_42"),
        ]:
            m = _INSTANCE_NAME_RE.match(container)
            self.assertIsNotNone(m, f"Should match: {container}")
            self.assertEqual(m.group("name"), expected)

    def test_rejects_non_bot_containers(self):
        from fansly_manager.instances import _INSTANCE_NAME_RE

        for container in [
            "fansly-manager",       # le manager lui-meme — exclu
            "fansly-bot",           # ancien format (sans suffixe)
            "fansly-bot-",          # suffixe vide
            "fansly-bot-a-b",       # tiret au milieu interdit
            "other-container",
            "fansly_bot_marie",     # underscores partout, pas le bon format
            "fansly-bot-" + "a" * 65,  # cap a 64 chars
        ]:
            m = _INSTANCE_NAME_RE.match(container)
            self.assertIsNone(m, f"Should NOT match: {container}")


class TestManagerStatusPill(unittest.TestCase):
    """Verifie que status_pill est XSS-safe et accessible (glyphe geometrique)."""

    def test_escapes_malicious_status(self):
        from fansly_manager.components import status_pill

        html_out = status_pill('<script>alert("xss")</script>')
        # Le payload brut ne doit jamais apparaitre tel quel
        self.assertNotIn("<script>", html_out)
        self.assertNotIn('alert("xss")', html_out)
        # Et il doit etre escape — le case-insensitive permet l'upper-case
        # qu'on applique au label
        self.assertIn("&lt;", html_out.lower())
        self.assertIn("&gt;", html_out.lower())

    def test_includes_geometric_glyph_for_accessibility(self):
        # Daltoniens : la couleur seule ne suffit pas, on ajoute un symbole.
        from fansly_manager.components import status_pill

        html = status_pill("running")
        self.assertIn("●", html)
        self.assertIn("fm-status-glyph", html)
        self.assertIn("aria-hidden", html)

    def test_unknown_status_uses_default(self):
        from fansly_manager.components import status_pill

        html = status_pill("frobnicated")
        self.assertIn("fm-status-unknown", html)
        self.assertIn("○", html)  # cercle vide pour unknown


class TestManagerHumanizeDuration(unittest.TestCase):
    """Edge cases de humanize_duration (negatif, float, bornes minutes/heures/jours)."""

    def test_zero_seconds(self):
        from fansly_manager.components import humanize_duration
        self.assertEqual(humanize_duration(0), "0 s")

    def test_seconds_below_minute(self):
        from fansly_manager.components import humanize_duration
        self.assertEqual(humanize_duration(45), "45 s")
        self.assertEqual(humanize_duration(59), "59 s")

    def test_minutes(self):
        from fansly_manager.components import humanize_duration
        self.assertEqual(humanize_duration(60), "1 min")
        self.assertEqual(humanize_duration(3599), "59 min")

    def test_hours(self):
        from fansly_manager.components import humanize_duration
        self.assertEqual(humanize_duration(3600), "1 h")
        self.assertEqual(humanize_duration(3720), "1 h 2 min")
        self.assertEqual(humanize_duration(86399), "23 h 59 min")

    def test_days(self):
        from fansly_manager.components import humanize_duration
        self.assertEqual(humanize_duration(86400), "1 j")
        self.assertEqual(humanize_duration(90000), "1 j 1 h")

    def test_negative_returns_dash_not_zero(self):
        # int(-0.5) = 0 sans le check explicit < 0 -> on rendrait "0 s"
        # On veut "—" pour signaler une valeur invalide.
        from fansly_manager.components import humanize_duration
        self.assertEqual(humanize_duration(-5), "—")
        self.assertEqual(humanize_duration(-0.5), "—")

    def test_invalid_type_returns_dash(self):
        from fansly_manager.components import humanize_duration
        self.assertEqual(humanize_duration("not a number"), "—")
        self.assertEqual(humanize_duration(None), "—")


class TestManagerDashboardUrl(unittest.TestCase):
    """Property dashboard_url : 4 combinaisons running/port."""

    def _inst(self, status, port):
        from fansly_manager.instances import Instance
        return Instance(
            name="t", container="fansly-bot-t",
            status=status, host_port=port, image="x",
        )

    def test_running_with_port(self):
        self.assertEqual(self._inst("running", 8501).dashboard_url, "http://localhost:8501")

    def test_running_no_port(self):
        self.assertIsNone(self._inst("running", None).dashboard_url)

    def test_stopped_with_port(self):
        self.assertIsNone(self._inst("exited", 8501).dashboard_url)

    def test_stopped_no_port(self):
        self.assertIsNone(self._inst("exited", None).dashboard_url)

    def test_invalid_port_rejected(self):
        # Port en dehors de la plage non-privilegiee 1024-65535
        self.assertIsNone(self._inst("running", 0).dashboard_url)
        self.assertIsNone(self._inst("running", -1).dashboard_url)
        self.assertIsNone(self._inst("running", 70000).dashboard_url)


class TestManagerExtractHostPort(unittest.TestCase):
    """_extract_host_port avec shapes Docker variees."""

    def test_normal_mapping(self):
        from fansly_manager.instances import _extract_host_port
        attrs = {"NetworkSettings": {"Ports": {
            "8501/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8501"}]
        }}}
        self.assertEqual(_extract_host_port(attrs), 8501)

    def test_prefers_localhost_over_other_ips(self):
        from fansly_manager.instances import _extract_host_port
        attrs = {"NetworkSettings": {"Ports": {
            "8501/tcp": [
                {"HostIp": "0.0.0.0", "HostPort": "9000"},
                {"HostIp": "127.0.0.1", "HostPort": "8501"},
            ]
        }}}
        self.assertEqual(_extract_host_port(attrs), 8501)

    def test_no_mapping_returns_none(self):
        from fansly_manager.instances import _extract_host_port
        self.assertIsNone(_extract_host_port({}))
        self.assertIsNone(_extract_host_port({"NetworkSettings": {}}))
        self.assertIsNone(_extract_host_port({"NetworkSettings": {"Ports": {}}}))
        self.assertIsNone(_extract_host_port({"NetworkSettings": {"Ports": {"8501/tcp": None}}}))


class TestManagerSafeWrappers(unittest.IsolatedAsyncioTestCase):
    """Verifie que les safe_* catchent toutes les DockerException, pas
    seulement NotFound — pour eviter qu'une erreur daemon crashe la page."""

    async def test_safe_list_returns_error_on_docker_exception(self):
        from unittest.mock import patch
        from fansly_manager import instances as inst_mod

        with patch.object(inst_mod, "_client") as mock_client:
            mock_client.side_effect = RuntimeError("daemon unreachable")
            result = inst_mod.safe_list_instances()
            self.assertFalse(result.ok)
            self.assertIn("daemon unreachable", result.error)

    async def test_safe_list_returns_empty_on_total_failure(self):
        # Compat alias list_instances() doit retourner [] sans crasher
        # meme si _client() leve une exception non-NotFound.
        from unittest.mock import patch
        from fansly_manager import instances as inst_mod

        with patch.object(inst_mod, "_client") as mock_client:
            mock_client.side_effect = RuntimeError("boom")
            self.assertEqual(inst_mod.list_instances(), [])

    def test_container_to_instance_reads_image_from_attrs_no_lazy_inspect(self):
        # Regression : un container dont l'image a ete supprimee ne doit PAS
        # declencher de lazy c.image (GET /images/<id>/json -> 404). On lit
        # depuis c.attrs. Acceder a .image sur ce mock leverait une erreur.
        from fansly_manager import instances as inst_mod

        class _DeadImageContainer:
            name = "fansly-bot-marie"
            status = "exited"
            attrs = {
                "State": {"StartedAt": "2026-07-01T00:00:00Z"},
                "Image": "sha256:5a8c4e33eaffa0119d7715c2a989a3f696e40ae10",
                "Config": {"Image": "fansly-bot:latest"},
                "NetworkSettings": {"Ports": {}},
            }

            @property
            def image(self):  # simule le 404 lazy de docker-py
                raise RuntimeError("404 No such image: sha256:5a8c4e33")

        inst = inst_mod._container_to_instance(_DeadImageContainer())
        self.assertIsNotNone(inst)
        self.assertEqual(inst.name, "marie")
        self.assertEqual(inst.image, "fansly-bot:latest")  # depuis attrs, pas .image

    def test_safe_list_skips_container_that_raises(self):
        # Un container dont la conversion leve NE DOIT PAS faire echouer
        # toute la liste : il est skip, les autres passent.
        from unittest.mock import patch
        from fansly_manager import instances as inst_mod

        class _GoodContainer:
            name = "fansly-bot-ok"
            status = "running"
            attrs = {
                "State": {"StartedAt": "2026-07-01T00:00:00Z"},
                "Image": "sha256:abc123",
                "Config": {"Image": "fansly-bot:latest"},
                "NetworkSettings": {"Ports": {"8501/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8503"}]}},
            }
            image = None  # pas utilise (on lit attrs)

        class _PoisonContainer:
            name = "fansly-bot-poison"
            # attrs manquant -> _container_to_instance leve dans le corps
            @property
            def attrs(self):
                raise RuntimeError("attrs corrompus")
            status = "exited"

        class _FakeClient:
            def __init__(self, containers):
                self._containers = containers
            class _C:
                pass
            @property
            def containers(self):
                outer = self
                class _Containers:
                    def list(self, all=False):
                        return outer._containers
                return _Containers()

        fake = _FakeClient([_GoodContainer(), _PoisonContainer()])
        with patch.object(inst_mod, "_client", return_value=fake):
            result = inst_mod.safe_list_instances()
        self.assertTrue(result.ok, f"la liste ne doit pas echouer, got {result.error}")
        names = [i.name for i in result.value]
        self.assertIn("ok", names)
        self.assertNotIn("poison", names)  # le container fautif est skip


# ============================================================
# Playlist ordonnee (rotation batch avec ordre fige au cycle 1)
# ============================================================


class TestPlaylistOrderState(_TmpStateMixin, unittest.TestCase):
    """Tests unitaires de la persistence playlist_order dans state.py."""

    def _fresh_store(self):
        from fansly_bot.infra.state import StateStore
        self.store = StateStore(self.settings)
        return self.store

    def test_new_batch_has_empty_playlist(self):
        store = self._fresh_store()
        store.start_batch("perla_10", max_cycles=3)
        batch = store.get_active_batch()
        self.assertEqual(batch.playlist_order, [])

    def test_set_playlist_order_persists(self):
        store = self._fresh_store()
        store.start_batch("perla_10")
        playlist = ["c.mp4", "a.mp4", "b.mp4"]
        store.set_batch_playlist_order(playlist)
        # Re-lire pour verifier la persistance
        batch = store.get_active_batch()
        self.assertEqual(batch.playlist_order, playlist)

    def test_advance_cycle_preserves_playlist(self):
        store = self._fresh_store()
        store.start_batch("perla_10", max_cycles=5)
        playlist = ["m3.mp4", "m1.mp4", "m2.mp4"]
        store.set_batch_playlist_order(playlist)
        store.add_to_batch_published("m3.mp4")
        # Cycle 1 -> 2
        store.advance_batch_cycle(2)
        batch = store.get_active_batch()
        self.assertEqual(batch.current_cycle, 2)
        self.assertEqual(batch.published_in_cycle, [], "published_in_cycle doit etre reset")
        self.assertEqual(batch.playlist_order, playlist, "playlist_order doit etre preserve")

    def test_get_published_post_ids_in_window(self):
        # Purge par IDs stockes : fenetre INCLUSIVE, exclut hors-fenetre et
        # posts sans fansly_post_id. Robuste au format tz/precision (DATE()).
        store = self._fresh_store()
        conn = store._conn
        rows = [
            (1, "b", 1, "a.mp4", "2026-07-01T00:34:32.1+00:00", "cap", "111"),
            (1, "b", 1, "b.mp4", "2026-07-02T22:36:20+00:00", "cap", "222"),
            (1, "b", 1, "c.mp4", "2026-07-03T05:00:00+00:00", "cap", "333"),  # hors
            (1, "b", 1, "d.mp4", "2026-07-01T10:00:00+00:00", "cap", None),   # sans id
        ]
        for r in rows:
            conn.execute(
                "INSERT INTO media_published (run_id,batch_name,cycle_number,"
                "media_filename,published_at,caption_used,fansly_post_id) "
                "VALUES (?,?,?,?,?,?,?)", r,
            )
        conn.commit()
        res = store.get_published_post_ids_in_window(
            "2026-07-01T00:00:00+00:00", "2026-07-02T23:59:59+00:00"
        )
        self.assertEqual([x[0] for x in res], ["111", "222"])

    def test_new_batch_resets_playlist(self):
        store = self._fresh_store()
        store.start_batch("batch_a")
        store.set_batch_playlist_order(["a.mp4", "b.mp4"])
        # Nouveau batch : la playlist doit etre remise a zero
        store.start_batch("batch_b")
        batch = store.get_active_batch()
        self.assertEqual(batch.name, "batch_b")
        self.assertEqual(batch.playlist_order, [])

    def test_add_to_batch_published_does_not_touch_playlist(self):
        store = self._fresh_store()
        store.start_batch("perla_10")
        playlist = ["x.mp4", "y.mp4"]
        store.set_batch_playlist_order(playlist)
        store.add_to_batch_published("x.mp4")
        batch = store.get_active_batch()
        self.assertEqual(batch.playlist_order, playlist)
        self.assertEqual(batch.published_in_cycle, ["x.mp4"])

    def test_migration_alter_table_adds_playlist_order(self):
        """DB pre-migration -> ouverture ajoute la colonne automatiquement."""
        # 1) Cree une DB avec active_batch SANS playlist_order (schema legacy)
        conn = sqlite3.connect(str(self.settings.paths.state_db))
        conn.execute("""
            CREATE TABLE active_batch (
                id                 INTEGER PRIMARY KEY CHECK (id = 1),
                name               TEXT NOT NULL,
                started_at         TEXT NOT NULL,
                max_cycles         INTEGER NOT NULL DEFAULT 0,
                current_cycle      INTEGER NOT NULL DEFAULT 1,
                published_in_cycle TEXT NOT NULL DEFAULT '[]',
                total_published    INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            INSERT INTO active_batch
                (id, name, started_at, max_cycles, current_cycle,
                 published_in_cycle, total_published)
            VALUES (1, 'legacy', '2026-06-25T00:00:00+00:00', 5, 3, '["a.mp4"]', 10)
        """)
        # job_queue legacy avec un job publish : la migration run_id backfille
        # l'active_batch depuis l'id du dernier job publish (comme en prod, ou
        # un lot est toujours pilote par un job). Sans job publish, la migration
        # supprimerait l'active_batch (stop propre) — comportement voulu mais
        # non representatif d'un vrai upgrade en cours de lot.
        conn.execute("""
            CREATE TABLE job_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL,
                config TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
                started_at TEXT, finished_at TEXT, log_path TEXT, error TEXT
            )
        """)
        conn.execute(
            "INSERT INTO job_queue (id, type, config, status, created_at) "
            "VALUES (42, 'publish', '{}', 'running', '2026-06-25T00:00:00+00:00')"
        )
        conn.commit()
        # Verifier absence pre-migration
        cols_before = [
            r[1] for r in conn.execute("PRAGMA table_info(active_batch)").fetchall()
        ]
        self.assertNotIn("playlist_order", cols_before)
        self.assertNotIn("run_id", cols_before)
        conn.close()

        # 2) Ouvrir avec StateStore : les migrations ALTER TABLE doivent s'appliquer
        store = self._fresh_store()

        conn = sqlite3.connect(str(self.settings.paths.state_db))
        cols_after = [
            r[1] for r in conn.execute("PRAGMA table_info(active_batch)").fetchall()
        ]
        self.assertIn("playlist_order", cols_after)
        self.assertIn("run_id", cols_after)
        conn.close()

        # 3) Le batch legacy est preserve, playlist_order par defaut = [],
        #    run_id backfille depuis le dernier job publish (id 42).
        batch = store.get_active_batch()
        self.assertEqual(batch.name, "legacy")
        self.assertEqual(batch.current_cycle, 3)
        self.assertEqual(batch.published_in_cycle, ["a.mp4"])
        self.assertEqual(batch.playlist_order, [])
        self.assertEqual(batch.run_id, 42)


class TestSelectNextMedia(unittest.TestCase):
    """Tests unitaires de la fonction pure de selection Uploader._select_next_media."""

    def setUp(self):
        # Import a l'interieur pour eviter le cout d'import si tests non lances
        from fansly_bot.services.uploader import UploaderService
        self.select = UploaderService._select_next_media
        # RNG deterministe pour reproductibilite des shuffles
        self.rng = random.Random(42)

    def test_cycle_1_initializes_playlist_with_shuffle(self):
        """1er cycle : playlist vide -> shuffle initial persistee."""
        disk = ["a.mp4", "b.mp4", "c.mp4", "d.mp4"]
        media, new_playlist, event = self.select(
            current_playlist=[],
            disk_names_sorted=disk,
            published_in_cycle=[],
            rng=self.rng,
        )
        self.assertEqual(event, "init")
        # La playlist contient tous les fichiers du disque, dans un ordre potentiellement different
        self.assertEqual(sorted(new_playlist), sorted(disk))
        # Le media picke est le 1er de la playlist shuffle
        self.assertEqual(media, new_playlist[0])

    def test_cycle_2_follows_frozen_order(self):
        """Cycle 2+ : playlist existante, published_in_cycle vide (reset) -> premier de la playlist."""
        playlist = ["c.mp4", "a.mp4", "d.mp4", "b.mp4"]
        disk = ["a.mp4", "b.mp4", "c.mp4", "d.mp4"]
        media, new_playlist, event = self.select(
            current_playlist=playlist,
            disk_names_sorted=disk,
            published_in_cycle=[],
            rng=self.rng,
        )
        self.assertEqual(event, "unchanged")
        self.assertEqual(new_playlist, playlist)
        self.assertEqual(media, "c.mp4", "Doit prendre le 1er de la playlist")

    def test_mid_cycle_skips_already_published(self):
        """Milieu de cycle : c.mp4 deja publie -> prendre a.mp4 (2eme de la playlist)."""
        playlist = ["c.mp4", "a.mp4", "d.mp4", "b.mp4"]
        disk = ["a.mp4", "b.mp4", "c.mp4", "d.mp4"]
        media, _, event = self.select(
            current_playlist=playlist,
            disk_names_sorted=disk,
            published_in_cycle=["c.mp4"],
            rng=self.rng,
        )
        self.assertEqual(event, "unchanged")
        self.assertEqual(media, "a.mp4")

    def test_A1_new_file_appended_to_end(self):
        """A1 : nouveau fichier sur disque, absent de playlist -> ajoute a la FIN."""
        playlist = ["c.mp4", "a.mp4", "b.mp4"]
        disk = ["a.mp4", "b.mp4", "c.mp4", "e.mp4", "f.mp4"]  # e et f sont nouveaux
        media, new_playlist, event = self.select(
            current_playlist=playlist,
            disk_names_sorted=disk,
            published_in_cycle=["c.mp4", "a.mp4", "b.mp4"],  # les 3 originaux deja publies
            rng=self.rng,
        )
        self.assertEqual(event, "extend")
        # Les 3 premiers restent dans l'ordre initial
        self.assertEqual(new_playlist[:3], ["c.mp4", "a.mp4", "b.mp4"])
        # e et f sont a la fin, en ordre alphabetique stable (pas de shuffle)
        self.assertEqual(new_playlist[3:], ["e.mp4", "f.mp4"])
        # Media picke : e (le premier nouveau, tous les autres deja publies)
        self.assertEqual(media, "e.mp4")

    def test_A1_multiple_new_files_ordered_alphabetically(self):
        """A1 : plusieurs nouveaux fichiers -> ordre alphabetique stable."""
        playlist = ["a.mp4"]
        disk = ["a.mp4", "z.mp4", "b.mp4", "m.mp4"]
        _, new_playlist, event = self.select(
            current_playlist=playlist,
            disk_names_sorted=sorted(disk),  # disk arrive deja trie
            published_in_cycle=[],
            rng=self.rng,
        )
        self.assertEqual(event, "extend")
        self.assertEqual(new_playlist, ["a.mp4", "b.mp4", "m.mp4", "z.mp4"])

    def test_B1_missing_file_skipped_silently(self):
        """B1 : c.mp4 dans la playlist mais absent du disque -> saute, prend le suivant."""
        playlist = ["c.mp4", "a.mp4", "d.mp4"]
        disk = ["a.mp4", "d.mp4"]  # c.mp4 disparu
        media, new_playlist, event = self.select(
            current_playlist=playlist,
            disk_names_sorted=disk,
            published_in_cycle=[],
            rng=self.rng,
        )
        # La playlist n'est pas modifiee : on garde c.mp4 pour compatibilite future
        # (si le fichier reapparait, il sera repris)
        self.assertEqual(new_playlist, playlist)
        self.assertEqual(event, "unchanged")
        # Le pick saute c.mp4 et prend a.mp4
        self.assertEqual(media, "a.mp4")

    def test_playlist_exhausted_returns_none(self):
        """Tous les fichiers de la playlist sont publies OU disparus -> None."""
        playlist = ["a.mp4", "b.mp4"]
        disk = ["a.mp4"]  # b.mp4 disparu
        media, _, event = self.select(
            current_playlist=playlist,
            disk_names_sorted=disk,
            published_in_cycle=["a.mp4"],  # a.mp4 publie ce cycle
            rng=self.rng,
        )
        self.assertIsNone(media)
        self.assertEqual(event, "exhausted")

    def test_deterministic_order_across_full_batch_lifecycle(self):
        """Simulation E2E : 3 medias, 2 cycles complets. L'ordre est identique."""
        disk = ["m1.mp4", "m2.mp4", "m3.mp4"]

        # Cycle 1 : init + publier les 3
        rng = random.Random(42)
        published = []
        picks_cycle_1 = []
        playlist = []
        for _ in range(3):
            media, playlist, _ = self.select(
                current_playlist=playlist,
                disk_names_sorted=disk,
                published_in_cycle=published,
                rng=rng,
            )
            picks_cycle_1.append(media)
            published.append(media)

        # Cycle 2 : published reset, meme playlist -> meme ordre
        published_cycle_2 = []
        picks_cycle_2 = []
        for _ in range(3):
            media, playlist, _ = self.select(
                current_playlist=playlist,
                disk_names_sorted=disk,
                published_in_cycle=published_cycle_2,
                rng=rng,
            )
            picks_cycle_2.append(media)
            published_cycle_2.append(media)

        self.assertEqual(
            picks_cycle_1, picks_cycle_2,
            f"L'ordre du cycle 2 doit etre identique au cycle 1. "
            f"Cycle1={picks_cycle_1} Cycle2={picks_cycle_2}"
        )


# ============================================================
# Reprise apres crash (Phase A) — machine a etats anti-doublon
# ============================================================


class TestCrashResume(_TmpStateMixin, unittest.TestCase):
    """Tests LOGIQUE de la reprise-apres-crash. Le 'crash' est simule en
    amenant la DB dans son etat residuel (via les primitives write-ahead)
    puis en fermant/rouvrant le StateStore et en appelant la reconciliation.
    Aucun Docker, aucun navigateur, aucun compte Fansly."""

    def _fresh_store(self):
        from fansly_bot.infra.state import StateStore
        self.store = StateStore(self.settings)
        return self.store

    def _crash_reopen(self):
        """Simule un kill du worker : ferme puis rouvre le StateStore sur la
        meme DB (etat exactement residuel apres crash)."""
        self.store.close()
        return self._fresh_store()

    def _enqueue_running_publish(self, store, batch="b"):
        jid = store.enqueue_job("publish", {"batch_name": batch, "max_cycles": 0})
        store._conn.execute("UPDATE job_queue SET status='running' WHERE id=?", (jid,))
        store._conn.commit()
        return jid

    # ---- CP1 : clicked=0 => aucun POST parti => republier, aucune perte ----
    def test_cp1_clicked0_republishes_no_loss(self):
        s = self._fresh_store()
        rid = s.start_or_resume_batch("b", 1, 100)
        s.set_batch_playlist_order(["m1.mp4", "m2.mp4"])
        s.mark_publish_in_flight(rid, "b", 1, "m1.mp4", "cap")  # write-ahead
        # clicked reste 0 : le clic Post n'a jamais ete emis
        s = self._crash_reopen()
        counts = s.reconcile_publish_in_flight()
        self.assertEqual(counts["republish"], 1)
        # m1 NON marque publie -> reste pending -> sera republie (pas de perte)
        self.assertNotIn("m1.mp4", s.get_active_batch().published_in_cycle)
        self.assertEqual(s.list_orphan_in_flight(), [])
        n = s._conn.execute(
            "SELECT COUNT(*) FROM media_published WHERE media_filename='m1.mp4'"
        ).fetchone()[0]
        self.assertEqual(n, 0)

    # ---- CP2/CP3b : clicked=1 sans id => skip conservateur, jamais republier ----
    def test_cp2_clicked1_no_id_skip_never_republish(self):
        s = self._fresh_store()
        rid = s.start_or_resume_batch("b", 0, 100)
        s.set_batch_playlist_order(["m1.mp4", "m2.mp4"])
        s.mark_publish_in_flight(rid, "b", 1, "m1.mp4", "cap")
        s.mark_in_flight_clicked(rid, "b", 1, "m1.mp4")  # clic emis, id JAMAIS capture
        s = self._crash_reopen()
        counts = s.reconcile_publish_in_flight()
        self.assertEqual(counts["ambiguous_zombie"], 1)
        # marque publie (skip) : dans published_in_cycle, in_flight vide
        self.assertIn("m1.mp4", s.get_active_batch().published_in_cycle)
        self.assertEqual(s.list_orphan_in_flight(), [])
        # media_published avec id NULL (zombie potentiel, non rotable)
        row = s._conn.execute(
            "SELECT fansly_post_id FROM media_published WHERE media_filename='m1.mp4'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertIsNone(row[0])

    # ---- CP3 : clicked=1 + id => publie confirme, une seule ligne ----
    def test_cp3_clicked1_with_id_confirmed(self):
        s = self._fresh_store()
        rid = s.start_or_resume_batch("b", 0, 100)
        s.set_batch_playlist_order(["m1.mp4", "m2.mp4"])
        s.mark_publish_in_flight(rid, "b", 1, "m1.mp4", "cap")
        s.mark_in_flight_clicked(rid, "b", 1, "m1.mp4")
        s.set_in_flight_post_id(rid, "b", 1, "m1.mp4", "POST_M1")  # id persiste
        s = self._crash_reopen()
        counts = s.reconcile_publish_in_flight()
        self.assertEqual(counts["confirmed"], 1)
        n = s._conn.execute(
            "SELECT COUNT(*) FROM media_published WHERE media_filename='m1.mp4' "
            "AND fansly_post_id='POST_M1'"
        ).fetchone()[0]
        self.assertEqual(n, 1)
        self.assertIn("m1.mp4", s.get_active_batch().published_in_cycle)
        self.assertEqual(s.list_orphan_in_flight(), [])

    # ---- CP4/CP7 : reconcile idempotent (re-run) sans double-count ----
    def test_cp4_cp7_reconcile_idempotent_no_double_count(self):
        s = self._fresh_store()
        rid = s.start_or_resume_batch("b", 0, 100)
        s.set_batch_playlist_order(["m1.mp4"])
        s.mark_publish_in_flight(rid, "b", 1, "m1.mp4", "cap")
        s.mark_in_flight_clicked(rid, "b", 1, "m1.mp4")
        s.set_in_flight_post_id(rid, "b", 1, "m1.mp4", "POST_M1")
        s = self._crash_reopen()
        s.reconcile_publish_in_flight()
        total1 = s.get_active_batch().total_published
        # re-run reconcile (crash pendant reconcile -> re-execute au boot suivant)
        s.reconcile_publish_in_flight()
        total2 = s.get_active_batch().total_published
        self.assertEqual(total1, total2, "total_published double-compte a la reapplication")
        n = s._conn.execute(
            "SELECT COUNT(*) FROM media_published WHERE media_filename='m1.mp4'"
        ).fetchone()[0]
        self.assertEqual(n, 1)

    # ---- CP6 : orphelin d'un cycle anterieur non ajoute au cycle courant ----
    def test_cp6_prev_cycle_orphan_not_added_to_current_cycle(self):
        s = self._fresh_store()
        rid = s.start_or_resume_batch("b", 0, 100)
        # le batch est au cycle 2, mais l'orphelin porte le cycle 1
        s._conn.execute("UPDATE active_batch SET current_cycle=2 WHERE id=1")
        s._conn.commit()
        s.mark_publish_in_flight(rid, "b", 1, "old.mp4", "cap")
        s.mark_in_flight_clicked(rid, "b", 1, "old.mp4")
        s.set_in_flight_post_id(rid, "b", 1, "old.mp4", "POST_OLD")
        s = self._crash_reopen()
        s.reconcile_publish_in_flight()
        # record fait (sous cycle 1) mais PAS ajoute au published_in_cycle du cycle 2
        self.assertNotIn("old.mp4", s.get_active_batch().published_in_cycle)
        n = s._conn.execute(
            "SELECT COUNT(*) FROM media_published WHERE media_filename='old.mp4' "
            "AND cycle_number=1"
        ).fetchone()[0]
        self.assertEqual(n, 1)

    # ---- CP8 : requeue + resume preserve la position ----
    def test_cp8_requeue_resumes_position(self):
        s = self._fresh_store()
        jid = self._enqueue_running_publish(s, "b")
        rid = s.start_or_resume_batch("b", 0, jid)
        s.set_batch_playlist_order(["m1.mp4", "m2.mp4", "m3.mp4"])
        s.add_to_batch_published("m1.mp4")
        s = self._crash_reopen()
        # boot : reconcile_stale_jobs doit REQUEUE le job publish running
        counts = s.reconcile_stale_jobs()
        self.assertEqual(counts["publish_requeued"], 1)
        job_status = s._conn.execute(
            "SELECT status FROM job_queue WHERE id=?", (jid,)
        ).fetchone()[0]
        self.assertEqual(job_status, "queued")
        # cleanup ne doit PAS supprimer l'active_batch (un job publish est queued)
        self.assertIsNone(s.cleanup_orphan_active_batch())
        # resume : run_id + position preserves
        rid2 = s.start_or_resume_batch("b", 0, 999)
        self.assertEqual(rid2, rid, "resume doit garder le run_id d'origine")
        ab = s.get_active_batch()
        self.assertEqual(ab.published_in_cycle, ["m1.mp4"])
        self.assertEqual(ab.playlist_order, ["m1.mp4", "m2.mp4", "m3.mp4"])

    # ---- CP-PURGE : job purge interrompu => failed ----
    def test_cppurge_stale_purge_marked_failed(self):
        s = self._fresh_store()
        jid = s.enqueue_job("purge", {"dry_run": False})
        s._conn.execute("UPDATE job_queue SET status='running' WHERE id=?", (jid,))
        s._conn.commit()
        s = self._crash_reopen()
        counts = s.reconcile_stale_jobs()
        self.assertEqual(counts["purge_failed"], 1)
        row = s._conn.execute(
            "SELECT status, error FROM job_queue WHERE id=?", (jid,)
        ).fetchone()
        self.assertEqual(row[0], "failed")
        self.assertIn("recovered_from_crash", row[1])

    # ---- publish 'cancelling' => cancelled (respect intention utilisateur) ----
    def test_publish_cancelling_becomes_cancelled(self):
        s = self._fresh_store()
        jid = self._enqueue_running_publish(s, "b")
        s._conn.execute("UPDATE job_queue SET status='cancelling' WHERE id=?", (jid,))
        s._conn.commit()
        s = self._crash_reopen()
        counts = s.reconcile_stale_jobs()
        self.assertEqual(counts["publish_cancelled"], 1)
        self.assertEqual(
            s._conn.execute("SELECT status FROM job_queue WHERE id=?", (jid,)).fetchone()[0],
            "cancelled",
        )

    # ---- Hole 5 : reconcile avec active_batch=None ne crashe pas ----
    def test_reconcile_no_active_batch_does_not_crash(self):
        s = self._fresh_store()
        # in_flight orphelin SANS active_batch (batch stoppe avant le crash)
        s.mark_publish_in_flight(777, "gone", 1, "m1.mp4", "cap")
        s.mark_in_flight_clicked(777, "gone", 1, "m1.mp4")
        s.set_in_flight_post_id(777, "gone", 1, "m1.mp4", "POST_X")
        self.assertIsNone(s.get_active_batch())
        s = self._crash_reopen()
        # ne doit PAS lever (skip add_to_batch_published, garde record+clear)
        counts = s.reconcile_publish_in_flight()
        self.assertEqual(counts["confirmed"], 1)
        self.assertEqual(s.list_orphan_in_flight(), [])  # clear effectue

    # ---- Hole 9 : add_to_batch_published idempotent (total non gonfle) ----
    def test_add_to_batch_published_idempotent_total(self):
        s = self._fresh_store()
        s.start_or_resume_batch("b", 0, 100)
        s.add_to_batch_published("m1.mp4")
        t1 = s.get_active_batch().total_published
        s.add_to_batch_published("m1.mp4")  # reapplication
        t2 = s.get_active_batch().total_published
        self.assertEqual(t1, t2)
        self.assertEqual(t1, 1)

    # ---- Hole 10 : 'cancelling' orphelin balaye en 'cancelled' ----
    def test_sweep_stale_cancelling(self):
        s = self._fresh_store()
        jid = s.enqueue_job("publish", {"batch_name": "b"})
        s._conn.execute("UPDATE job_queue SET status='cancelling' WHERE id=?", (jid,))
        s._conn.commit()
        n = s.sweep_stale_cancelling()
        self.assertEqual(n, 1)
        self.assertEqual(
            s._conn.execute("SELECT status FROM job_queue WHERE id=?", (jid,)).fetchone()[0],
            "cancelled",
        )

    # ---- CP-RETRY (in-session) : reconcile cible du run empeche reselection ----
    def test_cpretry_insession_reconcile_marks_published(self):
        s = self._fresh_store()
        rid = s.start_or_resume_batch("b", 0, 100)
        s.set_batch_playlist_order(["m1.mp4", "m2.mp4"])
        # m1 clicked + id, PAS encore committe (failsafe-timeout in-session)
        s.mark_publish_in_flight(rid, "b", 1, "m1.mp4", "cap")
        s.mark_in_flight_clicked(rid, "b", 1, "m1.mp4")
        s.set_in_flight_post_id(rid, "b", 1, "m1.mp4", "POST_M1")
        # PAS de crash/reopen : meme session, on reconcile le run courant
        counts = s.reconcile_publish_in_flight(run_id_filter=rid)
        self.assertEqual(counts["confirmed"], 1)
        # m1 est publie -> ne sera pas reselectionne (exclu de pending)
        self.assertIn("m1.mp4", s.get_active_batch().published_in_cycle)

    # ---- WIRING : la sequence de boot REELLE du worker (Worker._boot_recover) ----
    # Prouve le cablage WS7 de bout en bout (ordre des 4 appels, active_batch
    # conserve car le job publish est requeue) SANS Docker ni navigateur.
    def test_worker_boot_recover_wiring_end_to_end(self):
        from fansly_bot.worker import Worker
        s = self._fresh_store()
        # etat residuel post-crash : job publish 'running' + orpheline confirmee
        jid = self._enqueue_running_publish(s, "b")
        rid = s.start_or_resume_batch("b", 0, jid)
        s.set_batch_playlist_order(["m1.mp4", "m2.mp4"])
        s.mark_publish_in_flight(rid, "b", 1, "m1.mp4", "cap")
        s.mark_in_flight_clicked(rid, "b", 1, "m1.mp4")
        s.set_in_flight_post_id(rid, "b", 1, "m1.mp4", "POST_M1")
        s.close()
        # boot du worker REEL (ouvre son propre StateStore sur la meme DB)
        w = Worker(self.settings)
        w._boot_recover()
        st = w._state
        # 1) orpheline confirmee (pas republiee) : ligne media_published, in_flight vide
        self.assertEqual(st.list_orphan_in_flight(), [])
        n = st._conn.execute(
            "SELECT COUNT(*) FROM media_published WHERE media_filename='m1.mp4' "
            "AND fansly_post_id='POST_M1'"
        ).fetchone()[0]
        self.assertEqual(n, 1)
        # 2) job publish requeue (repris), pas 'failed'
        self.assertEqual(
            st._conn.execute("SELECT status FROM job_queue WHERE id=?", (jid,)).fetchone()[0],
            "queued",
        )
        # 3) active_batch CONSERVE (car un job publish est queued) et resumable
        ab = st.get_active_batch()
        self.assertIsNotNone(ab)
        self.assertEqual(ab.name, "b")
        self.assertEqual(ab.run_id, rid)
        self.assertIn("m1.mp4", ab.published_in_cycle)
        st.close()

    # ---- run_id stable : idempotence media_published a travers re-queues ----
    def test_run_id_stable_prevents_duplicate_media_published(self):
        s = self._fresh_store()
        rid = s.start_or_resume_batch("b", 0, 100)
        s.commit_publication(
            run_id=rid, batch_name="b", cycle_number=1, media_filename="m1.mp4",
            caption="c", fansly_post_id="P1", add_to_current_cycle=True,
        )
        # meme run_id (resume) => ON CONFLICT DO NOTHING, pas de doublon
        s.commit_publication(
            run_id=rid, batch_name="b", cycle_number=1, media_filename="m1.mp4",
            caption="c", fansly_post_id="P1", add_to_current_cycle=True,
        )
        n = s._conn.execute(
            "SELECT COUNT(*) FROM media_published WHERE media_filename='m1.mp4'"
        ).fetchone()[0]
        self.assertEqual(n, 1)


# ============================================================
# Exclusion des zombies id-NULL (#3) — fermeture du trou anti-doublon
# ============================================================


class TestZombieExclusion(_TmpStateMixin, unittest.TestCase):
    """Un media publie SANS id capture (zombie clicked=1-sans-id) ne doit
    JAMAIS etre republie : le post precedent (peut-etre cree) n'est pas rotable
    (id inconnu), donc le republier creerait un doublon permanent. Ces tests
    prouvent l'exclusion au niveau logique (aucun navigateur/disque)."""

    def _fresh_store(self):
        from fansly_bot.infra.state import StateStore
        self.store = StateStore(self.settings)
        return self.store

    def _uploader(self, run_id):
        """UploaderService REEL (via __new__) avec juste ce qu'il faut pour
        exercer le vrai cablage list_pending_media -> _blocked_media ->
        get_unconfirmed_media (aucun navigateur)."""
        from fansly_bot.services.uploader import UploaderService
        up = UploaderService.__new__(UploaderService)
        up._settings = self.settings
        up._state = self.store
        up._run_id = run_id
        return up

    def _make_media(self, batch, names):
        self.settings.paths.media_folder = self.tmp / "Medias"
        folder = self.settings.paths.media_folder / batch
        folder.mkdir(parents=True, exist_ok=True)
        for n in names:
            (folder / n).write_bytes(b"x")

    # ---- get_unconfirmed_media : ne remonte que les id-NULL du run ----
    def test_get_unconfirmed_media_scopes_to_null_id_and_run(self):
        s = self._fresh_store()
        rid = s.start_or_resume_batch("b", 0, 100)
        # confirme (id) -> PAS un zombie
        s.commit_publication(run_id=rid, batch_name="b", cycle_number=1,
                             media_filename="ok.mp4", caption="c",
                             fansly_post_id="P_OK", add_to_current_cycle=True)
        # zombie (id NULL) du meme run
        s.commit_publication(run_id=rid, batch_name="b", cycle_number=1,
                             media_filename="zombie.mp4", caption="c",
                             fansly_post_id=None, add_to_current_cycle=True)
        # zombie d'un AUTRE run -> ne doit pas polluer
        s.commit_publication(run_id=999, batch_name="b", cycle_number=1,
                             media_filename="other_run.mp4", caption="c",
                             fansly_post_id=None, add_to_current_cycle=False)
        unconfirmed = s.get_unconfirmed_media(rid)
        self.assertEqual(unconfirmed, {"zombie.mp4"})

    # ---- _select_next_media : saute le media bloque ----
    def test_select_next_media_skips_blocked(self):
        from fansly_bot.services.uploader import UploaderService
        rng = __import__("random").Random(0)
        playlist = ["z.mp4", "a.mp4", "b.mp4"]
        # z.mp4 (1er de la playlist) est bloque -> doit prendre a.mp4
        media, _, _ = UploaderService._select_next_media(
            current_playlist=playlist, disk_names_sorted=sorted(playlist),
            published_in_cycle=[], rng=rng, blocked={"z.mp4"},
        )
        self.assertEqual(media, "a.mp4")

    def test_select_next_media_all_blocked_returns_none(self):
        from fansly_bot.services.uploader import UploaderService
        rng = __import__("random").Random(0)
        pl = ["a.mp4", "b.mp4"]
        media, _, event = UploaderService._select_next_media(
            current_playlist=pl, disk_names_sorted=sorted(pl),
            published_in_cycle=[], rng=rng, blocked={"a.mp4", "b.mp4"},
        )
        self.assertIsNone(media)
        self.assertEqual(event, "exhausted")

    # ---- LOGIQUE : _select_next_media + get_unconfirmed_media (fonction pure) ----
    def test_zombie_excluded_from_selection_logic(self):
        from fansly_bot.services.uploader import UploaderService
        s = self._fresh_store()
        rid = s.start_or_resume_batch("b", 0, 100)
        s.set_batch_playlist_order(["m1.mp4", "m2.mp4"])
        s.commit_publication(run_id=rid, batch_name="b", cycle_number=1,
                             media_filename="m1.mp4", caption="c",
                             fansly_post_id="P1", add_to_current_cycle=True)
        s.commit_publication(run_id=rid, batch_name="b", cycle_number=1,
                             media_filename="m2.mp4", caption="c",
                             fansly_post_id=None, add_to_current_cycle=True)
        s.advance_batch_cycle(2)
        blocked = s.get_unconfirmed_media(rid)
        self.assertIn("m2.mp4", blocked)
        media, _, _ = UploaderService._select_next_media(
            current_playlist=s.get_active_batch().playlist_order,
            disk_names_sorted=["m1.mp4", "m2.mp4"],
            published_in_cycle=s.get_active_batch().published_in_cycle,
            rng=__import__("random").Random(0),
            blocked=blocked,
        )
        self.assertEqual(media, "m1.mp4")

    # ---- INTEGRATION REELLE : le vrai cablage exclut le zombie (pas de blocked passe a la main) ----
    # Ce test ECHOUERAIT si on retirait le cablage blocked dans publish_next OU
    # l'exclusion dans list_pending_media (couvre la "fausse assurance" relevee en revue).
    def test_list_pending_media_excludes_zombie_real_wiring(self):
        s = self._fresh_store()
        self._make_media("b", ["m1.mp4", "m2.mp4"])
        rid = s.start_or_resume_batch("b", 0, 100)
        s.set_batch_playlist_order(["m1.mp4", "m2.mp4"])
        # m1 confirme, m2 zombie (id NULL)
        s.commit_publication(run_id=rid, batch_name="b", cycle_number=1,
                             media_filename="m1.mp4", caption="c",
                             fansly_post_id="P1", add_to_current_cycle=True)
        s.commit_publication(run_id=rid, batch_name="b", cycle_number=1,
                             media_filename="m2.mp4", caption="c",
                             fansly_post_id=None, add_to_current_cycle=True)
        s.advance_batch_cycle(2)
        up = self._uploader(rid)
        # VRAI cablage : _blocked_media -> get_unconfirmed_media
        self.assertEqual(up._blocked_media(), {"m2.mp4"})
        pending_names = {p.name for p in up.list_pending_media()}
        self.assertIn("m1.mp4", pending_names)     # confirme -> republiable
        self.assertNotIn("m2.mp4", pending_names)   # zombie -> jamais republie

    # ---- INTEGRATION : advance_cycle_if_needed avance quand seul un confirme reste ----
    def test_advance_cycle_progresses_with_confirmed_media(self):
        s = self._fresh_store()
        self._make_media("b", ["m1.mp4", "m2.mp4"])
        rid = s.start_or_resume_batch("b", 0, 100)
        s.set_batch_playlist_order(["m1.mp4", "m2.mp4"])
        # m2 zombie ; m1 confirme ET publie ce cycle -> plus rien de pending
        s.commit_publication(run_id=rid, batch_name="b", cycle_number=1,
                             media_filename="m1.mp4", caption="c",
                             fansly_post_id="P1", add_to_current_cycle=True)
        s.commit_publication(run_id=rid, batch_name="b", cycle_number=1,
                             media_filename="m2.mp4", caption="c",
                             fansly_post_id=None, add_to_current_cycle=True)
        up = self._uploader(rid)
        # m1 pas encore fait au cycle courant ? Il l'est (published_in_cycle).
        # Donc pending vide -> mais PAS tous bloques (m1 confirme) -> avance.
        self.assertEqual(up.advance_cycle_if_needed(), "advanced")
        self.assertIsNotNone(s.get_active_batch())

    # ---- INTEGRATION : anti-spin, tous bloques -> lot STOPPE (pas de boucle infinie) ----
    def test_advance_cycle_stops_when_all_media_blocked(self):
        s = self._fresh_store()
        self._make_media("b", ["m1.mp4", "m2.mp4"])
        rid = s.start_or_resume_batch("b", 0, 100)  # max_cycles=0 (infini)
        s.set_batch_playlist_order(["m1.mp4", "m2.mp4"])
        # LES DEUX medias sont des zombies -> aucun publiable -> boucle a vide
        for m in ("m1.mp4", "m2.mp4"):
            s.commit_publication(run_id=rid, batch_name="b", cycle_number=1,
                                 media_filename=m, caption="c",
                                 fansly_post_id=None, add_to_current_cycle=True)
        s.advance_batch_cycle(2)
        up = self._uploader(rid)
        self.assertEqual(up.advance_cycle_if_needed(), "stopped")
        self.assertIsNone(s.get_active_batch(), "lot doit etre stoppe, pas spinner")

    # ---- CANCEL->RESTART : un media publie par un run ANTERIEUR (meme cycle) n'est pas republie ----
    # Reproduit le bug prod perla_269 (5 medias run-26 absents du published_in_cycle
    # du run-27). Ce test ECHOUERAIT sans la source cross-run get_published_media_in_cycle.
    def test_cross_run_published_excluded_after_cancel_restart(self):
        s = self._fresh_store()
        self._make_media("b", ["m1.mp4", "m2.mp4", "m3.mp4"])
        # Run A : publie m1 en cycle 1 (id reel) puis le lot est annule/arrete
        ridA = s.start_or_resume_batch("b", 0, 26)
        s.set_batch_playlist_order(["m1.mp4", "m2.mp4", "m3.mp4"])
        s.commit_publication(run_id=ridA, batch_name="b", cycle_number=1,
                             media_filename="m1.mp4", caption="c",
                             fansly_post_id="PA1", add_to_current_cycle=True)
        s.stop_batch()
        # Run B : RESTART FRAIS (nouveau run_id, published_in_cycle remis a zero)
        ridB = s.start_or_resume_batch("b", 0, 27)
        s.set_batch_playlist_order(["m1.mp4", "m2.mp4", "m3.mp4"])
        self.assertEqual(s.get_active_batch().published_in_cycle, [])
        self.assertNotEqual(ridB, ridA)
        up = self._uploader(ridB)
        pending = {p.name for p in up.list_pending_media()}
        # m1 (publie par run A dans CE cycle) ne doit PAS etre republie par run B
        self.assertNotIn("m1.mp4", pending)
        self.assertEqual(pending, {"m2.mp4", "m3.mp4"})

    # ---- cross-run : le blocage est SCOPE au cycle courant (cycle N+1 republie) ----
    def test_cross_run_exclusion_is_cycle_scoped(self):
        s = self._fresh_store()
        self._make_media("b", ["m1.mp4", "m2.mp4"])
        rid = s.start_or_resume_batch("b", 0, 100)
        s.set_batch_playlist_order(["m1.mp4", "m2.mp4"])
        # m1 publie en cycle 1 (id reel)
        s.commit_publication(run_id=rid, batch_name="b", cycle_number=1,
                             media_filename="m1.mp4", caption="c",
                             fansly_post_id="P1", add_to_current_cycle=True)
        s.advance_batch_cycle(2)  # -> cycle 2
        up = self._uploader(rid)
        pending = {p.name for p in up.list_pending_media()}
        # En cycle 2, m1 (publie en cycle 1) DOIT redevenir publiable (republication cyclique)
        self.assertIn("m1.mp4", pending)
        self.assertIn("m2.mp4", pending)

    # ---- media confirme (id) reste republiable au cycle suivant ----
    def test_confirmed_media_is_not_blocked(self):
        s = self._fresh_store()
        rid = s.start_or_resume_batch("b", 0, 100)
        s.commit_publication(run_id=rid, batch_name="b", cycle_number=1,
                             media_filename="ok.mp4", caption="c",
                             fansly_post_id="P_OK", add_to_current_cycle=True)
        self.assertEqual(s.get_unconfirmed_media(rid), set())

    # ---- CŒUR DU FIX : le scope run_id survit a un RESUME (run_id preserve) ----
    def test_zombie_survives_resume_same_run_id(self):
        s = self._fresh_store()
        rid = s.start_or_resume_batch("b", 0, 100)
        s.commit_publication(run_id=rid, batch_name="b", cycle_number=1,
                             media_filename="z.mp4", caption="c",
                             fansly_post_id=None, add_to_current_cycle=True)
        # RESUME : meme nom de lot, job.id different -> doit renvoyer le run_id
        # d'origine (rid), pas 777. Le zombie reste donc bloque.
        rid2 = s.start_or_resume_batch("b", 0, 777)
        self.assertEqual(rid2, rid, "resume doit preserver le run_id d'origine")
        self.assertIn("z.mp4", s.get_unconfirmed_media(rid))

    # ---- degradation gracieuse de _blocked_media ----
    def test_blocked_media_none_run_id_returns_empty(self):
        self._fresh_store()
        up = self._uploader(None)
        self.assertEqual(up._blocked_media(), set())

    def test_blocked_media_swallows_lookup_error(self):
        self._fresh_store()
        up = self._uploader(42)

        class _Boom:
            def get_unconfirmed_media(self, *a, **k):
                raise RuntimeError("db down")
        up._state = _Boom()
        # ne doit PAS propager -> set() (mais log warning, non asserte ici)
        self.assertEqual(up._blocked_media(), set())


# ============================================================
# Garde anti-double-POST in-session (#2) — verrouillage de la decision
# ============================================================


class TestPostClickGuard(unittest.TestCase):
    """Verrouille _should_emit_post_click : LE garde-fou du doublon historique.
    Une inversion de cette logique = doublon reel sans crash -> ces tests
    doivent echouer si quelqu'un casse la decision."""

    def setUp(self):
        from fansly_bot.services.uploader import UploaderService
        self.decide = UploaderService._should_emit_post_click

    def test_no_in_flight_row_clicks(self):
        # Aucune ligne in_flight (1ere tentative) -> on clique
        self.assertTrue(self.decide(None))

    def test_clicked_zero_clicks(self):
        # Write-ahead pose mais clic pas encore emis -> on clique
        self.assertTrue(self.decide({"clicked": 0}))

    def test_clicked_one_skips(self):
        # Un clic Post a DEJA ete emis -> on NE reclique PAS (anti-doublon)
        self.assertFalse(self.decide({"clicked": 1}))

    def test_clicked_one_with_id_skips(self):
        self.assertFalse(self.decide({"clicked": 1, "fansly_post_id": "P1"}))

    def test_missing_clicked_key_defaults_to_click(self):
        # Defensif : ligne sans champ 'clicked' -> on clique (write-ahead refera l'etat)
        self.assertTrue(self.decide({}))

    def test_clicked_zero_with_null_id_clicks(self):
        self.assertTrue(self.decide({"clicked": 0, "fansly_post_id": None}))


# =================== entry point ===================

if __name__ == "__main__":
    unittest.main()
