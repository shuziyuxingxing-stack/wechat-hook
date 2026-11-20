import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from weixin_drafts import DRAFT_TTL, DraftStore


class DraftStoreTests(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "drafts.sqlite3"
        self.now = 10_000
        self.store = DraftStore(self.path, clock=lambda: self.now)
        self.owner = {"actor_id": "owner", "owner_id": "owner"}

    def test_exact_recipient_and_owner_confirmation(self):
        draft = self.store.save("test-account", "room", "拟回复")
        self.assertEqual(self.store.pending("test-account", "room"), [draft])
        self.assertEqual(self.store.pending("other-account", "room"), [])
        self.assertEqual(self.store.pending("test-account", "other-room"), [])
        with self.assertRaises(PermissionError):
            self.store.claim("test-account", "room", draft.draft_id, confirmed=True,
                             actor_id="peer", owner_id="owner")
        with self.assertRaises(PermissionError):
            self.store.claim("test-account", "room", draft.draft_id, confirmed=1, **self.owner)
        self.assertIsNone(self.store.claim("other-account", "room", draft.draft_id,
                                           confirmed=True, **self.owner))
        self.assertIsNone(self.store.claim("test-account", "other-room", draft.draft_id,
                                           confirmed=True, **self.owner))
        self.assertEqual(self.store.claim("test-account", "room", draft.draft_id,
                                          confirmed=True, **self.owner), draft)

    def test_claim_is_committed_before_caller_can_attempt_sending(self):
        draft = self.store.save("test-account", "room", "最多提交一次")

        def confirm(_: int):
            store = DraftStore(self.path, clock=lambda: self.now)
            return store.claim("test-account", "room", draft.draft_id,
                               confirmed=True, **self.owner)

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(confirm, range(12)))
        self.assertEqual([result for result in results if result is not None], [draft])
        # Even if the one caller failed after claiming, opening a new store
        # cannot return the same draft for an uncertain retry.
        self.assertIsNone(confirm(0))

    def test_expired_draft_cannot_be_claimed(self):
        draft = self.store.save("test-account", "room", "已过期")
        self.now += DRAFT_TTL
        self.assertEqual(self.store.pending("test-account", "room"), [])
        self.assertIsNone(self.store.claim("test-account", "room", draft.draft_id,
                                           confirmed=True, **self.owner))

    def test_mode_persists_per_account_and_session(self):
        with self.assertRaises(PermissionError):
            self.store.set_mode("test-account", "room", "manual", actor_id="peer", owner_id="owner")
        self.assertEqual(self.store.mode("test-account", "room"), "auto")
        self.store.set_mode("test-account", "room", "manual", **self.owner)
        self.store.set_mode("test-account", "other-room", "draft", **self.owner)
        reopened = DraftStore(self.path, clock=lambda: self.now)
        self.assertEqual(reopened.mode("test-account", "room"), "manual")
        self.assertEqual(reopened.mode("test-account", "other-room"), "draft")
        self.assertEqual(reopened.mode("other-account", "room"), "auto")
        reopened.set_mode("test-account", "room", "auto", **self.owner)
        self.assertEqual(self.store.mode("test-account", "room"), "auto")

    def test_source_id_must_be_canonical_positive_signed_int64_text(self):
        for value in ("", "0", "01", "-1", "+1", "１２", " 1", "1.0", "9223372036854775808", 1, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.save("test-account", "room", "拟回复", source_server_id=value)
        for value in ("1", "9223372036854775807"):
            with self.subTest(value=value):
                draft = self.store.save("test-account", "room", "拟回复", source_server_id=value)
                reopened = DraftStore(self.path, clock=lambda: self.now)
                self.assertEqual(reopened.pending("test-account", "room"), [draft])
                consumed = reopened.claim("test-account", "room", draft.draft_id, confirmed=True, **self.owner)
                self.assertEqual(consumed.source_server_id, value)

    def test_legacy_database_keeps_rows_with_no_source(self):
        legacy_path = self.path.with_name("legacy.sqlite3")
        with closing(sqlite3.connect(legacy_path)) as conn, conn:
            conn.execute(
                "CREATE TABLE reply_drafts (draft_id TEXT PRIMARY KEY, account_id TEXT NOT NULL,"
                " session_id TEXT NOT NULL, text TEXT NOT NULL, created_at REAL NOT NULL,"
                " expires_at REAL NOT NULL)"
            )
            conn.execute(
                "INSERT INTO reply_drafts VALUES (?, ?, ?, ?, ?, ?)",
                ("a" * 32, "test-account", "room", "旧草稿", self.now, self.now + DRAFT_TTL),
            )
        migrated = DraftStore(legacy_path, clock=lambda: self.now)
        legacy, = migrated.pending("test-account", "room")
        self.assertEqual(legacy.text, "旧草稿")
        self.assertIsNone(legacy.source_server_id)
        new = migrated.save("test-account", "room", "新草稿", source_server_id="123")
        self.assertIn(new, DraftStore(legacy_path, clock=lambda: self.now).pending("test-account", "room"))
