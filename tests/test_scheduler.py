import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.application.scheduler import Scheduler
from src.service.schedule_store import ScheduleStore


class FakeDispatcher:
    def __init__(self, fail=0, on_send=None):
        self.fail = fail
        self.on_send = on_send
        self.sent = []

    def send(self, channel, subject, body, *, task_id, run_id):
        self.sent.append((channel, subject, body, task_id, run_id))
        if self.on_send:
            self.on_send()
        if len(self.sent) <= self.fail:
            raise RuntimeError('transport detail')


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / 'schedules.sqlite')
        self.store = ScheduleStore(self.path)
        self.now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        self.dispatcher = FakeDispatcher()
        self.executed = []
        self.scheduler = Scheduler(self.store, self.dispatcher, self.execute, clock=lambda: self.now)

    def tearDown(self):
        self.scheduler.stop()
        self.temp.cleanup()

    def execute(self, text):
        self.executed.append(text)
        return 'result: ' + text

    def create(self, kind='reminder', at=None, repeat='once'):
        return self.store.create(kind=kind, content='hello', channel='console',
                                 scheduled_at=(at or self.now).isoformat(), timezone_name='UTC', repeat=repeat)

    def test_idle_input_independent_timer_and_due_reminder_without_agent(self):
        real_now = datetime.now(timezone.utc)
        self.now = real_now
        self.scheduler.clock = lambda: datetime.now(timezone.utc)
        self.create(at=real_now + timedelta(seconds=0.15))
        self.scheduler.poll_interval = 0.01
        self.scheduler.start()
        deadline = time.monotonic() + 1
        while not self.dispatcher.sent and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.dispatcher.sent)
        self.assertEqual(self.dispatcher.sent[0][2], 'hello')
        self.assertEqual(self.executed, [])

    def test_agent_runs_once_despite_notification_failures_and_retries_are_bounded(self):
        self.dispatcher.fail = 4
        self.scheduler.max_notification_attempts = 3
        self.create(kind='agent')
        self.scheduler.tick()
        self.assertEqual(self.executed, ['hello'])
        run = self.store.history()[0]
        self.assertEqual(run['status'], 'pending_notification')
        for _ in range(2):
            self.now = datetime.fromisoformat(run['next_attempt_at'])
            self.scheduler.tick()
            run = self.store.history()[0]
        self.assertEqual(run['status'], 'failed')
        self.assertEqual(run['notification_attempts'], 3)
        self.assertEqual(self.executed, ['hello'])

    def test_missed_run_does_not_execute_or_notify(self):
        self.create(kind='agent', at=self.now - timedelta(hours=1))
        self.scheduler.tick()
        self.assertEqual(self.store.history()[0]['status'], 'missed')
        self.assertEqual(self.executed, [])
        self.assertEqual(self.dispatcher.sent, [])

    def test_agent_error_marks_failed_without_retry(self):
        self.scheduler.execute_agent = lambda _text: (_ for _ in ()).throw(RuntimeError('secret'))
        self.create(kind='agent')
        self.scheduler.tick()
        run = self.store.history()[0]
        self.assertEqual(run['status'], 'failed')
        self.assertEqual(run['error'], 'execution failed')
        self.assertEqual(self.dispatcher.sent, [])

    def test_start_recovery_marks_executing_unknown_and_does_not_replay(self):
        self.create(kind='agent')
        self.store.claim_due(self.now)
        self.scheduler.start()
        self.scheduler.stop()
        self.assertEqual(self.store.history()[0]['status'], 'unknown')
        self.assertEqual(self.executed, [])

    def test_saved_notification_resumes_after_restart_without_agent_reexecution(self):
        self.create(kind='agent')
        claimed = self.store.claim_due(self.now)
        self.store.save_result(claimed['id'], 'saved answer', self.now.isoformat())
        resumed = Scheduler(ScheduleStore(self.path), self.dispatcher, self.execute,
                            clock=lambda: self.now)
        resumed.tick()
        resumed.stop()
        self.assertEqual(self.executed, [])
        self.assertEqual(self.dispatcher.sent[0][2], 'saved answer')

    def test_second_worker_fails_to_start_while_first_owns_lock(self):
        self.scheduler.start()
        other = Scheduler(ScheduleStore(self.path), FakeDispatcher(), self.execute)
        try:
            with self.assertRaises(RuntimeError):
                other.start()
            self.assertIsNotNone(other.fatal_error)
        finally:
            other.stop()

    def test_stop_joins_worker_and_releases_lock_for_next_worker(self):
        self.scheduler.start()
        worker = self.scheduler._thread
        self.scheduler.stop()
        self.assertFalse(worker.is_alive())
        other = Scheduler(ScheduleStore(self.path), FakeDispatcher(), self.execute)
        other.start()
        other.stop()

    def test_stop_keeps_lock_while_manual_tick_executor_is_in_flight(self):
        self.create(kind='agent')
        entered = threading.Event()
        release = threading.Event()
        def blocked_executor(_text):
            entered.set()
            release.wait(2)
            return 'finished'
        self.scheduler.execute_agent = blocked_executor
        tick_thread = threading.Thread(target=self.scheduler.tick)
        tick_thread.start()
        self.assertTrue(entered.wait(1))
        stopped = threading.Event()
        stop_thread = threading.Thread(target=lambda: (self.scheduler.stop(), stopped.set()))
        stop_thread.start()
        time.sleep(0.03)
        self.assertFalse(stopped.is_set())
        contender = Scheduler(ScheduleStore(self.path), FakeDispatcher(), self.execute)
        with self.assertRaises(RuntimeError):
            contender.start()
        release.set()
        tick_thread.join(1)
        stop_thread.join(1)
        self.assertTrue(stopped.is_set())
        replacement = Scheduler(ScheduleStore(self.path), FakeDispatcher(), self.execute)
        replacement.start()
        replacement.stop()

    def test_pause_or_cancel_after_execution_suppresses_send(self):
        for state in ('paused', 'cancelled'):
            with self.subTest(state=state):
                task = self.create(kind='agent', repeat='daily')
                def execute_and_change_state(_text, task_id=task['id'], state=state):
                    getattr(self.store, 'pause' if state == 'paused' else 'cancel')(task_id)
                    return 'completed answer'
                self.scheduler.execute_agent = execute_and_change_state
                self.scheduler.tick()
                self.assertEqual(self.dispatcher.sent, [])
                self.assertIn(self.store.history(task['id'])[0]['status'],
                              {'pending_notification', 'failed'})

    def test_pause_cancel_during_send_reconciles_success_and_failure(self):
        for state in ('paused', 'cancelled'):
            for fail in (False, True):
                with self.subTest(state=state, fail=fail):
                    task = self.create(kind='agent', repeat='daily')
                    entered = threading.Event()
                    release = threading.Event()
                    def in_send():
                        entered.set()
                        self.assertTrue(release.wait(2))
                    self.dispatcher = FakeDispatcher(fail=1 if fail else 0, on_send=in_send)
                    scheduler = Scheduler(self.store, self.dispatcher, self.execute,
                                          clock=lambda: self.now)
                    worker = threading.Thread(target=scheduler.tick)
                    worker.start()
                    self.assertTrue(entered.wait(1))
                    getattr(self.store, 'pause' if state == 'paused' else 'cancel')(task['id'])
                    release.set()
                    worker.join(1)
                    self.assertFalse(worker.is_alive())
                    self.assertIsNone(scheduler.fatal_error)
                    run = self.store.history(task['id'])[0]
                    if fail and state == 'paused':
                        self.assertEqual(run['status'], 'pending_notification')
                        self.assertEqual(run['notification_attempts'], 1)
                    elif fail:
                        self.assertEqual(run['status'], 'failed')
                        self.assertEqual(run['error'], 'cancelled')
                    else:
                        self.assertEqual(run['status'], 'delivered')
                    if not fail and state == 'paused':
                        self.store.resume(task['id'])
                        scheduler.tick()
                        self.assertEqual(len(self.dispatcher.sent), 1)
                    scheduler.stop()

    def test_stop_overlapping_recovery_prevents_late_worker_start(self):
        entered = threading.Event()
        release = threading.Event()
        original = self.store.recover_interrupted
        def blocked_recovery():
            entered.set()
            self.assertTrue(release.wait(2))
            return original()
        self.store.recover_interrupted = blocked_recovery
        starter = threading.Thread(target=self.scheduler.start)
        starter.start()
        self.assertTrue(entered.wait(1))
        stopped = threading.Event()
        stopper = threading.Thread(target=lambda: (self.scheduler.stop(), stopped.set()))
        stopper.start()
        time.sleep(0.03)
        self.assertFalse(stopped.is_set())
        release.set()
        starter.join(1)
        stopper.join(1)
        self.assertTrue(stopped.is_set())
        self.assertIsNone(self.scheduler._thread)
        self.assertIsNone(self.scheduler._lock_fd)
        other = Scheduler(ScheduleStore(self.path), FakeDispatcher(), self.execute)
        other.start()
        other.stop()

    def test_simultaneous_start_calls_create_only_one_worker(self):
        entered = threading.Event()
        release = threading.Event()
        original = self.store.recover_interrupted
        calls = []
        def blocked_recovery():
            calls.append(1)
            entered.set()
            self.assertTrue(release.wait(2))
            return original()
        self.store.recover_interrupted = blocked_recovery
        first = threading.Thread(target=self.scheduler.start)
        second = threading.Thread(target=self.scheduler.start)
        first.start()
        self.assertTrue(entered.wait(1))
        second.start()
        release.set()
        first.join(1)
        second.join(1)
        self.assertEqual(len(calls), 1)
        worker = self.scheduler._thread
        self.assertIsNotNone(worker)
        self.assertTrue(worker.is_alive())
        self.scheduler.stop()

    def test_pause_between_claim_and_execution_skips_agent(self):
        task = self.create(kind='agent', repeat='daily')
        original_claim = self.store.claim_due
        def claim_then_pause(now):
            run = original_claim(now)
            self.store.pause(task['id'])
            return run
        self.store.claim_due = claim_then_pause
        self.scheduler.tick()
        self.assertEqual(self.executed, [])
        self.assertEqual(self.dispatcher.sent, [])
        self.assertEqual(self.store.history(task['id'])[0]['status'], 'failed')

    def test_storage_failure_exposes_fatal_error_and_stops_more_side_effects(self):
        self.create(kind='reminder')
        original = self.store.claim_due
        self.store.claim_due = lambda _now: (_ for _ in ()).throw(OSError('db unavailable'))
        self.scheduler.start()
        deadline = time.monotonic() + 1
        while self.scheduler.fatal_error is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIsInstance(self.scheduler.fatal_error, OSError)
        self.assertEqual(self.dispatcher.sent, [])
        self.store.claim_due = original

    def test_concurrent_tick_calls_are_serialized(self):
        self.create(kind='agent', repeat='daily')
        barrier = threading.Barrier(3)
        results = []
        def run_tick():
            barrier.wait()
            results.append(self.scheduler.tick())
        threads = [threading.Thread(target=run_tick) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertEqual(len(self.executed), 1)
        self.assertEqual(len(self.store.history()), 1)


if __name__ == '__main__':
    unittest.main()
