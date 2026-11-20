import tempfile
import unittest
import sqlite3
from pathlib import Path

from state import MonitorState, Observation, StateError


class MonitorStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'state.sqlite3'
        self.store = MonitorState(self.path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def reopen(self):
        self.store.close()
        self.store = MonitorState(self.path)

    def test_restart_retains_pending_and_ack_removes_it(self):
        scan = self.store.start_scan('a', 'p')
        self.store.append_page('a', 'p', scan, cursor='', next_cursor=None,
            observations=[Observation('id1', 'title')], page_complete=True)
        self.reopen()
        self.assertEqual([x['stable_id'] for x in self.store.pending('a', 'p')], ['id1'])
        self.assertEqual(self.store.acknowledge('a', 'p', ['id1']), 1)
        self.reopen()
        self.assertEqual(self.store.pending('a', 'p'), [])
        again = self.store.start_scan('a', 'p')
        r = self.store.append_page('a', 'p', again, cursor='', next_cursor=None,
            observations=[Observation('id1', 'changed title')], page_complete=True)
        self.assertEqual(r['added'], 0)
        self.assertEqual(self.store.pending('a', 'p'), [])

    def test_partial_page_and_missing_id_never_advance(self):
        scan = self.store.start_scan('a', 'p', coverage_verified=True)
        r = self.store.append_page('a', 'p', scan, cursor='', next_cursor='next',
            observations=[Observation('id1'), Observation(None, 'unidentifiable')], page_complete=True)
        self.assertFalse(r['cursor_advanced'])
        self.assertEqual(self.store.snapshot('a', 'p')['cursor'], '')
        self.assertEqual(self.store.snapshot('a', 'p')['status'], 'partial')
        self.reopen()
        r = self.store.append_page('a', 'p', scan, cursor='', next_cursor='next',
            observations=[Observation('id1'), Observation('id2')], page_complete=True)
        self.assertEqual(r['added'], 1)
        self.assertEqual(self.store.snapshot('a', 'p')['cursor'], 'next')
        self.assertEqual(len(self.store.pending('a', 'p')), 2)
        self.store.fail_page('a', 'p', scan, cursor='next', reason='network_failure')
        self.reopen()
        self.assertEqual(self.store.snapshot('a', 'p')['status'], 'failed')
        self.assertEqual(self.store.snapshot('a', 'p')['cursor'], 'next')
        r = self.store.append_page('a', 'p', scan, cursor='next', next_cursor=None,
            observations=[Observation('id3')], page_complete=True)
        self.assertEqual(r['status'], 'complete')

    def test_incomplete_page_can_record_known_items_and_retry(self):
        scan = self.store.start_scan('a', 'p')
        result = self.store.append_page('a', 'p', scan, cursor='', next_cursor=None,
            observations=[Observation('id1')], page_complete=False)
        self.assertFalse(result['pagination_complete'])
        self.assertEqual(self.store.snapshot('a', 'p')['status'], 'partial')
        self.assertEqual(self.store.snapshot('a', 'p')['cursor'], '')
        self.assertEqual(self.store.pending('a', 'p')[0]['stable_id'], 'id1')
        result = self.store.append_page('a', 'p', scan, cursor='', next_cursor=None,
            observations=[Observation('id1'), Observation('id2')], page_complete=True)
        self.assertEqual(result['added'], 1)
        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(result['reason'], 'coverage_unverified')

    def test_account_publisher_and_acknowledgements_are_isolated(self):
        for account, publisher in [('a', 'p'), ('a', 'q'), ('b', 'p')]:
            scan = self.store.start_scan(account, publisher)
            self.store.append_page(account, publisher, scan, cursor='', next_cursor=None,
                observations=[Observation('shared')], page_complete=True)
        self.store.acknowledge('a', 'p', ['shared'])
        self.assertEqual(self.store.pending('a', 'p'), [])
        self.assertEqual(len(self.store.pending('a', 'q')), 1)
        self.assertEqual(len(self.store.pending('b', 'p')), 1)

    def test_stale_cursor_and_scan_do_not_commit_items(self):
        scan = self.store.start_scan('a', 'p')
        self.store.append_page('a', 'p', scan, cursor='', next_cursor='second',
            observations=[Observation('id1')], page_complete=True)
        with self.assertRaises(StateError):
            self.store.append_page('a', 'p', scan, cursor='', next_cursor=None,
                observations=[Observation('must_not_commit')], page_complete=True)
        with self.assertRaises(StateError):
            self.store.append_page('a', 'p', 'wrong', cursor='second', next_cursor=None,
                observations=[Observation('also_not_commit')], page_complete=True)
        self.assertEqual([x['stable_id'] for x in self.store.pending('a', 'p')], ['id1'])
        self.assertEqual(self.store.snapshot('a', 'p')['cursor'], 'second')

    def test_abandon_and_replay_without_losing_pending(self):
        scan = self.store.start_scan('a', 'p')
        self.store.append_page('a', 'p', scan, cursor='', next_cursor=None,
            observations=[Observation('id1'), Observation(None)], page_complete=True)
        with self.assertRaises(StateError):
            self.store.start_scan('a', 'p')
        self.store.abandon_scan('a', 'p', scan)
        self.assertEqual(self.store.snapshot('a', 'p')['status'], 'incomplete')
        scan2 = self.store.start_scan('a', 'p')
        self.store.append_page('a', 'p', scan2, cursor='', next_cursor=None,
            observations=[Observation('id1'), Observation('id2')], page_complete=True)
        self.assertEqual(len(self.store.pending('a', 'p')), 2)

    def test_bad_input_rejected_before_transaction(self):
        scan = self.store.start_scan('a', 'p')
        for items in ([Observation('valid'), Observation('')]*201,
                      [Observation('valid'), Observation('bad\0')],
                      [Observation('valid'), {'stable_id': 'not-observation'}]):
            with self.assertRaises(StateError):
                self.store.append_page('a', 'p', scan, cursor='', next_cursor=None,
                                       observations=items, page_complete=True)
        self.assertEqual(self.store.pending('a', 'p'), [])
        self.assertEqual(self.store.snapshot('a', 'p')['cursor'], '')

    def test_cursor_cycles_rejected_before_article_is_inserted(self):
        scan = self.store.start_scan('a', 'p')
        self.store.append_page('a', 'p', scan, cursor='', next_cursor='one',
            observations=[Observation('first')], page_complete=True)
        self.store.append_page('a', 'p', scan, cursor='one', next_cursor='two',
            observations=[Observation('second')], page_complete=True)
        self.reopen()
        with self.assertRaises(StateError):
            self.store.append_page('a', 'p', scan, cursor='two', next_cursor='one',
                observations=[Observation('never_inserted')], page_complete=True)
        self.assertEqual(self.store.snapshot('a', 'p')['cursor'], 'two')
        self.assertEqual([x['stable_id'] for x in self.store.pending('a', 'p')], ['first', 'second'])

    def test_generator_is_bounded_and_no_rows_are_committed(self):
        scan = self.store.start_scan('a', 'p')
        consumed = []
        def source():
            for index in range(10000):
                consumed.append(index)
                yield Observation(str(index))
        with self.assertRaises(StateError):
            self.store.append_page('a', 'p', scan, cursor='', next_cursor=None,
                                   observations=source(), page_complete=True)
        self.assertEqual(len(consumed), 201)
        self.assertEqual(self.store.pending('a', 'p'), [])

    def test_database_failure_rolls_back_article_and_checkpoint(self):
        scan = self.store.start_scan('a', 'p')
        self.store._db.execute('''CREATE TRIGGER fail_test BEFORE INSERT ON articles
            WHEN NEW.stable_id='bad' BEGIN SELECT RAISE(ABORT, 'simulated'); END''')
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.append_page('a', 'p', scan, cursor='', next_cursor='next',
                observations=[Observation('first'), Observation('bad')], page_complete=True)
        self.reopen()
        self.assertEqual(self.store.snapshot('a', 'p')['cursor'], '')
        self.assertEqual(self.store.pending('a', 'p'), [])


if __name__ == '__main__':
    unittest.main()
