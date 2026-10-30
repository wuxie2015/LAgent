import tempfile
import threading
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
from pathlib import Path

from src.domain.schedule import normalize_scheduled_time, next_occurrence
from src.service.schedule_store import ScheduleStore


class ScheduleTimeTests(unittest.TestCase):
    def test_naive_local_and_offset_inputs(self):
        self.assertEqual(normalize_scheduled_time('2026-11-01T09:30:00', 'Asia/Tokyo'), '2026-11-01T00:30:00+00:00')
        self.assertEqual(normalize_scheduled_time('2026-11-01T09:30:00+09:00', 'Asia/Tokyo'), '2026-11-01T00:30:00+00:00')

    def test_dst_ambiguous_requires_offset_and_nonexistent_rejected(self):
        with self.assertRaises(ValueError):
            normalize_scheduled_time('2026-11-01T01:30:00', 'America/New_York')
        self.assertEqual(normalize_scheduled_time('2026-11-01T01:30:00-04:00', 'America/New_York'), '2026-11-01T05:30:00+00:00')
        with self.assertRaises(ValueError):
            normalize_scheduled_time('2026-03-08T02:30:00', 'America/New_York')

    def test_daily_wall_time_skips_dst_gap(self):
        nxt = next_occurrence('2026-03-07T07:30:00+00:00', 'America/New_York', 'daily')
        self.assertEqual(nxt, '2026-03-09T06:30:00+00:00')

    def test_weekly_multiple_days_stays_anchored_to_initial_wall_time(self):
        nxt = next_occurrence('2026-01-05T09:00:00+09:00', 'Asia/Tokyo', 'weekly', [0, 4], after='2026-01-05T01:00:00Z')
        self.assertEqual(nxt, '2026-01-09T00:00:00+00:00')

    def test_weekly_initial_date_must_match_selected_days(self):
        with self.assertRaises(ValueError):
            next_occurrence('2026-01-06T09:00:00+09:00', 'Asia/Tokyo', 'weekly', [0, 4])


class ScheduleStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / 'schedules.sqlite')
        self.store = ScheduleStore(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def create(self, **kw):
        values = dict(kind='reminder', content='hello', channel='console', scheduled_at='2026-01-01T12:00:00Z', timezone_name='UTC')
        values.update(kw)
        return self.store.create(**values)

    def test_restart_claim_snapshot_and_idempotency(self):
        task = self.create(request_key='req-1')
        self.assertEqual(self.create(request_key='req-1')['id'], task['id'])
        with self.assertRaises(ValueError):
            self.store.create(kind='reminder', content='different', channel='console', scheduled_at='2026-01-01T12:00:00Z', timezone_name='UTC', request_key='req-1')
        other = ScheduleStore(self.path)
        run = self.store.claim_due('2026-01-01T12:01:00Z')
        self.assertEqual(run['content'], 'hello')
        self.assertIsNone(other.claim_due('2026-01-01T12:01:00Z'))
        other.save_result(run['id'], 'done', '2026-01-01T12:02:00Z')
        self.assertEqual(self.store.notification_due('2026-01-01T12:03:00Z')[0]['result'], 'done')

    def test_missed_occurrence_advances_repeat(self):
        task = self.create(repeat='daily', grace_seconds=60)
        run = self.store.claim_due('2026-01-02T13:00:00Z')
        self.assertEqual(run['status'], 'missed')
        updated = self.store.get(task['id'])
        self.assertEqual(updated['next_run_at'], '2026-01-03T12:00:00+00:00')
        self.assertEqual(updated['status'], 'active')

    def test_pause_and_cancel_stop_pending_notification(self):
        task = self.create(repeat='daily')
        run = self.store.claim_due('2026-01-01T12:00:00Z')
        self.store.save_result(run['id'], 'done', '2026-01-01T12:01:00Z')
        self.store.pause(task['id'])
        self.assertEqual(self.store.notification_due('2026-01-02T00:00:00Z'), [])
        self.store.resume(task['id'])
        self.store.cancel(task['id'])
        self.assertEqual(self.store.notification_due('2026-01-02T00:00:00Z'), [])

    def test_retry_persists_attempt_and_pause_cancel_suppress_it(self):
        task = self.create(repeat='daily')
        run = self.store.claim_due('2026-01-01T12:00:00Z')
        self.store.save_result(run['id'], 'done', '2026-01-01T12:01:00Z')
        failed = self.store.notification_failed(run['id'], '2026-01-01T12:02:00Z', error='private transport detail')
        self.assertEqual(failed['notification_attempts'], 1)
        self.assertEqual(failed['error'], 'notification failed')
        self.store.pause(task['id'])
        self.assertEqual(self.store.notification_due('2026-01-02T00:00:00Z'), [])
        self.store.resume(task['id'])
        self.store.cancel(task['id'])
        self.assertEqual(self.store.notification_due('2026-01-02T00:00:00Z'), [])

    def test_corrupt_database_is_not_deleted(self):
        bad = Path(self.temp.name) / 'bad.sqlite'
        bad.write_bytes(b'not sqlite')
        with self.assertRaises(Exception):
            ScheduleStore(str(bad)).list()
        self.assertEqual(bad.read_bytes(), b'not sqlite')

    def test_malformed_schema_is_not_recreated_or_erased(self):
        bad = Path(self.temp.name) / 'malformed.sqlite'
        import sqlite3
        with sqlite3.connect(bad) as conn:
            conn.execute('CREATE TABLE unrelated (value TEXT)')
            conn.execute("INSERT INTO unrelated VALUES ('keep')")
        with self.assertRaises(Exception):
            ScheduleStore(str(bad)).list()
        with sqlite3.connect(bad) as conn:
            self.assertEqual(conn.execute('SELECT value FROM unrelated').fetchone()[0], 'keep')

    def test_update_content_preserves_next_occurrence_and_timezone_change_preserves_instant(self):
        task = self.create(repeat='daily')
        self.store.claim_due('2026-01-01T12:00:00Z')
        next_at = self.store.get(task['id'])['next_run_at']
        updated = self.store.update(task['id'], content='changed')
        self.assertEqual(updated['next_run_at'], next_at)
        with patch('src.service.schedule_store._now', return_value='2026-01-01T12:01:00+00:00'):
            updated = self.store.update(task['id'], timezone_name='America/New_York')
        self.assertEqual(updated['scheduled_at'], '2026-01-01T12:00:00+00:00')
        self.assertEqual(updated['next_run_at'], '2026-01-02T12:00:00+00:00')

    def test_timezone_update_recalculates_weekly_occurrence_from_local_wall_time(self):
        task = self.create(scheduled_at='2026-01-05T23:30:00Z', timezone_name='UTC',
                           repeat='weekly', weekdays=[0], grace_seconds=300)
        self.store.claim_due('2026-06-29T23:30:00Z')
        with patch('src.service.schedule_store._now', return_value='2026-06-30T12:00:00+00:00'):
            updated = self.store.update(task['id'], timezone_name='Europe/London')
        self.assertEqual(updated['next_run_at'], '2026-07-06T22:30:00+00:00')
        run = self.store.claim_due('2026-07-06T22:30:00Z')
        self.assertEqual(run['status'], 'executing')
        self.assertEqual(self.store.get(task['id'])['next_run_at'], '2026-07-13T22:30:00+00:00')

    def test_timezone_update_keeps_future_once_instant(self):
        task = self.create(scheduled_at='2026-11-01T12:00:00Z')
        with patch('src.service.schedule_store._now', return_value='2026-10-30T12:00:00+00:00'):
            updated = self.store.update(task['id'], timezone_name='America/New_York')
        self.assertEqual(updated['scheduled_at'], '2026-11-01T12:00:00+00:00')
        self.assertEqual(updated['next_run_at'], '2026-11-01T12:00:00+00:00')

    def test_once_completed_run_can_be_cancelled_and_not_retried(self):
        task = self.create()
        run = self.store.claim_due('2026-01-01T12:00:00Z')
        self.assertEqual(self.store.get(task['id'])['status'], 'completed')
        self.store.cancel(task['id'])
        self.store.save_result(run['id'], 'done', '2026-01-01T12:01:00Z')
        self.assertEqual(self.store.notification_due('2026-01-02T00:00:00Z'), [])

    def test_kind_is_limited_to_supported_values(self):
        with self.assertRaises(ValueError):
            self.create(kind='other')

    def test_create_rejects_weekly_schedule_whose_start_day_is_not_selected(self):
        with self.assertRaises(ValueError):
            self.create(scheduled_at='2026-01-06T09:00:00+09:00', timezone_name='Asia/Tokyo',
                        repeat='weekly', weekdays=[0, 4])

    def test_update_rejects_weekly_rule_that_excludes_initial_date(self):
        task = self.create(scheduled_at='2026-01-06T09:00:00+09:00', timezone_name='Asia/Tokyo')
        with self.assertRaises(ValueError):
            self.store.update(task['id'], repeat='weekly', weekdays=[0, 4])
        self.assertEqual(self.store.get(task['id'])['repeat'], 'once')

    def test_concurrent_partial_updates_do_not_revert_each_other(self):
        task = self.create()
        original_get = self.store.get
        read_done = threading.Event()
        continue_update = threading.Event()

        def delayed_get(task_id):
            result = original_get(task_id)
            if threading.current_thread().name == 'stale-update':
                read_done.set()
                if not continue_update.wait(5):
                    raise RuntimeError('test synchronization timed out')
            return result

        self.store.get = delayed_get
        errors = []

        def update_grace():
            try:
                self.store.update(task['id'], grace_seconds=90)
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=update_grace, name='stale-update')
        worker.start()
        self.assertTrue(read_done.wait(5))
        self.store.update(task['id'], content='concurrent content')
        continue_update.set()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        updated = self.store.get(task['id'])
        self.assertEqual(updated['content'], 'concurrent content')
        self.assertEqual(updated['grace_seconds'], 90)


if __name__ == '__main__':
    unittest.main()
