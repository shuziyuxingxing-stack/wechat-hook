"""Consent-gated, bounded storage for private-chat voice samples."""
from __future__ import annotations

import math
import os
import sqlite3
import time
import uuid
import wave
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Union


PathLike = Union[str, os.PathLike[str]]
_MAX_WAV_BYTES = 16 * 1024 * 1024
_MAX_TIMESTAMP = 253402300799  # 9999-12-31 in Unix seconds
_MAX_ID_LENGTH = 512
_MAX_NICKNAME_LENGTH = 256
_MAX_TOTAL_RECORDINGS = 1000
_MAX_TOTAL_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True)
class VoiceClip:
    sender_id: str
    session_id: str
    nickname: str
    timestamp: int
    path: Path


def _identifier(value: object, name: str) -> str:
    if (not isinstance(value, str) or not value or value != value.strip() or
            len(value) > _MAX_ID_LENGTH or value in (".", "..") or
            any(ord(char) < 32 or char in "/\\" for char in value)):
        raise ValueError(f"invalid {name}")
    return value


def _timestamp(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("timestamp must be a Unix timestamp")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("timestamp is outside the supported range")
    if value < 0 or value > _MAX_TIMESTAMP:
        raise ValueError("timestamp is outside the supported range")
    return int(value)


def _nickname(value: object, fallback: str) -> str:
    if value is None:
        return fallback
    if (not isinstance(value, str) or len(value) > _MAX_NICKNAME_LENGTH or
            any(ord(char) < 32 for char in value)):
        raise ValueError("invalid nickname")
    return value


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _validate_wav(path: Path) -> int:
    """Validate a bounded, uncompressed PCM WAV without retaining its bytes."""
    size = path.stat().st_size
    if not 44 <= size <= _MAX_WAV_BYTES:
        raise ValueError("WAV size is outside the supported range")
    with path.open("rb") as stream:
        header = stream.read(12)
    if len(header) != 12 or header[:4] != b"RIFF" or header[8:] != b"WAVE":
        raise ValueError("audio is not a RIFF WAV")
    try:
        with wave.open(str(path), "rb") as wav:
            channels = wav.getnchannels()
            width = wav.getsampwidth()
            rate = wav.getframerate()
            frames = wav.getnframes()
            if (wav.getcomptype() != "NONE" or channels < 1 or width not in (1, 2, 3, 4)
                    or rate < 1 or frames < 1):
                raise ValueError("unsupported WAV format")
            expected = channels * width * frames
            if expected > _MAX_WAV_BYTES or len(wav.readframes(frames)) != expected:
                raise ValueError("truncated or oversized WAV")
    except (wave.Error, EOFError) as exc:
        raise ValueError("invalid WAV structure") from exc
    return size


class VoiceHistoryStore:
    """Store opted-in private voice clips, bounded to ten per sender.

    The SQLite database contains only sender/session/nickname/timestamp/path
    metadata and consent sender IDs. Audio is copied into ``data_dir`` only
    after ``set_consent(sender_id, True)`` has explicitly recorded consent.
    """

    def __init__(self, data_dir: PathLike, record_root: PathLike) -> None:
        root = Path(data_dir)
        source_root = Path(record_root)
        if not root.is_absolute() or not source_root.is_absolute():
            raise ValueError("storage and record roots must be absolute paths")
        root.mkdir(parents=True, exist_ok=True)
        self._data_root = root.resolve(strict=True)
        if not self._data_root.is_dir():
            raise ValueError("storage root must be a directory")
        self._record_root = source_root.resolve(strict=True)
        if not self._record_root.is_dir():
            raise ValueError("record root must be a directory")

        audio_root = self._data_root / "voice-history"
        audio_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        if audio_root.is_symlink():
            raise ValueError("voice history directory cannot be a symlink")
        self._audio_root = audio_root.resolve(strict=True)
        if not _is_within(self._audio_root, self._data_root) or not self._audio_root.is_dir():
            raise ValueError("voice history directory escapes the plugin data directory")
        try:
            os.chmod(self._audio_root, 0o700)
        except OSError:
            pass

        self._database = self._audio_root / "index.sqlite3"
        if self._database.is_symlink():
            raise ValueError("voice history database cannot be a symlink")
        with self._connection() as connection:
            connection.executescript(
                "CREATE TABLE IF NOT EXISTS consent ("
                " sender_id TEXT PRIMARY KEY, granted_at INTEGER NOT NULL DEFAULT 0"
                ");"
                "CREATE TABLE IF NOT EXISTS clips ("
                " sender_id TEXT NOT NULL, session_id TEXT NOT NULL, nickname TEXT NOT NULL,"
                " timestamp INTEGER NOT NULL, path TEXT NOT NULL UNIQUE"
                ");"
                "CREATE INDEX IF NOT EXISTS clips_by_sender_time "
                " ON clips(sender_id, timestamp DESC);"
                "CREATE INDEX IF NOT EXISTS clips_by_sender_session_time "
                " ON clips(sender_id, session_id, timestamp DESC);"
            )
            consent_columns = {row[1] for row in connection.execute("PRAGMA table_info(consent)")}
            if "granted_at" not in consent_columns:
                connection.execute(
                    "ALTER TABLE consent ADD COLUMN granted_at INTEGER NOT NULL DEFAULT 0"
                )
                connection.execute(
                    "UPDATE consent SET granted_at = ? WHERE granted_at = 0", (int(time.time()),)
                )
        try:
            os.chmod(self._database, 0o600)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database, timeout=10)
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _source_path(self, value: PathLike) -> Path:
        source = Path(value)
        if not source.is_absolute() or ".." in source.parts:
            raise ValueError("voice path must be absolute and cannot traverse directories")
        source = source.resolve(strict=True)
        if (not _is_within(source, self._record_root) or not source.is_file()
                or source.suffix.lower() != ".wav"):
            raise ValueError("voice path must be a WAV inside the configured record root")
        _validate_wav(source)
        return source

    def _stored_path(self, value: str) -> Path:
        path = Path(value)
        if not path.is_absolute():
            raise ValueError("stored voice path is not absolute")
        resolved = path.resolve(strict=True)
        if (resolved.parent != self._audio_root or resolved.suffix != ".wav"
                or not resolved.is_file()):
            raise ValueError("stored voice path escapes the voice history directory")
        return resolved

    def set_consent(self, sender_id: str, allowed: bool) -> None:
        sender_id = _identifier(sender_id, "sender_id")
        if type(allowed) is not bool:
            raise ValueError("consent must be an explicit boolean")
        if allowed:
            with self._connection() as connection:
                connection.execute(
                    "INSERT OR IGNORE INTO consent(sender_id, granted_at) VALUES (?, ?)",
                    (sender_id, int(time.time())),
                )
            return
        self.revoke_consent(sender_id)

    def consent_since(self, sender_id: str) -> int | None:
        sender_id = _identifier(sender_id, "sender_id")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT granted_at FROM consent WHERE sender_id = ?", (sender_id,)
            ).fetchone()
        return int(row[0]) if row is not None else None

    def has_consent(self, sender_id: str) -> bool:
        sender_id = _identifier(sender_id, "sender_id")
        with self._connection() as connection:
            return connection.execute(
                "SELECT 1 FROM consent WHERE sender_id = ?", (sender_id,)
            ).fetchone() is not None

    def revoke_consent(self, sender_id: str) -> int:
        """Revoke consent and remove all retained clips for that sender."""
        sender_id = _identifier(sender_id, "sender_id")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT path FROM clips WHERE sender_id = ?", (sender_id,)
            ).fetchall()
            connection.execute("DELETE FROM clips WHERE sender_id = ?", (sender_id,))
            connection.execute("DELETE FROM consent WHERE sender_id = ?", (sender_id,))
            connection.commit()
        self._unlink_stored_paths(row[0] for row in rows)
        return len(rows)

    def _unlink_stored_paths(self, paths) -> None:
        for raw_path in paths:
            try:
                path = self._stored_path(raw_path)
            except (OSError, ValueError):
                continue
            path.unlink(missing_ok=True)

    def record(
        self,
        sender_id: str,
        session_id: str,
        nickname: str,
        timestamp: int | float,
        wav_path: PathLike,
        *,
        private_chat: bool,
    ) -> VoiceClip:
        """Validate and copy one private WAV from the configured media root."""
        sender_id = _identifier(sender_id, "sender_id")
        session_id = _identifier(session_id, "session_id")
        nickname = _nickname(nickname, sender_id)
        timestamp = _timestamp(timestamp)
        if type(private_chat) is not bool or not private_chat:
            raise ValueError("voice history accepts private-chat clips only")
        consent_since = self.consent_since(sender_id)
        if consent_since is None:
            raise PermissionError("explicit voice-history consent is required")
        if timestamp < consent_since:
            raise PermissionError("voice sample predates explicit consent")
        source = self._source_path(wav_path)

        name = f"{uuid.uuid4().hex}.wav"
        destination = self._audio_root / name
        temporary = self._audio_root / f".{uuid.uuid4().hex}.part"
        try:
            copied = 0
            with source.open("rb") as source_stream, temporary.open("xb") as target_stream:
                while chunk := source_stream.read(1024 * 1024):
                    copied += len(chunk)
                    if copied > _MAX_WAV_BYTES:
                        raise ValueError("WAV exceeds the supported size limit")
                    target_stream.write(chunk)
                try:
                    os.chmod(temporary, 0o600)
                except OSError:
                    pass
                target_stream.flush()
                os.fsync(target_stream.fileno())
            if copied != _validate_wav(temporary):
                raise ValueError("WAV changed while it was copied")
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

        try:
            temporary.replace(destination)
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current_consent = connection.execute(
                    "SELECT granted_at FROM consent WHERE sender_id = ?", (sender_id,)
                ).fetchone()
                if current_consent is None:
                    raise PermissionError("voice-history consent was revoked")
                if timestamp < current_consent[0]:
                    raise PermissionError("voice sample predates explicit consent")
                connection.execute(
                    "INSERT INTO clips(sender_id, session_id, nickname, timestamp, path) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (sender_id, session_id, nickname, timestamp, str(destination)),
                )
                removed = self._prune(connection, sender_id)
                connection.commit()
            self._unlink_stored_paths(removed)
        except Exception:
            temporary.unlink(missing_ok=True)
            destination.unlink(missing_ok=True)
            raise
        return VoiceClip(sender_id, session_id, nickname, timestamp, destination)

    def _prune(self, connection: sqlite3.Connection, sender_id: str) -> list[str]:
        removed: list[str] = []
        sender_rows = connection.execute(
            "SELECT rowid, path FROM clips WHERE sender_id = ? "
            "ORDER BY timestamp DESC, rowid DESC", (sender_id,)
        ).fetchall()
        for rowid, path in sender_rows[10:]:
            connection.execute("DELETE FROM clips WHERE rowid = ?", (rowid,))
            removed.append(path)

        rows = connection.execute("SELECT rowid, path FROM clips ORDER BY rowid DESC").fetchall()
        total_bytes = 0
        sizes: dict[int, int] = {}
        for rowid, path in rows:
            try:
                stored = self._stored_path(path)
                size = stored.stat().st_size
            except (OSError, ValueError):
                size = 0
                connection.execute("DELETE FROM clips WHERE rowid = ?", (rowid,))
                removed.append(path)
                continue
            sizes[rowid] = size
            total_bytes += size

        excess_count = max(0, len(sizes) - _MAX_TOTAL_RECORDINGS)
        for rowid, path in rows:
            if rowid not in sizes:
                continue
            if excess_count <= 0 and total_bytes <= _MAX_TOTAL_BYTES:
                break
            connection.execute("DELETE FROM clips WHERE rowid = ?", (rowid,))
            total_bytes -= sizes[rowid]
            excess_count -= 1
            removed.append(path)
        return removed

    def find_newest(
        self, sender_id: str, session_id: str, *, after_timestamp: int | None = None
    ) -> VoiceClip | None:
        sender_id = _identifier(sender_id, "sender_id")
        session_id = _identifier(session_id, "session_id")
        consent_since = self.consent_since(sender_id)
        if consent_since is None:
            return None
        after_timestamp = consent_since if after_timestamp is None else max(
            consent_since, _timestamp(after_timestamp)
        )
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT rowid, sender_id, session_id, nickname, timestamp, path "
                "FROM clips WHERE sender_id = ? AND session_id = ? AND timestamp >= ? "
                "ORDER BY timestamp DESC, rowid DESC",
                (sender_id, session_id, after_timestamp),
            ).fetchall()
            for rowid, sender, session, nickname, timestamp, raw_path in rows:
                try:
                    path = self._stored_path(raw_path)
                    _validate_wav(path)
                    timestamp = _timestamp(timestamp)
                    sender = _identifier(sender, "sender_id")
                    session = _identifier(session, "session_id")
                    nickname = _nickname(nickname, sender)
                except (OSError, ValueError):
                    connection.execute("DELETE FROM clips WHERE rowid = ?", (rowid,))
                    self._unlink_stored_paths((raw_path,))
                    continue
                return VoiceClip(sender, session, nickname, timestamp, path)
        return None
