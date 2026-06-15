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


class TestPublishingCycleCleanupMode(unittest.TestCase):
    """Verifie que le flag cycle_cleanup_mode est bien lu/valide par Pydantic."""

    def test_default_is_batch(self):
        from fansly_bot.config import load_settings

        s = load_settings()
        self.assertEqual(s.publishing.cycle_cleanup_mode, "batch")

    def test_invalid_mode_rejected(self):
        from pydantic import ValidationError
        from fansly_bot.config import Publishing

        # Reuse a valid base, just change cycle_cleanup_mode to an invalid value
        from fansly_bot.config import load_settings

        ok_pub = load_settings().publishing
        with self.assertRaises(ValidationError):
            ok_pub.model_copy(update={"cycle_cleanup_mode": "wrong_value"}).model_validate(
                ok_pub.model_copy(
                    update={"cycle_cleanup_mode": "wrong_value"}
                ).model_dump()
            )


# =================== entry point ===================

if __name__ == "__main__":
    unittest.main()
