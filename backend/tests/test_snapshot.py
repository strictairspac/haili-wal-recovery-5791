"""Snapshot resolution for queueing and the worker (needs service deps).

The WAL engine itself is dependency-free; this module imports the service
layer only when SQLAlchemy/FastAPI are installed, and otherwise skips.
"""
import importlib.util
import unittest

DEPENDENCIES = all(importlib.util.find_spec(name) for name in ('sqlalchemy', 'fastapi'))

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


@unittest.skipUnless(DEPENDENCIES, '需要 SQLAlchemy 与 FastAPI')
class SnapshotScenarioTest(unittest.TestCase):
    def setUp(self):
        from src.services.recovery_service import snapshot_scenario
        from src.services.wal_engine import freeze_page_image, recover
        self.snapshot_scenario = snapshot_scenario
        self.freeze = freeze_page_image
        self.recover = recover

    def run_snapshot(self, snapshot):
        return self.recover(snapshot['log_set']['initial'], snapshot['log_set']['records'],
                            **self.snapshot_scenario(snapshot))

    def test_old_snapshot_without_new_fields(self):
        # Written by the previous release: only case.crash_after.
        snapshot = {'log_set': {'initial': INITIAL, 'records': RECORDS},
                    'case': {'crash_after': 10}}
        self.assertEqual(self.snapshot_scenario(snapshot), {'crash_after': 10})
        result = self.run_snapshot(snapshot)
        self.assertEqual(result['recovered'], {'sample_A': 60, 'sample_B': 50})
        self.assertEqual(result['page_lsn'], {'sample_A': 4, 'sample_B': 4})

    def test_frozen_image_used_after_queueing(self):
        image = self.freeze(INITIAL, RECORDS, executed_through=10,
                            log_flushed_through=10, page_flushes={'sample_B': 10})
        params = {'legacy': False, 'executed_through': 10, 'crash_after': 10,
                  'log_flushed_through': 10, 'page_flushes': {'sample_B': 10}}
        snapshot = {'log_set': {'initial': INITIAL, 'records': RECORDS},
                    'case': {'crash_after': 10, 'executed_through': 10,
                             'log_flushed_through': 10, 'page_flushes': {'sample_B': 10},
                             'page_image': image},
                    'page_image': image, 'parameters': params}
        result = self.run_snapshot(snapshot)
        self.assertTrue(result['invariant_passed'])
        self.assertEqual(result['page_image'], {'sample_A': 75, 'sample_B': 35})
        self.assertGreater(result['redo_skipped_count'], 0)

    def test_legacy_row_uses_no_force_image(self):
        from src.services.crash_case_service import case_parameters
        row = type('CaseRow', (), {'executed_through': None, 'crash_after': 10,
                                   'log_flushed_through': None, 'page_flushes': None})()
        params = case_parameters(row)
        self.assertTrue(params['legacy'])
        image = self.freeze(INITIAL, RECORDS, executed_through=10)
        snapshot = {'log_set': {'initial': INITIAL, 'records': RECORDS},
                    'case': {'crash_after': 10}, 'parameters': params, 'page_image': image}
        result = self.run_snapshot(snapshot)
        self.assertEqual(result['recovered'], {'sample_A': 60, 'sample_B': 50})
        self.assertEqual(result['page_lsn'], {'sample_A': 4, 'sample_B': 4})


if __name__ == '__main__':
    unittest.main()
