"""Search text observed by the Weixin adapter within one account and chat."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import time


RETENTION_SECONDS = 30 * 86400
MAX_PER_CHAT = 500
MAX_TOTAL = 5_000
MAX_TEXT = 2_000
MAX_SNIPPET = 600
MAX_RESULT = 30
MAX_TIMESTAMP = 253_402_300_799
_SCOPE = 'platform_id = ? AND account_id = ? AND chat_kind = ? AND session_id = ?'
_SCHEMA = '''
CREATE TABLE IF NOT EXISTS observed_messages (
    platform_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    chat_kind INTEGER NOT NULL CHECK(chat_kind IN (0, 1)),
    session_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    sender_name TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    message_text TEXT NOT NULL,
    text_truncated INTEGER NOT NULL CHECK(text_truncated IN (0, 1)),
    PRIMARY KEY(platform_id, account_id, chat_kind, session_id, message_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS observed_messages_chat_time
    ON observed_messages(platform_id, account_id, chat_kind, session_id, timestamp DESC, message_id DESC);
CREATE INDEX IF NOT EXISTS observed_messages_time ON observed_messages(timestamp);
'''


def _id(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 512 or value != value.strip():
        raise ValueError('invalid history identifier')
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError('invalid history identifier')
    return value


def _timestamp(value: int) -> int:
    if type(value) is not int or not 0 <= value <= MAX_TIMESTAMP:
        raise ValueError('history timestamps must be Unix seconds')
    return value


def _filter(value: str) -> str:
    if not isinstance(value, str) or len(value) > 128 or any(ord(char) < 32 for char in value):
        raise ValueError('invalid history search filter')
    return value.strip().casefold()


def _snippet(text: str, keyword: str) -> str:
    if len(text) <= MAX_SNIPPET or not keyword:
        return text[:MAX_SNIPPET]
    folded_index = text.casefold().find(keyword)
    # Casefold can expand a character (for example, ß becomes ss). Convert
    # the match location back to the original text before slicing.
    for index, char in enumerate(text):
        folded_index -= len(char.casefold())
        if folded_index < 0:
            start = max(0, min(index - 100, len(text) - MAX_SNIPPET))
            return text[start:start + MAX_SNIPPET]
    return text[:MAX_SNIPPET]


@dataclass(frozen=True)
class HistoryScope:
    """Use IDs from the current validated DesktopEvent, never user/model input."""

    platform_id: str
    self_id: str
    session_id: str
    group: bool

    def __post_init__(self):
        for value in (self.platform_id, self.self_id, self.session_id):
            _id(value)
        if type(self.group) is not bool:
            raise ValueError('invalid history chat kind')

    def values(self) -> tuple[str, str, int, str]:
        return self.platform_id, self.self_id, int(self.group), self.session_id


class WeixinHistoryStore:
    """Bounded local store for an enabled chat. It does not turn on recording."""

    def __init__(self, database: Path, *, clock=time.time):
        path = Path(database)
        if not path.is_absolute() or path.is_symlink():
            raise ValueError('history database must be an absolute local file path')
        self._path = path
        self._clock = clock
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with closing(self._connection()) as connection, connection:
            connection.executescript(_SCHEMA)

    def _connection(self):
        connection = sqlite3.connect(self._path, timeout=3)
        connection.row_factory = sqlite3.Row
        connection.create_function('history_casefold', 1, str.casefold, deterministic=True)
        return connection

    def record(self, scope: HistoryScope, message_id: str, sender_id: str,
               sender_name: str, timestamp: int, text: str) -> bool:
        """Store the first seen version of one inbound text message."""
        _id(message_id)
        _id(sender_id)
        _timestamp(timestamp)
        if not isinstance(text, str) or '\0' in text or not text.strip():
            return False
        now = int(self._clock())
        cutoff = now - RETENTION_SECONDS
        if not cutoff <= timestamp <= now + 300:
            return False
        if not isinstance(sender_name, str) or '\0' in sender_name:
            sender_name = ''
        scope_values = scope.values()
        with closing(self._connection()) as connection, connection:
            connection.execute('DELETE FROM observed_messages WHERE timestamp < ?', (cutoff,))
            cursor = connection.execute(
                'INSERT OR IGNORE INTO observed_messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (*scope_values, message_id, sender_id, sender_name[:256], timestamp,
                 text[:MAX_TEXT], int(len(text) > MAX_TEXT)),
            )
            if not cursor.rowcount:
                return False
            connection.execute(
                f'''DELETE FROM observed_messages WHERE {_SCOPE} AND message_id IN (
                        SELECT message_id FROM observed_messages WHERE {_SCOPE}
                        ORDER BY timestamp DESC, message_id DESC LIMIT -1 OFFSET ?
                    )''',
                (*scope_values, *scope_values, MAX_PER_CHAT),
            )
            connection.execute(
                '''DELETE FROM observed_messages WHERE
                    (platform_id, account_id, chat_kind, session_id, message_id) IN (
                        SELECT platform_id, account_id, chat_kind, session_id, message_id
                        FROM observed_messages
                        ORDER BY timestamp DESC, platform_id, account_id, chat_kind, session_id, message_id
                        LIMIT -1 OFFSET ?
                    )''',
                (MAX_TOTAL,),
            )
        return True

    def search(self, scope: HistoryScope, *, keyword: str = '', sender: str = '',
               start: int | None = None, end: int | None = None, limit: int = 20) -> dict:
        """Return newest matches from the current chat's retained inbound text."""
        keyword = _filter(keyword)
        sender = _filter(sender)
        if type(limit) is not int or not 1 <= limit <= MAX_RESULT:
            raise ValueError('invalid history result limit')
        if start is not None:
            _timestamp(start)
        if end is not None:
            _timestamp(end)
        if start is not None and end is not None and start > end:
            raise ValueError('invalid history time range')
        cutoff = int(self._clock()) - RETENTION_SECONDS
        params: list[str | int] = [*scope.values(), max(start or 0, cutoff)]
        where = [_SCOPE, 'timestamp >= ?']
        if end is not None:
            where.append('timestamp <= ?')
            params.append(end)
        if keyword:
            where.append('instr(history_casefold(message_text), ?) > 0')
            params.append(keyword)
        if sender:
            where.append('(instr(history_casefold(sender_id), ?) > 0 OR instr(history_casefold(sender_name), ?) > 0)')
            params.extend((sender, sender))
        params.append(limit + 1)
        with closing(self._connection()) as connection:
            records = connection.execute(
                'SELECT message_id, sender_id, sender_name, timestamp, message_text, text_truncated '
                f'FROM observed_messages WHERE {" AND ".join(where)} '
                'ORDER BY timestamp DESC, message_id DESC LIMIT ?', params,
            ).fetchall()
        matches = []
        for row in records[:limit]:
            text = row['message_text']
            matches.append({
                'message_id': row['message_id'],
                'sender_id': row['sender_id'],
                'sender_name': row['sender_name'],
                'timestamp': row['timestamp'],
                'text': _snippet(text, keyword),
                'text_truncated': bool(row['text_truncated']) or len(text) > MAX_SNIPPET,
            })
        return {
            'scope': 'recorded_incoming_text_in_current_chat',
            'complete': False,
            'retention_days': RETENTION_SECONDS // 86400,
            'has_more_matches': len(records) > limit,
            'matches': matches,
        }
