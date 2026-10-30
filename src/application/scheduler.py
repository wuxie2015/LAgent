"""Single-worker scheduler for persisted reminders and agent runs.

The scheduler owns an OS-level lock for the schedule database while running.
Agent execution is injected so this module has no model or CLI dependencies.
"""

from __future__ import annotations

import fcntl
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Callable


_LOG = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, store, dispatcher, execute_agent: Callable[[str], str], *,
                 poll_interval: float = 1.0, max_notification_attempts: int = 5,
                 clock: Callable[[], datetime] | None = None,
                 on_error: Callable[[BaseException], None] | None = None):
        if not callable(execute_agent):
            raise ValueError("execute_agent must be callable")
        if isinstance(poll_interval, bool) or not isinstance(poll_interval, (int, float)) or poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        if type(max_notification_attempts) is not int or max_notification_attempts < 1:
            raise ValueError("max_notification_attempts must be a positive integer")
        if on_error is not None and not callable(on_error):
            raise ValueError("on_error must be callable")
        self.store = store
        self.dispatcher = dispatcher
        self.execute_agent = execute_agent
        self.poll_interval = float(poll_interval)
        self.max_notification_attempts = max_notification_attempts
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.on_error = on_error
        self.fatal_error: BaseException | None = None
        self._stop_event = threading.Event()
        self._pass_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._startup_lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._lock_fd: int | None = None
        db_path = os.path.realpath(os.fspath(store.path))
        self._lock_path = db_path + ".worker.lock"

    def _set_fatal(self, error: BaseException) -> None:
        if self.fatal_error is not None:
            return
        self.fatal_error = error
        _LOG.error("scheduler stopped after fatal error type %s", type(error).__name__)
        if self.on_error is not None:
            try:
                self.on_error(error)
            except Exception:
                _LOG.exception("scheduler error callback failed")

    def _acquire_lock(self) -> None:
        with self._lifecycle_lock:
            if self._lock_fd is not None:
                return
            fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                error = RuntimeError("another scheduler worker owns this database")
                self.fatal_error = error
                raise error
            except BaseException:
                os.close(fd)
                raise
            self._lock_fd = fd

    def _release_lock(self) -> None:
        with self._lifecycle_lock:
            fd, self._lock_fd = self._lock_fd, None
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def start(self) -> None:
        # Serialize the whole transition, including recovery and thread
        # registration, against concurrent start() and stop() calls.
        with self._startup_lock:
            with self._lifecycle_lock:
                if self._thread is not None and self._thread.is_alive():
                    return
                if self.fatal_error is not None:
                    raise RuntimeError("scheduler has stopped after a fatal error") from self.fatal_error
            self._stop_event.clear()
            self._acquire_lock()
            try:
                # Recovery is safe only after taking the exclusive worker lock.
                self.store.recover_interrupted()
                # stop() can signal while recovery is blocked. In that case,
                # do not clear its signal or start a worker after stop returns.
                if self._stop_event.is_set():
                    self._release_lock()
                    return
                thread = threading.Thread(target=self._run, name="lin-agent-scheduler", daemon=False)
                with self._lifecycle_lock:
                    self._thread = thread
                thread.start()
            except BaseException as error:
                self._set_fatal(error)
                self._release_lock()
                raise

    def stop(self) -> None:
        self._stop_event.set()
        with self._startup_lock:
            with self._lifecycle_lock:
                thread = self._thread
            if thread is not None and thread is not threading.current_thread():
                thread.join()
            # A stopped worker releases its lock in _run's finally block.
            # Manual tick mode has no worker thread, so wait for an in-flight
            # pass before releasing its process lock.
            if thread is None:
                with self._pass_lock:
                    self._release_lock()
            elif thread is threading.current_thread():
                return
            elif not thread.is_alive():
                self._release_lock()

    def tick(self) -> bool:
        """Run one serialized polling pass; useful for deterministic callers/tests."""
        if self.fatal_error is not None:
            raise RuntimeError("scheduler has stopped after a fatal error") from self.fatal_error
        self._acquire_lock()
        with self._pass_lock:
            try:
                return self._tick_locked()
            except BaseException as error:
                self._set_fatal(error)
                raise

    def _tick_locked(self) -> bool:
        if self._stop_event.is_set():
            return False
        now = self.clock()
        claimed = self.store.claim_due(now)
        did_work = False
        if claimed is not None:
            did_work = True
            if claimed["status"] != "missed" and not self._stop_event.is_set():
                self._execute_claimed(claimed)
        if self._stop_event.is_set():
            return did_work
        for run in self.store.notification_due(self.clock()):
            if self._stop_event.is_set():
                break
            self._deliver(run)
            did_work = True
        return did_work

    def _execute_claimed(self, run: dict) -> None:
        # A schedule may have been paused or cancelled after the atomic claim
        # but before execution began. Do not start new work in that state.
        task = self.store.get(run["task_id"])
        if task["status"] not in {"active", "completed"}:
            self.store.mark_failed(run["id"], self.clock(), error="execution skipped")
            return
        if self._stop_event.is_set():
            return
        try:
            result = run["content"] if run["kind"] == "reminder" else self.execute_agent(run["content"])
            if not isinstance(result, str):
                raise ValueError("agent result must be a string")
        except Exception as error:
            self.store.mark_failed(run["id"], self.clock(), error="execution failed")
            _LOG.warning("scheduled execution failed for run %s (%s)", run["id"],
                         type(error).__name__)
            return
        # Persist the result before any delivery attempt. If shutdown arrives
        # during execution, the saved result remains resumable after restart.
        self.store.save_result(run["id"], result, self.clock())

    def _deliver(self, run: dict) -> None:
        task = self.store.get(run["task_id"])
        if task["status"] not in {"active", "completed"}:
            return
        if self._stop_event.is_set():
            return
        try:
            self.dispatcher.send(run["channel"], "Lin Agent 提醒", run["result"],
                                task_id=run["task_id"], run_id=run["id"])
        except Exception:
            self.store.notification_failed(run["id"], self.clock(), error="notification failed",
                                           max_attempts=self.max_notification_attempts)
            return
        self.store.mark_delivered(run["id"])

    def _run(self) -> None:
        try:
            while not self._stop_event.is_set():
                try:
                    self.tick()
                except BaseException as error:
                    self._set_fatal(error)
                    break
                if self._stop_event.wait(self.poll_interval):
                    break
        finally:
            self._release_lock()
