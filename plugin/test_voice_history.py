"""Synthetic-only tests for the consent-gated local voice history."""
from __future__ import annotations

import io
import sqlite3
import tempfile
import time
import unittest
import wave
from pathlib import Path

from voice_history import VoiceHistoryStore


def synthetic_wav(frames: int = 160) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\x00\x00" * frames)
    return output.getvalue()


class VoiceHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.data = base / "plugin-data"
        self.records = base / "records"
        self.records.mkdir()
        self.store = VoiceHistoryStore(self.data, self.records)
        self.source = self.records / "incoming.wav"
        self.source.write_bytes(synthetic_wav())

    def tearDown(self):
        self.temp.cleanup()

    def record(self, timestamp: int, sender: str = "wxid_sender", session: str = "wxid_sender"):
        return self.store.record(
            sender, session, "Alice", timestamp, self.source, private_chat=True
        )

    def test_requires_persistent_explicit_consent_and_private_chat(self):
        with self.assertRaises(PermissionError):
            self.record(100)
        self.assertEqual(list((self.data / "voice-history").glob("*.wav")), [])

        granted_at = int(time.time())
        self.store.set_consent("wxid_sender", True)
        self.assertGreaterEqual(self.store.consent_since("wxid_sender"), granted_at)
        with self.assertRaises(ValueError):
            self.store.record(
                "wxid_sender", "group@chatroom", "Alice", 100,
                self.source, private_chat=False,
            )
        clip = self.record(granted_at + 1)
        self.assertTrue(clip.path.is_file())
        self.assertNotEqual(clip.path, self.source)
        self.assertEqual(clip.path.read_bytes(), synthetic_wav())

    def test_keeps_at_most_ten_per_sender_and_finds_newest_for_session(self):
        self.store.set_consent("wxid_sender", True)
        granted_at = self.store.consent_since("wxid_sender")
        clips = [self.record(granted_at + timestamp) for timestamp in range(12)]

        newest = self.store.find_newest("wxid_sender", "wxid_sender")
        self.assertIsNotNone(newest)
        self.assertEqual(newest.timestamp, granted_at + 11)
        self.assertEqual(newest.path, clips[-1].path)
        self.assertTrue(all(not clip.path.exists() for clip in clips[:2]))
        self.assertTrue(all(clip.path.exists() for clip in clips[2:]))
        db = sqlite3.connect(self.data / "voice-history" / "index.sqlite3")
        try:
            count = db.execute(
                "SELECT COUNT(*) FROM clips WHERE sender_id = ?", ("wxid_sender",)
            ).fetchone()[0]
            columns = [row[1] for row in db.execute("PRAGMA table_info(clips)")]
            database_bytes = (self.data / "voice-history" / "index.sqlite3").read_bytes()
        finally:
            db.close()
        self.assertEqual(count, 10)
        self.assertEqual(columns, ["sender_id", "session_id", "nickname", "timestamp", "path"])
        self.assertNotIn(b"RIFF", database_bytes)
        self.assertIsNone(self.store.find_newest("wxid_sender", "other-session"))

    def test_revoke_removes_consent_and_saved_audio(self):
        self.store.set_consent("wxid_sender", True)
        clip = self.record(self.store.consent_since("wxid_sender") + 1)

        removed = self.store.revoke_consent("wxid_sender")

        self.assertEqual(removed, 1)
        self.assertFalse(self.store.has_consent("wxid_sender"))
        self.assertFalse(clip.path.exists())
        self.assertIsNone(self.store.find_newest("wxid_sender", "wxid_sender"))
        with self.assertRaises(PermissionError):
            self.record(int(time.time()) + 1)

    def test_pre_consent_voice_is_neither_saved_nor_selected(self):
        before_consent = int(time.time()) - 10
        self.store.set_consent("wxid_sender", True)
        with self.assertRaises(PermissionError):
            self.record(before_consent)
        self.assertIsNone(self.store.find_newest("wxid_sender", "wxid_sender"))

    def test_rejects_invalid_identifiers_timestamps_wav_and_path_escapes(self):
        self.store.set_consent("wxid_sender", True)
        after_consent = self.store.consent_since("wxid_sender") + 1
        with self.assertRaises(ValueError):
            self.store.record("../sender", "session", "Alice", 100,
                              self.source, private_chat=True)
        with self.assertRaises(ValueError):
            self.store.record("wxid_sender", "session", "Alice", float("nan"),
                              self.source, private_chat=True)
        with self.assertRaises(ValueError):
            self.store.record("wxid_sender", "session", "Alice", True,
                              self.source, private_chat=True)
        with self.assertRaises(ValueError):
            self.store.record("wxid_sender", "session", "Alice", after_consent,
                              self.source.parent / ".." / "outside.wav", private_chat=True)

        invalid = self.records / "invalid.wav"
        invalid.write_bytes(b"not a wav file" * 8)
        with self.assertRaises(ValueError):
            self.store.record("wxid_sender", "session", "Alice", after_consent,
                              invalid, private_chat=True)

        outside = self.records.parent / "outside.wav"
        outside.write_bytes(synthetic_wav())
        escaped = self.records / "escaped.wav"
        try:
            escaped.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is unavailable on this host")
        with self.assertRaises(ValueError):
            self.store.record("wxid_sender", "session", "Alice", after_consent,
                              escaped, private_chat=True)


if __name__ == "__main__":
    unittest.main()
