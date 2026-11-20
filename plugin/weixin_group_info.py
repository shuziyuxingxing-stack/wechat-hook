"""Read one account's explicitly supplied group snapshot for AstrBot queries."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Literal


MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
MAX_GROUPS = 64
MAX_MEMBERS = 1_000
MAX_PAGE = 100
_ID = re.compile(r'[!-~]{1,256}')
_UNSAFE_TEXT = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff]')


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError('invalid group snapshot identifier')
    return value


def _text(value: object, limit: int, *, multiline: bool = False) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= limit or not value.strip() or
            _UNSAFE_TEXT.search(value) or (not multiline and any(c in value for c in '\t\r\n'))):
        raise ValueError('invalid group snapshot text')
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate field in group snapshot')
        result[key] = value
    return result


def _load_snapshot(path: Path) -> dict:
    if not path.is_absolute() or not path.is_file():
        raise ValueError('group snapshot must be an absolute file path')
    with path.open('rb') as file:
        data = file.read(MAX_SNAPSHOT_BYTES + 1)
    if len(data) > MAX_SNAPSHOT_BYTES:
        raise ValueError('group snapshot is too large')
    try:
        snapshot = json.loads(data.decode('utf-8-sig'), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise ValueError('invalid group snapshot JSON') from None
    if not isinstance(snapshot, dict):
        raise ValueError('invalid group snapshot')
    return snapshot


def _capture_time(value: object) -> str:
    try:
        stamp = datetime.fromisoformat(_text(value, 48))
    except ValueError:
        raise ValueError('group snapshot capture time must be ISO 8601 with a time zone') from None
    if stamp.tzinfo is None:
        raise ValueError('group snapshot capture time must include a time zone')
    return stamp.isoformat()


def _validated_groups(value: object) -> dict[str, dict]:
    if not isinstance(value, list) or len(value) > MAX_GROUPS:
        raise ValueError('invalid group snapshot list')
    groups = {}
    for group in value:
        if not isinstance(group, dict):
            raise ValueError('invalid group snapshot entry')
        group_id = _identifier(group.get('id'))
        if not group_id.endswith('@chatroom') or group_id in groups:
            raise ValueError('invalid or duplicate group ID')
        name = _text(group.get('name'), 256)
        members_complete = group.get('members_complete')
        announcement_complete = group.get('announcement_complete')
        if type(members_complete) is not bool or type(announcement_complete) is not bool:
            raise ValueError('group snapshot requires explicit completeness values')
        members = group.get('members')
        if not isinstance(members, list) or len(members) > MAX_MEMBERS:
            raise ValueError('invalid group snapshot member list')
        by_id = {}
        for member in members:
            if not isinstance(member, dict):
                raise ValueError('invalid group snapshot member')
            member_id = _identifier(member.get('id'))
            if member_id in by_id:
                raise ValueError('duplicate member ID in group snapshot')
            by_id[member_id] = {'id': member_id, 'name': _text(member.get('name'), 256)}
        if 'announcement' not in group:
            raise ValueError('group snapshot requires an announcement field')
        announcement = group['announcement']
        groups[group_id] = {
            'group_id': group_id,
            'group_name': name,
            'members': list(by_id.values()),
            'members_complete': members_complete,
            'announcement': None if announcement is None else _text(announcement, 8_000, multiline=True),
            'announcement_complete': announcement_complete,
        }
    return groups


def query_group_info(path: Path, account_id: str, group: str,
                     section: Literal['members', 'announcement'], *, limit: int = 50,
                     offset: int = 0) -> dict | None:
    """Query an exact group ID or unique exact name in the configured account's snapshot.

    ``account_id`` comes from the caller's validated account context. The
    snapshot supplies its own source, capture timestamp, and completeness.
    A null announcement is only evidence of no announcement when complete.
    """
    _identifier(account_id)
    selector = _text(group, 256)
    if (section not in ('members', 'announcement') or type(limit) is not int or
            type(offset) is not int or not 1 <= limit <= MAX_PAGE or not 0 <= offset <= MAX_MEMBERS):
        raise ValueError('invalid group snapshot query')
    snapshot = _load_snapshot(Path(path))
    if snapshot.get('account_id') != account_id:
        raise ValueError('group snapshot account does not match the current account')
    source = _text(snapshot.get('source'), 128)
    captured_at = _capture_time(snapshot.get('captured_at'))
    groups = _validated_groups(snapshot.get('groups'))
    selected = groups.get(selector)
    if selected is None:
        matches = [entry for entry in groups.values() if entry['group_name'] == selector]
        if len(matches) > 1:
            raise ValueError('group snapshot name is ambiguous; use its exact ID')
        if not matches:
            return None
        selected = matches[0]
    result = {
        'source': source,
        'captured_at': captured_at,
        'group_id': selected['group_id'],
        'group_name': selected['group_name'],
        'complete': selected[f'{section}_complete'],
    }
    if section == 'announcement':
        return {**result, 'announcement': selected['announcement']}
    members = selected['members']
    end = offset + limit
    return {**result, 'members': members[offset:end], 'total_captured': len(members),
            'next_offset': end if end < len(members) else None}
