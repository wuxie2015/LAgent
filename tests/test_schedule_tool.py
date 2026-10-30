import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

from src.application.agent import Agent
from src.service.notifications import NotificationDispatcher
from src.service.schedule_store import ScheduleStore
from src.tools.schedule import ScheduleTool


class ScheduleToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ScheduleStore(Path(self.tmp.name) / "schedules.sqlite3")
        self.dispatcher = NotificationDispatcher()
        self.now = datetime(2026, 10, 30, 0, 0, tzinfo=timezone.utc)
        self.tool = ScheduleTool(self.store, self.dispatcher, clock=lambda: self.now)

    def tearDown(self):
        self.tmp.cleanup()

    def create_args(self, **updates):
        args = {"action": "create", "kind": "reminder", "content": "submit report",
                "scheduled_at": "2026-10-31T12:00:00+09:00", "timezone_name": "Asia/Tokyo"}
        args.update(updates)
        return args

    def test_now_reports_configured_zone_and_channels(self):
        result = self.tool.run(action="now")
        self.assertEqual(result["now"], "2026-10-30T09:00:00+09:00")
        self.assertEqual(result["available_channels"], ["console"])

    def test_create_returns_durable_receipt_with_local_and_utc_time(self):
        result = self.tool.run(**self.create_args())
        self.assertTrue(result["ok"])
        self.assertTrue(result["created"])
        self.assertEqual(result["local_time"], "2026-10-31T12:00:00+09:00")
        self.assertEqual(result["scheduled_at"], "2026-10-31T03:00:00+00:00")
        self.assertEqual(self.store.get(result["task_id"])["content"], "submit report")

    def test_invalid_past_time_and_unconfigured_channel_do_not_create(self):
        for args in (
            self.create_args(scheduled_at="2026-10-29T12:00:00+09:00"),
            self.create_args(channel="email"),
            {"action": "create", "kind": "reminder", "content": "missing time"},
        ):
            with self.subTest(args=args):
                result = self.tool.run(**args)
                self.assertFalse(result["ok"])
                self.assertEqual(self.store.list(), [])

    def test_invalid_unknown_and_unexpected_arguments_are_rejected_by_signature(self):
        result = self.tool.run(action="create", **{})
        self.assertFalse(result["ok"])
        with self.assertRaises(TypeError):
            self.tool.run(action="list", destination="attacker@example.test")

    def test_duplicate_calls_same_turn_share_task_and_new_turn_does_not(self):
        self.tool.begin_request()
        one = self.tool.run(**self.create_args())
        repeated = self.tool.run(**self.create_args())
        self.assertEqual(one["task_id"], repeated["task_id"])
        self.assertEqual(len(self.store.list()), 1)
        self.tool.begin_request()
        distinct = self.tool.run(**self.create_args())
        self.assertNotEqual(one["task_id"], distinct["task_id"])
        self.assertEqual(len(self.store.list()), 2)

    def test_parallel_duplicate_create_is_idempotent(self):
        self.tool.begin_request()
        barrier = threading.Barrier(3)
        results = []
        def create():
            barrier.wait()
            results.append(self.tool.run(**self.create_args()))
        threads = [threading.Thread(target=create) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertEqual(results[0]["task_id"], results[1]["task_id"])
        self.assertEqual(len(self.store.list()), 1)

    def test_duplicate_weekly_payload_normalizes_weekday_order_and_equivalent_local_time(self):
        self.tool.begin_request()
        first = self.tool.run(**self.create_args(
            scheduled_at="2026-11-02T12:00:00", repeat="weekly", weekdays=[0, 4]))
        equivalent = self.tool.run(**self.create_args(
            scheduled_at="2026-11-02T12:00:00+09:00", repeat="weekly", weekdays=[4, 0]))
        self.assertEqual(first["task_id"], equivalent["task_id"])
        self.assertEqual(len(self.store.list()), 1)

    def test_update_list_get_history_cancel(self):
        created = self.tool.run(**self.create_args())
        task_id = created["task_id"]
        updated = self.tool.run(action="update", task_id=task_id, content="updated")
        self.assertEqual(updated["task"]["content"], "updated")
        self.assertEqual(self.tool.run(action="get", task_id=task_id)["task"]["content"], "updated")
        self.assertEqual(len(self.tool.run(action="list")["tasks"]), 1)
        self.assertEqual(self.tool.run(action="history", task_id=task_id)["history"], [])
        self.assertEqual(self.tool.run(action="cancel", task_id=task_id)["task"]["status"], "cancelled")
        self.assertFalse(self.tool.run(action="update", task_id=task_id, content="no")["ok"])

    def test_update_local_time_keeps_named_timezone(self):
        created = self.tool.run(**self.create_args())
        result = self.tool.run(action="update", task_id=created["task_id"],
                               scheduled_at="2026-10-31T09:00:00", timezone_name="Asia/Tokyo")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["task"]["scheduled_at"], "2026-10-31T00:00:00+00:00")

    def test_update_checks_existing_channel_configuration(self):
        email_dispatcher = NotificationDispatcher(smtp_host="mail.example.test", smtp_from="a@example.test", smtp_to="b@example.test")
        tool = ScheduleTool(self.store, email_dispatcher, clock=lambda: self.now)
        created = tool.run(**self.create_args(channel="email"))
        self.assertTrue(created["ok"])
        unavailable = ScheduleTool(self.store, self.dispatcher, clock=lambda: self.now)
        result = unavailable.run(action="update", task_id=created["task_id"], content="updated")
        self.assertFalse(result["ok"])

    def test_fatal_scheduler_refuses_create_and_update(self):
        enabled = [True]
        tool = ScheduleTool(self.store, self.dispatcher, clock=lambda: self.now,
                            writes_available=lambda: enabled[0])
        enabled[0] = False
        self.assertFalse(tool.run(**self.create_args())["ok"])

    def test_agent_natural_language_tool_call_gets_receipt(self):
        class FakeModel:
            def __init__(self):
                self.calls = 0
            def chat(self, messages, tools):
                self.calls += 1
                if self.calls == 1:
                    return {"message": {"role": "assistant", "content": "", "tool_calls": [{
                        "function": {"name": "schedule", "arguments": self.create_arguments}
                    }]}}
                return {"message": {"role": "assistant", "content": "已为你安排。"}}
        fake = FakeModel()
        fake.create_arguments = self.create_args()
        agent = Agent(fake, [self.tool], "system")
        self.tool.begin_request()
        self.assertEqual(agent.ask("明天中午提醒我交报告"), "已为你安排。")
        self.assertEqual(len(self.store.list()), 1)

if __name__ == "__main__":
    unittest.main()
