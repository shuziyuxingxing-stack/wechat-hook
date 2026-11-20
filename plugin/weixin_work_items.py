"""Local, explicitly confirmed work items extracted from Weixin messages."""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date
import json
from pathlib import Path
import re
import secrets
import sqlite3
from threading import Lock
import time
import unicodedata


_PROPOSAL_ID = re.compile(r'^[0-9a-f]{20}$')
_MAX_PENDING = 256
_MAX_PENDING_PER_SCOPE = 8
_PENDING_SECONDS = 30 * 60
_MAX_ITEMS = 20


def _line(value: object, *, limit: int, required: bool = False) -> str:
    if value is None and not required:
        return ''
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError('invalid work item field')
    if any(unicodedata.category(character) in ('Cc', 'Cs', 'Zl', 'Zp') for character in value):
        raise ValueError('work item fields must be single-line text')
    value = value.strip()
    if required and not value:
        raise ValueError('a required work item field is empty')
    return value


@dataclass(frozen=True, slots=True)
class WorkScope:
    account: str
    session: str
    sender: str

    def __post_init__(self) -> None:
        for value in self.as_key():
            if _line(value, limit=512, required=True) != value:
                raise ValueError('invalid work item scope')

    def as_key(self) -> tuple[str, str, str]:
        return self.account, self.session, self.sender


@dataclass(frozen=True, slots=True)
class WorkItem:
    title: str
    assignee: str
    due_date: str
    source: str


@dataclass(frozen=True, slots=True)
class WorkProposal:
    id: str
    items: tuple[WorkItem, ...]
    source: str


def _item(value: object, source: str) -> WorkItem:
    if not isinstance(value, dict) or set(value) - {'title', 'assignee', 'due_date'}:
        raise ValueError('work items support title, assignee and due_date')
    title = _line(value.get('title'), limit=240, required=True)
    assignee = _line(value.get('assignee'), limit=100)
    due_date = _line(value.get('due_date'), limit=10)
    if due_date:
        if date.fromisoformat(due_date).isoformat() != due_date:
            raise ValueError('due_date must be YYYY-MM-DD')
    return WorkItem(title, assignee, due_date, source)


class WorkItemStore:
    """Keep tentative proposals in memory and save confirmed items atomically.

    The caller must derive the scope and source from a verified event and call
    confirm only after an explicit user action. Methods are synchronous so the
    AstrBot adapter can use asyncio.to_thread for database operations.
    """

    def __init__(self, database: Path | str, *, clock: Callable[[], float] = time.monotonic):
        self._path = Path(database)
        self._clock = clock
        self._pending: OrderedDict[tuple[WorkScope, str], tuple[float, WorkProposal]] = OrderedDict()
        self._lock = Lock()

    def _expire(self, now: float) -> None:
        while self._pending and next(iter(self._pending.values()))[0] <= now:
            self._pending.popitem(last=False)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._path, timeout=5)
        try:
            with connection:
                connection.execute('''CREATE TABLE IF NOT EXISTS confirmed_proposal (
                    proposal_id TEXT PRIMARY KEY NOT NULL,
                    account TEXT NOT NULL,
                    session TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    source TEXT NOT NULL,
                    items_json TEXT NOT NULL
                )''')
                connection.execute('''CREATE INDEX IF NOT EXISTS work_scope_index
                    ON confirmed_proposal(account, session, sender)''')
                yield connection
        finally:
            connection.close()

    def propose(self, scope: WorkScope, items: list[dict], *, source: str) -> WorkProposal:
        if not isinstance(scope, WorkScope) or not isinstance(items, list) or not 1 <= len(items) <= _MAX_ITEMS:
            raise ValueError('a proposal requires a valid scope and 1–20 work items')
        source = _line(source, limit=512, required=True)
        proposal = WorkProposal(secrets.token_hex(10), tuple(_item(value, source) for value in items), source)
        with self._lock:
            now = self._clock()
            self._expire(now)
            same_scope = [key for key in self._pending if key[0] == scope]
            if len(same_scope) >= _MAX_PENDING_PER_SCOPE:
                del self._pending[same_scope[0]]
            if len(self._pending) >= _MAX_PENDING:
                self._pending.popitem(last=False)
            self._pending[scope, proposal.id] = now + _PENDING_SECONDS, proposal
        return proposal

    def confirm(self, scope: WorkScope, proposal_id: str) -> WorkProposal | None:
        if not isinstance(scope, WorkScope) or not isinstance(proposal_id, str) or not _PROPOSAL_ID.fullmatch(proposal_id):
            raise ValueError('invalid work proposal ID or scope')
        key = (scope, proposal_id)
        with self._lock:
            self._expire(self._clock())
            entry = self._pending.get(key)
            if entry is None and not self._path.is_file():
                return None
            with self._connection() as connection:
                if entry is not None:
                    proposal = entry[1]
                    items_json = json.dumps([asdict(item) for item in proposal.items], ensure_ascii=False)
                    connection.execute('INSERT OR IGNORE INTO confirmed_proposal VALUES (?,?,?,?,?,?)',
                                       (proposal.id, *scope.as_key(), proposal.source, items_json))
                row = connection.execute('''SELECT source, items_json FROM confirmed_proposal
                    WHERE proposal_id=? AND account=? AND session=? AND sender=?''',
                                         (proposal_id, *scope.as_key())).fetchone()
            if row is None:
                return None
            self._pending.pop(key, None)
            return WorkProposal(proposal_id, self._deserialize(row[1]), row[0])

    def list_items(self, scope: WorkScope, *, limit: int = 20) -> list[WorkItem]:
        if not isinstance(scope, WorkScope) or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('invalid work item scope or list limit')
        if not self._path.is_file():
            return []
        with self._connection() as connection:
            rows = connection.execute('''SELECT items_json FROM confirmed_proposal
                WHERE account=? AND session=? AND sender=? ORDER BY rowid DESC LIMIT ?''',
                                      (*scope.as_key(), limit))
            output = []
            for (items_json,) in rows:
                output.extend(self._deserialize(items_json)[:limit - len(output)])
                if len(output) >= limit:
                    break
        return output

    @staticmethod
    def _deserialize(items_json: str) -> tuple[WorkItem, ...]:
        return tuple(WorkItem(**value) for value in json.loads(items_json))
