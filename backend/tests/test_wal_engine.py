"""Scenario knobs, page-image recovery, and legacy compatibility.

Run from backend/: ``python -m unittest discover -s tests``
"""
import unittest

from src.services.wal_engine import (freeze_page_image, recover, validate)

INITIAL = {'sample_A': 100, 'sample_B': 50}
RECORDS = [
    {'op': 'begin', 'tx': 'T1'},
    {'op': 'write', 'tx': 'T1', 'key': 'sample_A', 'value': 75},
    {'op': 'commit', 'tx': 'T1'},
    {'op': 'checkpoint'},
    {'op': 'begin', 'tx': 'T2'},
    {'op': 'write', 'tx': 'T2', 'key': 'sample_B', 'value': 40},
    {'op': 'begin', 'tx': 'T3'},
    {'op': 'write', 'tx': 'T3', 'key': 'sample_A', 'value': 60},
    {'op': 'commit', 'tx': 'T3'},
    {'op': 'write', 'tx': 'T2', 'key': 'sample_B', 'value': 35},
    {'op': 'commit', 'tx': 'T2'},
]
FINAL = {'sample_A': 60, 'sample_B': 50}


def run(**knobs):
    return recover(INITIAL, RECORDS, **knobs)


class LegacyScenarioTest(unittest.TestCase):
    def test_crash_after_only_is_no_force(self):
        result = run(crash_after=10)
        self.assertEqual(result['recovered'], FINAL)
        self.assertTrue(result['invariant_passed'])
        self.assertEqual(result['page_lsn'], {'sample_A': 4, 'sample_B': 4})
        self.assertEqual(result['log_flushed_through'], 10)

    def test_zero_and_full_point(self):
        self.assertEqual(run(crash_after=0)['recovered'], {'sample_A': 100, 'sample_B': 50})
        self.assertEqual(run(crash_after=11)['recovered'], {'sample_A': 60, 'sample_B': 35})

    def test_explicit_fields_equal_scenario(self):
        a = run(crash_after=10)
        b = run(executed_through=10, log_flushed_through=10)
        self.assertEqual(a['recovered'], b['recovered'])
        self.assertEqual(a['trace'], b['trace'])


class PageImageTest(unittest.TestCase):
    def test_uncommitted_page_flushed_is_undone(self):
        result = run(executed_through=10, log_flushed_through=10, page_flushes={'sample_B': 10})
        self.assertEqual(result['recovered'], FINAL)
        self.assertTrue(result['invariant_passed'])
        self.assertEqual(result['page_image']['sample_B'], 35)
        self.assertGreater(result['undo_applied_count'], 0)
        self.assertTrue(any(t['phase'] == 'undo_loser' and t['key'] == 'sample_B' for t in result['trace']))

    def test_committed_write_not_flushed_is_redone(self):
        result = run(executed_through=9, log_flushed_through=9, page_flushes={'sample_A': 4})
        self.assertEqual(result['recovered']['sample_A'], 60)
        self.assertTrue(any(t['phase'] == 'redo' and t['lsn'] == 8 for t in result['trace']))

    def test_page_written_back_after_abort(self):
        records = [
            {'op': 'begin', 'tx': 'A'},
            {'op': 'write', 'tx': 'A', 'key': 'k', 'value': 9},
            {'op': 'abort', 'tx': 'A'},
            {'op': 'begin', 'tx': 'B'},
            {'op': 'write', 'tx': 'B', 'key': 'k', 'value': 7},
            {'op': 'commit', 'tx': 'B'},
        ]
        result = recover({'k': 1}, records, executed_through=6, log_flushed_through=6, page_flushes={'k': 2})
        self.assertEqual(result['recovered'], {'k': 7})

    def test_same_key_multiple_writes(self):
        result = run(executed_through=10, log_flushed_through=10, page_flushes={'sample_B': 6})
        self.assertEqual(result['recovered'], FINAL)
        self.assertTrue(any(t['phase'] == 'redo' and t['lsn'] == 10 for t in result['trace']))
        self.assertTrue(any(t['phase'] == 'undo_loser' and t['lsn'] == 6 for t in result['trace']))

    def test_page_already_current_skips_redo(self):
        result = run(executed_through=11, log_flushed_through=11, page_flushes={'sample_B': 10})
        self.assertGreater(result['redo_skipped_count'], 0)
        self.assertTrue(any(t['phase'] == 'redo_skipped' and t['reason'] for t in result['trace']))

    def test_page_report_covers_every_key(self):
        result = run(executed_through=10, log_flushed_through=10, page_flushes={'sample_B': 10})
        pages = {p['key']: p for p in result['pages']}
        self.assertEqual(set(pages), set(INITIAL))
        for page in pages.values():
            self.assertEqual(page['before_recovery'], result['page_image'][page['key']])
            self.assertEqual(page['after_recovery'], result['recovered'][page['key']])


