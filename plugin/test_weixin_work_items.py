import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from weixin_work_items import WorkItemStore, WorkScope


class WorkItemsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / 'tasks.sqlite3'
        self.scope = WorkScope('account', 'direct-chat', 'owner')
        self.now = 10.0
        self.store = WorkItemStore(self.database, clock=lambda: self.now)

    def propose(self, items=None):
        return self.store.propose(self.scope, items or [
            {'title': '准备周报', 'assignee': '小林', 'due_date': '2026-11-27'},
            {'title': '核对会议时间'},
        ], source='msg-42')

    def test_proposal_has_no_disk_record_until_explicit_confirmation(self):
        proposal = self.propose()
        self.assertFalse(self.database.exists())
        self.assertEqual(self.store.list_items(self.scope), [])
        confirmed = self.store.confirm(self.scope, proposal.id)
        self.assertEqual(confirmed, proposal)
        self.assertEqual(confirmed.items[0].source, 'msg-42')
        reloaded = WorkItemStore(self.database)
        self.assertEqual(reloaded.confirm(self.scope, proposal.id), proposal)
        self.assertEqual(reloaded.list_items(self.scope), list(proposal.items))
        self.assertEqual(reloaded.list_items(self.scope, limit=1), [proposal.items[0]])

    def test_scope_mismatch_does_not_confirm_or_expose_items(self):
        proposal = self.propose()
        other_scopes = [
            WorkScope('other-account', 'direct-chat', 'owner'),
            WorkScope('account', 'other-chat', 'owner'),
            WorkScope('account', 'direct-chat', 'other-sender'),
        ]
        for other in other_scopes:
            self.assertIsNone(self.store.confirm(other, proposal.id))
        self.assertFalse(self.database.exists())
        self.store.confirm(self.scope, proposal.id)
        for other in other_scopes:
            self.assertIsNone(self.store.confirm(other, proposal.id))
            self.assertEqual(self.store.list_items(other), [])

    def test_expired_proposal_cannot_be_confirmed(self):
        proposal = self.propose()
        self.now += 31 * 60
        self.assertIsNone(self.store.confirm(self.scope, proposal.id))
        self.assertFalse(self.database.exists())

    def test_invalid_model_fields_reject_the_entire_proposal(self):
        for bad in (
            {'title': '不可用\n另一个事项'},
            {'title': '  '},
            {'title': '登记截止日期', 'due_date': '2026-02-30'},
            {'title': '登记截止日期', 'due_date': '20261127'},
            {'title': '模型不能伪造来源', 'source': 'another-message'},
        ):
            with self.subTest(item=bad), self.assertRaises(ValueError):
                self.propose([{'title': '有效项目'}, bad])
        self.assertFalse(self.database.exists())

    def test_concurrent_confirmation_records_one_proposal(self):
        proposal = self.propose()
        with ThreadPoolExecutor(max_workers=4) as pool:
            confirmations = list(pool.map(lambda _: self.store.confirm(self.scope, proposal.id), range(8)))
        self.assertEqual(confirmations, [proposal] * 8)
        self.assertEqual(self.store.list_items(self.scope), list(proposal.items))


if __name__ == '__main__':
    unittest.main()
