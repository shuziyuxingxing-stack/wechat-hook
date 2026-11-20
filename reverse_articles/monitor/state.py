"""Offline, transactional checkpoints and an explicit notification outbox.

This accepts observations from a future trusted, account-bound source; it does
not authenticate accounts, discover articles, make requests or deliver messages.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import re
import sqlite3
import uuid
from itertools import islice
from typing import Iterable

MAX_BATCH = 200
MAX_PAGES = 1000
MAX_CURSOR = 4096
MAX_ID = 512
MAX_TITLE = 1024
MAX_URL = 8192
_REASON = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class StateError(ValueError):
    """Invalid, stale, or conflicting checkpoint operation."""


@dataclass(frozen=True)
class Observation:
    """A source-asserted stable article identity; absent means not deduplicable."""
    stable_id: str | None
    title: str = ""
    url: str = ""


def _string(value: object, maximum: int, *, empty: bool = False) -> str:
    if (not isinstance(value, str) or len(value) > maximum or '\0' in value or
            (not empty and (not value or not value.strip()))):
        raise StateError('invalid string')
    return value


def _scope(account: object, publisher: object) -> tuple[str, str]:
    return _string(account, MAX_ID), _string(publisher, MAX_ID)


class MonitorState:
    """A local SQLite outbox with atomic page persistence and explicit ACKs.

    Callers must use independently authenticated account/publisher IDs, supply
    identifiers whose stability is established by the source, and store the DB
    in a user-restricted directory. Source-provided text is untrusted data.
    """
    def __init__(self, path: str | Path):
        self._db = sqlite3.connect(str(path), timeout=5, isolation_level=None)
        self._db.execute('PRAGMA foreign_keys = ON')
        self._db.execute('PRAGMA synchronous = FULL')
        self._db.execute('PRAGMA journal_mode = DELETE')
        self._db.executescript('''
        CREATE TABLE IF NOT EXISTS scans (
          account TEXT NOT NULL, publisher TEXT NOT NULL, scan_id TEXT NOT NULL,
          cursor TEXT, status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
          coverage_verified INTEGER NOT NULL, pages INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(account, publisher)
        );
        CREATE TABLE IF NOT EXISTS articles (
          account TEXT NOT NULL, publisher TEXT NOT NULL, stable_id TEXT NOT NULL,
          title TEXT NOT NULL, url TEXT NOT NULL,
          first_seen INTEGER NOT NULL DEFAULT (unixepoch()),
          acknowledged INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(account, publisher, stable_id)
        );
        CREATE TABLE IF NOT EXISTS scan_cursors (
          account TEXT NOT NULL, publisher TEXT NOT NULL, scan_id TEXT NOT NULL, cursor TEXT NOT NULL,
          PRIMARY KEY(account, publisher, scan_id, cursor)
        );
        ''')

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> 'MonitorState':
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    @contextmanager
    def _transaction(self):
        self._db.execute('BEGIN IMMEDIATE')
        try:
            yield
        except BaseException:
            self._db.execute('ROLLBACK')
            raise
        else:
            self._db.execute('COMMIT')

    def start_scan(self, account: str, publisher: str, *,
                   coverage_verified: bool = False) -> str:
        """Start only after the previous scan finishes or is abandoned.

        coverage_verified is an assertion from a trusted adapter, not something
        this component can infer from reaching the end of a response.
        """
        account, publisher = _scope(account, publisher)
        if type(coverage_verified) is not bool:
            raise StateError('invalid coverage assertion')
        scan_id = uuid.uuid4().hex
        with self._transaction():
            old = self._db.execute('SELECT status FROM scans WHERE account=? AND publisher=?',
                                   (account, publisher)).fetchone()
            if old and old[0] in ('running', 'partial', 'failed'):
                raise StateError('scan already active')
            self._db.execute('''INSERT INTO scans(account,publisher,scan_id,cursor,status,reason,coverage_verified,pages)
                VALUES(?,?,?,'','running','',?,0)
                ON CONFLICT(account,publisher) DO UPDATE SET scan_id=excluded.scan_id,
                cursor='',status='running',reason='',coverage_verified=excluded.coverage_verified,pages=0''',
                (account, publisher, scan_id, int(coverage_verified)))
            self._db.execute('DELETE FROM scan_cursors WHERE account=? AND publisher=?', (account, publisher))
            self._db.execute('INSERT INTO scan_cursors(account,publisher,scan_id,cursor) VALUES(?,?,?,?)',
                             (account, publisher, scan_id, ''))
        return scan_id

    def _current(self, account: str, publisher: str, scan_id: str):
        row = self._db.execute('SELECT scan_id,cursor,status,reason,coverage_verified,pages FROM scans '
                               'WHERE account=? AND publisher=?', (account, publisher)).fetchone()
        if not row or row[0] != scan_id or row[2] not in ('running', 'partial', 'failed'):
            raise StateError('scan is stale or not active')
        return row

    def append_page(self, account: str, publisher: str, scan_id: str, *, cursor: str,
                    next_cursor: str | None, observations: Iterable[Observation],
                    page_complete: bool) -> dict:
        """Atomically persist known articles; advance only a complete safe page.

        Incomplete pages or items without stable IDs remain retryable at the same
        cursor, including after restart. Stable entries are still queued once.
        """
        account, publisher = _scope(account, publisher)
        scan_id = _string(scan_id, 64)
        cursor = _string(cursor, MAX_CURSOR, empty=True)
        if type(page_complete) is not bool:
            raise StateError('invalid page completion flag')
        if next_cursor is not None:
            next_cursor = _string(next_cursor, MAX_CURSOR)
            if next_cursor == cursor:
                raise StateError('cursor does not advance')
        items = list(islice(observations, MAX_BATCH + 1))
        if len(items) > MAX_BATCH:
            raise StateError('page exceeds limit')
        checked: list[tuple[str, str, str]] = []
        missing = 0
        for item in items:
            if not isinstance(item, Observation):
                raise StateError('invalid observation')
            title, url = _string(item.title, MAX_TITLE, empty=True), _string(item.url, MAX_URL, empty=True)
            if item.stable_id is None or item.stable_id == '':
                missing += 1
            else:
                checked.append((_string(item.stable_id, MAX_ID), title, url))
        with self._transaction():
            current = self._current(account, publisher, scan_id)
            if current[1] != cursor:
                raise StateError('stale cursor')
            if current[5] >= MAX_PAGES:
                raise StateError('page limit exceeded; abandon or resume using a verified source')
            if (page_complete and not missing and next_cursor is not None and
                    self._db.execute('SELECT 1 FROM scan_cursors WHERE account=? AND publisher=? '
                        'AND scan_id=? AND cursor=?', (account, publisher, scan_id, next_cursor)).fetchone()):
                raise StateError('cursor cycle')
            added = 0
            for stable_id, title, url in checked:
                added += self._db.execute('''INSERT OR IGNORE INTO articles
                    (account,publisher,stable_id,title,url) VALUES (?,?,?,?,?)''',
                    (account, publisher, stable_id, title, url)).rowcount
            if not page_complete or missing:
                status = 'partial'
                reason = 'missing_stable_id' if missing else 'incomplete_page'
                self._db.execute('UPDATE scans SET status=?,reason=? WHERE account=? AND publisher=?',
                                 (status, reason, account, publisher))
            elif next_cursor is not None:
                status, reason = 'running', ''
                self._db.execute('INSERT INTO scan_cursors(account,publisher,scan_id,cursor) VALUES(?,?,?,?)',
                                 (account, publisher, scan_id, next_cursor))
                self._db.execute('UPDATE scans SET status=?,reason=?,cursor=?,pages=pages+1 '
                                 'WHERE account=? AND publisher=?',
                                 (status, reason, next_cursor, account, publisher))
            else:
                status = 'complete' if current[4] else 'incomplete'
                reason = '' if current[4] else 'coverage_unverified'
                self._db.execute('UPDATE scans SET status=?,reason=?,cursor=NULL,pages=pages+1 '
                                 'WHERE account=? AND publisher=?',
                                 (status, reason, account, publisher))
        return {'status': status, 'reason': reason, 'added': added, 'missing_stable_id': missing,
                'cursor_advanced': page_complete and missing == 0,
                'pagination_complete': page_complete and missing == 0 and next_cursor is None}

    def fail_page(self, account: str, publisher: str, scan_id: str, *, cursor: str, reason: str) -> None:
        """Record a source failure without advancing the persisted checkpoint."""
        account, publisher = _scope(account, publisher)
        _string(scan_id, 64)
        _string(cursor, MAX_CURSOR, empty=True)
        if not isinstance(reason, str) or not _REASON.fullmatch(reason):
            raise StateError('invalid reason code')
        with self._transaction():
            current = self._current(account, publisher, scan_id)
            if current[1] != cursor:
                raise StateError('stale cursor')
            self._db.execute("UPDATE scans SET status='failed',reason=? WHERE account=? AND publisher=?",
                             (reason, account, publisher))

    def abandon_scan(self, account: str, publisher: str, scan_id: str) -> None:
        """Finish an unrecoverable scan without claiming complete coverage."""
        account, publisher = _scope(account, publisher)
        with self._transaction():
            self._current(account, publisher, _string(scan_id, 64))
            self._db.execute("UPDATE scans SET status='incomplete',reason='abandoned' WHERE account=? AND publisher=?",
                             (account, publisher))

    def snapshot(self, account: str, publisher: str) -> dict | None:
        account, publisher = _scope(account, publisher)
        row = self._db.execute('SELECT scan_id,cursor,status,reason,coverage_verified,pages FROM scans '
                               'WHERE account=? AND publisher=?', (account, publisher)).fetchone()
        if not row:
            return None
        result = dict(zip(('scan_id', 'cursor', 'status', 'reason', 'coverage_verified', 'pages'), row))
        result['coverage_verified'] = bool(result['coverage_verified'])
        return result

    def pending(self, account: str, publisher: str, *, limit: int = 50) -> list[dict]:
        """Read pending entries without changing delivery or acknowledgement state."""
        account, publisher = _scope(account, publisher)
        if type(limit) is not int or not 1 <= limit <= MAX_BATCH:
            raise StateError('invalid limit')
        rows = self._db.execute('SELECT stable_id,title,url,first_seen FROM articles '
            'WHERE account=? AND publisher=? AND acknowledged=0 ORDER BY rowid LIMIT ?',
            (account, publisher, limit)).fetchall()
        return [dict(zip(('stable_id', 'title', 'url', 'first_seen'), row)) for row in rows]

    def acknowledge(self, account: str, publisher: str, stable_ids: Iterable[str]) -> int:
        """ACK after the caller has established successful delivery, scoped to a publisher."""
        account, publisher = _scope(account, publisher)
        ids = list(islice(stable_ids, MAX_BATCH + 1))
        if len(ids) > MAX_BATCH:
            raise StateError('acknowledgement exceeds limit')
        ids = [_string(item, MAX_ID) for item in ids]
        count = 0
        with self._transaction():
            for stable_id in ids:
                count += self._db.execute('UPDATE articles SET acknowledged=1 WHERE account=? AND publisher=? '
                    'AND stable_id=? AND acknowledged=0', (account, publisher, stable_id)).rowcount
        return count
