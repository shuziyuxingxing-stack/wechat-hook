"""History behavior tested with a synthetic local database only."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import weixin_history_search as history


class WeixinHistoryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.file = Path(directory.name) / 'history.sqlite3'
        self.now = 1_700_000_000
        self.store = self.open_store()
        self.scope = history.HistoryScope('desktop', 'account-1', 'example@chatroom', True)

    def open_store(self):
        return history.WeixinHistoryStore(self.file, clock=lambda: self.now)

    def insert(self, key, text, *, offset=0, sender='member-1', name='测试人员', scope=None):
        return self.store.record(scope or self.scope, key, sender, name, self.now + offset, text)

    def test_filters_match_literal_unicode_and_inclusive_timestamp(self):
        self.insert('one', '解决 STRASSE_100% 并发送附件。', offset=-20)
        self.insert('two', '解决 STRASSEa100y', offset=-19)
        self.insert('three', '解决 Straße_100%', offset=-18, sender='member-2', name='其他人员')
        result = self.store.search(self.scope, keyword='straße_100%', sender='测试人员',
                                   start=self.now - 20, end=self.now - 20)
        self.assertEqual([row['message_id'] for row in result['matches']], ['one'])
        self.assertFalse(result['complete'])
        self.assertFalse(result['has_more_matches'])
        self.assertEqual(self.store.search(self.scope, keyword="' OR 1=1 --")['matches'], [])

    def test_isolation_and_duplicate_id_across_account_platform_and_chat(self):
        self.assertTrue(self.insert('id', '目标会话内容'))
        for different in (
            history.HistoryScope('desktop', 'account-2', 'example@chatroom', True),
            history.HistoryScope('desktop', 'account-1', 'other@chatroom', True),
            history.HistoryScope('another-platform', 'account-1', 'example@chatroom', True),
            history.HistoryScope('desktop', 'account-1', 'example@chatroom', False),
        ):
            self.assertTrue(self.insert('id', '隔离标记', scope=different))
        self.assertFalse(self.insert('id', '重复更新'))
        self.store = self.open_store()
        result = self.store.search(self.scope)['matches']
        self.assertEqual([row['text'] for row in result], ['目标会话内容'])

    def test_result_bounds_and_expiry(self):
        with patch.object(history, 'MAX_PER_CHAT', 2), patch.object(history, 'MAX_TOTAL', 3):
            self.insert('old', 'old', offset=-3)
            self.insert('middle', 'middle', offset=-2)
            self.insert('new', 'new', offset=-1)
            result = self.store.search(self.scope, limit=1)
            self.assertEqual([row['message_id'] for row in result['matches']], ['new'])
            self.assertTrue(result['has_more_matches'])
            for account in ('account-2', 'account-3'):
                self.insert('extra', account, scope=history.HistoryScope('desktop', account, 'session', False))
            self.assertEqual([row['message_id'] for row in self.store.search(self.scope)['matches']], ['new'])
        self.now += history.RETENTION_SECONDS
        self.assertFalse(self.store.search(self.scope)['matches'])
        self.assertFalse(self.insert('expired', 'late delivery', offset=-history.RETENTION_SECONDS - 1))

    def test_late_unicode_match_is_visible_in_bounded_snippet(self):
        self.insert('long', 'ß' * 700 + '目标词' + '尾' * 1000)
        hit = self.store.search(self.scope, keyword='目标词')['matches'][0]
        self.assertIn('目标词', hit['text'])
        self.assertEqual(len(hit['text']), history.MAX_SNIPPET)
        self.assertTrue(hit['text_truncated'])

    def test_invalid_input_and_nontext_are_rejected(self):
        self.assertFalse(self.insert('blank', '  '))
        self.assertFalse(self.insert('null', 'a\x00b'))
        with self.assertRaises(ValueError):
            self.store.search(self.scope, start=self.now, end=self.now - 1)
        with self.assertRaises(ValueError):
            self.store.search(self.scope, limit=True)
        with self.assertRaises(ValueError):
            history.HistoryScope('desktop', '', 'example@chatroom', True)


if __name__ == '__main__':
    unittest.main()
