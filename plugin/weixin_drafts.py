"""Local reply drafts and manual takeover for an exact Weixin session.

The adapter must supply its verified account and destination IDs. Operator IDs
must come from trusted configuration and an authenticated user action. This
module never sends messages. A claim commits before passing a draft back to the
caller, so a failed or uncertain submission cannot be retried with that draft.
"""

from __future__ import annotations

import hmac
import secrets
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path


DRAFT_TTL = 15 * 60
MAX_DRAFT_TEXT = 8000
MAX_PENDING_PER_SESSION = 25
MAX_PENDING_TOTAL = 500
MODES = frozenset({"auto", "draft", "manual"})


@dataclass(frozen=True)
class Draft:
    draft_id: str
    account_id: str
    session_id: str
    text: str
    created_at: float
    expires_at: float
    source_server_id: str | None = None


def _scope_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 512
        or value != value.strip()
        or any(not char.isprintable() for char in value)
    ):
        raise ValueError("invalid account, session or operator ID")
    return value


def _authorize_owner(actor_id: str, owner_id: str) -> None:
    try:
        actor = _scope_id(actor_id).encode("utf-8")
        owner = _scope_id(owner_id).encode("utf-8")
    except (ValueError, UnicodeError):
        raise PermissionError("authenticated owner required") from None
    if not hmac.compare_digest(actor, owner):
        raise PermissionError("authenticated owner required")


def _validate_source_server_id(value: str | None) -> None:
    if value is not None and (
        not isinstance(value, str)
        or not 1 <= len(value) <= 19
        or not value.isascii()
        or not value.isdigit()
        or value[0] == "0"
        or int(value) > (1 << 63) - 1
    ):
        raise ValueError("invalid source message server ID")


class DraftStore:
    """Keep bounded private drafts in the plugin's local data directory."""

    def __init__(self, db_path: Path | str, *, clock=time.time) -> None:
        self._path = Path(db_path)
        if not self._path.is_absolute() or self._path.is_symlink():
            raise ValueError("draft storage requires an absolute, local database path")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        with closing(self._connect()) as conn, conn:
            conn.executescript(
                "BEGIN IMMEDIATE;"
                "CREATE TABLE IF NOT EXISTS reply_drafts ("
                " draft_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, session_id TEXT NOT NULL,"
                " text TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,"
                " source_server_id TEXT"
                ");"
                "CREATE INDEX IF NOT EXISTS drafts_by_session"
                " ON reply_drafts(account_id, session_id, created_at);"
                "CREATE TABLE IF NOT EXISTS reply_modes ("
                " account_id TEXT NOT NULL, session_id TEXT NOT NULL, mode TEXT NOT NULL,"
                " PRIMARY KEY(account_id, session_id)"
                ");"
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(reply_drafts)")}
            if "source_server_id" not in columns:
                conn.execute("ALTER TABLE reply_drafts ADD COLUMN source_server_id TEXT")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, timeout=5)

    def mode(self, account_id: str, session_id: str) -> str:
        scope = (_scope_id(account_id), _scope_id(session_id))
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT mode FROM reply_modes WHERE account_id = ? AND session_id = ?", scope
            ).fetchone()
        return row[0] if row else "auto"

    def set_mode(
        self, account_id: str, session_id: str, mode: str, *, actor_id: str, owner_id: str
    ) -> None:
        _authorize_owner(actor_id, owner_id)
        scope = (_scope_id(account_id), _scope_id(session_id))
        if not isinstance(mode, str) or mode not in MODES:
            raise ValueError("unknown reply mode")
        with closing(self._connect()) as conn, conn:
            if mode == "auto":
                conn.execute("DELETE FROM reply_modes WHERE account_id = ? AND session_id = ?", scope)
            else:
                conn.execute(
                    "INSERT INTO reply_modes VALUES (?, ?, ?)"
                    " ON CONFLICT(account_id, session_id) DO UPDATE SET mode = excluded.mode",
                    (*scope, mode),
                )

    def save(
        self, account_id: str, session_id: str, text: str, *, source_server_id: str | None = None
    ) -> Draft:
        scope = (_scope_id(account_id), _scope_id(session_id))
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_DRAFT_TEXT or "\0" in text:
            raise ValueError("invalid reply draft text")
        _validate_source_server_id(source_server_id)
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            now = self._clock()
            conn.execute("DELETE FROM reply_drafts WHERE expires_at <= ?", (now,))
            total = conn.execute("SELECT count(*) FROM reply_drafts").fetchone()[0]
            count = conn.execute(
                "SELECT count(*) FROM reply_drafts WHERE account_id = ? AND session_id = ?", scope
            ).fetchone()[0]
            if count >= MAX_PENDING_PER_SESSION or total >= MAX_PENDING_TOTAL:
                raise OverflowError("too many pending reply drafts")
            draft = Draft(secrets.token_hex(16), *scope, text, now, now + DRAFT_TTL, source_server_id)
            conn.execute(
                "INSERT INTO reply_drafts (draft_id, account_id, session_id, text, created_at,"
                " expires_at, source_server_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (draft.draft_id, *scope, text, draft.created_at, draft.expires_at, source_server_id),
            )
        return draft

    def pending(self, account_id: str, session_id: str) -> list[Draft]:
        scope = (_scope_id(account_id), _scope_id(session_id))
        now = self._clock()
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT draft_id, account_id, session_id, text, created_at, expires_at, source_server_id"
                " FROM reply_drafts WHERE account_id = ? AND session_id = ?"
                " AND created_at <= ? AND expires_at > ? ORDER BY created_at DESC, draft_id DESC",
                (*scope, now, now),
            ).fetchall()
        return [Draft(*row) for row in rows]

    def claim(
        self, account_id: str, session_id: str, draft_id: str, *,
        actor_id: str, owner_id: str, confirmed: bool,
    ) -> Draft | None:
        """Consume a draft using the exact account and session, before send."""
        _authorize_owner(actor_id, owner_id)
        if confirmed is not True:
            raise PermissionError("explicit send confirmation required")
        scope = (_scope_id(account_id), _scope_id(session_id))
        if not isinstance(draft_id, str) or len(draft_id) != 32 or any(c not in "0123456789abcdef" for c in draft_id):
            raise ValueError("invalid draft ID")
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            now = self._clock()
            row = conn.execute(
                "SELECT draft_id, account_id, session_id, text, created_at, expires_at, source_server_id"
                " FROM reply_drafts WHERE draft_id = ? AND account_id = ? AND session_id = ?"
                " AND created_at <= ? AND expires_at > ?",
                (draft_id, *scope, now, now),
            ).fetchone()
            if row is None:
                return None
            conn.execute("DELETE FROM reply_drafts WHERE draft_id = ?", (draft_id,))
        return Draft(*row)