class DurableLogTest(unittest.TestCase):
    def test_unflushed_commit_is_lost_and_undone(self):
        result = run(executed_through=11, log_flushed_through=8)
        self.assertEqual(result['recovered'], {'sample_A': 75, 'sample_B': 50})
        self.assertIn('T3', result['losers'])
        self.assertEqual(len(result['lost_records']), 3)
        self.assertTrue(any(t['phase'] == 'lost' and t['lsn'] == 9 for t in result['trace']))
        self.assertTrue(result['invariant_passed'])

    def test_unflushed_write_is_lost(self):
        # LSN 10 (T2 writes B=35) is executed but not durable. T2 also never
        # reaches a durable commit, so its durable B=40 at LSN 6 is undone too.
        result = run(executed_through=10, log_flushed_through=9)
        self.assertEqual(result['recovered'], {'sample_A': 60, 'sample_B': 50})
        self.assertEqual(result['lost_records'][0]['lsn'], 10)
        self.assertTrue(any(t['phase'] == 'undo_loser' and t['lsn'] == 6 for t in result['trace']))


class WriteAheadRejectTest(unittest.TestCase):
    def test_flush_beyond_durable_log_rejected(self):
        with self.assertRaisesRegex(ValueError, '越过日志落盘位置'):
            run(executed_through=10, log_flushed_through=8, page_flushes={'sample_B': 9})

    def test_executed_checkpoint_beyond_durable_log_rejected(self):
        with self.assertRaisesRegex(ValueError, '检查点'):
            run(executed_through=10, log_flushed_through=3)

    def test_flush_point_after_execution_rejected(self):
        with self.assertRaisesRegex(ValueError, '必须在0到执行位置之间'):
            run(executed_through=10, log_flushed_through=10, page_flushes={'sample_B': 11})

    def test_unknown_flush_key_rejected(self):
        with self.assertRaisesRegex(ValueError, '不在初始账本'):
            run(executed_through=10, log_flushed_through=10, page_flushes={'nope': 5})

    def test_flush_before_log_point_is_legal(self):
        result = run(executed_through=10, log_flushed_through=10, page_flushes={'sample_B': 5})
        self.assertTrue(result['invariant_passed'])


class FrozenImageTest(unittest.TestCase):
    def setUp(self):
        self.knobs = dict(executed_through=10, log_flushed_through=10, page_flushes={'sample_B': 10})
        self.image = freeze_page_image(INITIAL, RECORDS, **self.knobs)

    def test_frozen_image_matches_computed(self):
        self.assertEqual(self.image['values'], {'sample_A': 75, 'sample_B': 35})
        self.assertEqual(self.image['page_lsn'], {'sample_A': 4, 'sample_B': 10})
        self.assertEqual(self.image['checkpoint_lsn'], 4)

    def test_queued_snapshot_replays_from_frozen_image(self):
        queued = run(page_image=self.image, **self.knobs)
        direct = run(**self.knobs)
        self.assertEqual(queued['recovered'], direct['recovered'])
        self.assertEqual(queued['result_sha256'], direct['result_sha256'])

    def test_drifted_image_rejected(self):
        bad = dict(self.image)
        bad['values'] = dict(self.image['values'])
        bad['values']['sample_B'] = 999
        with self.assertRaisesRegex(ValueError, '不一致'):
            run(page_image=bad, **self.knobs)

    def test_validate_strict_lock_and_checkpoint(self):
        with self.assertRaisesRegex(ValueError, '严格写锁'):
            validate({'k': 1}, [
                {'op': 'begin', 'tx': 'A'}, {'op': 'write', 'tx': 'A', 'key': 'k', 'value': 2},
                {'op': 'begin', 'tx': 'B'}, {'op': 'write', 'tx': 'B', 'key': 'k', 'value': 3},
            ])
        with self.assertRaisesRegex(ValueError, '静止检查点'):
            validate({'k': 1}, [{'op': 'begin', 'tx': 'A'}, {'op': 'checkpoint'}])


if __name__ == '__main__':
    unittest.main()
