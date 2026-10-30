import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from src.service.notifications import NotificationDispatcher, NotificationError


class NotificationDispatcherTests(unittest.TestCase):
    def test_console_channel_outputs_readable_message(self):
        dispatcher = NotificationDispatcher()
        output = io.StringIO()
        with redirect_stdout(output):
            dispatcher.send("console", "Reminder", "Take a break", task_id="task-1", run_id="run-1")
        self.assertIn("Reminder", output.getvalue())
        self.assertIn("Take a break", output.getvalue())

    def test_available_channels_only_includes_configured_routes(self):
        dispatcher = NotificationDispatcher(
            console_enabled=False,
            smtp_host="mail.example.test",
            smtp_from="from@example.test",
            smtp_to="to@example.test",
            webhook_url="https://hooks.example.test/notify",
        )
        self.assertEqual(dispatcher.available_channels(), ["email", "webhook"])

    def test_email_uses_fixed_addresses_and_starttls(self):
        dispatcher = NotificationDispatcher(
            console_enabled=False,
            smtp_host="mail.example.test",
            smtp_from="from@example.test",
            smtp_to="to@example.test",
            smtp_user="user",
            smtp_password="secret",
        )
        smtp = Mock()
        smtp_cls = Mock(return_value=smtp)
        with patch("src.service.notifications.smtplib.SMTP", smtp_cls):
            dispatcher.send("email", "Subject", "Body", task_id="task-1", run_id="run-1")
        smtp_cls.assert_called_once_with("mail.example.test", 587, timeout=15)
        smtp.starttls.assert_called_once()
        smtp.login.assert_called_once_with("user", "secret")
        raw_message = smtp.send_message.call_args.args[0]
        self.assertEqual(raw_message["From"], "from@example.test")
        self.assertEqual(raw_message["To"], "to@example.test")
        self.assertEqual(raw_message["Subject"], "Subject")
        self.assertIn("Body", raw_message.get_content())
        smtp.quit.assert_called_once()

    def test_email_ssl_uses_ssl_transport(self):
        dispatcher = NotificationDispatcher(
            console_enabled=False,
            smtp_host="mail.example.test", smtp_port=465,
            smtp_from="from@example.test", smtp_to="to@example.test", smtp_security="ssl",
        )
        with patch("src.service.notifications.smtplib.SMTP_SSL") as smtp_cls:
            dispatcher.send("email", "S", "B", task_id="t", run_id="r")
        smtp_cls.assert_called_once_with("mail.example.test", 465, timeout=15, context=unittest.mock.ANY)

    def test_webhook_posts_json_with_bearer_and_idempotency_headers(self):
        dispatcher = NotificationDispatcher(
            console_enabled=False,
            webhook_url="https://hooks.example.test/notify", webhook_token="secret-token",
        )
        response = Mock()
        response.status = 204
        response.read.return_value = b""
        opener = Mock()
        opener.open.return_value = response
        with patch("src.service.notifications.urllib.request.build_opener", return_value=opener):
            dispatcher.send("webhook", "Subject", "Body", task_id="task-1", run_id="run-1")
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://hooks.example.test/notify")
        self.assertEqual(request.get_header("Content-type"), "application/json; charset=utf-8")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-token")
        self.assertEqual(request.get_header("Idempotency-key"), "run-1")
        self.assertEqual(json.loads(request.data), {
            "subject": "Subject", "body": "Body", "task_id": "task-1", "run_id": "run-1",
        })
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 15)

    def test_unconfigured_or_unknown_channels_are_rejected(self):
        dispatcher = NotificationDispatcher(console_enabled=False)
        with self.assertRaises(NotificationError):
            dispatcher.validate_channel("email")
        with self.assertRaises(NotificationError):
            dispatcher.validate_channel("sms")

    def test_bad_configuration_and_header_injection_are_rejected(self):
        with self.assertRaises(ValueError):
            NotificationDispatcher(smtp_user="user")
        with self.assertRaises(ValueError):
            NotificationDispatcher(smtp_from="a@example.test\r\nBcc: x@example.test")
        with self.assertRaises(ValueError):
            NotificationDispatcher(smtp_security="invalid")
        with self.assertRaises(ValueError):
            NotificationDispatcher(webhook_url="http://hooks.example.test/notify")
        self.assertIn("webhook", NotificationDispatcher(
            webhook_url="http://localhost:8080/notify",
        ).available_channels())
        with self.assertRaises(ValueError):
            NotificationDispatcher(webhook_token="token\nforged")
        dispatcher = NotificationDispatcher()
        with self.assertRaises(ValueError):
            dispatcher.send("console", "bad\nsubject", "Body", task_id="t", run_id="r")

    def test_plain_smtp_does_not_login_and_invalid_timeout_rejected(self):
        with self.assertRaises(ValueError):
            NotificationDispatcher(timeout=0)
        dispatcher = NotificationDispatcher(
            console_enabled=False,
            smtp_host="localhost", smtp_from="a@example.test", smtp_to="b@example.test",
            smtp_security="plain", smtp_user="user", smtp_password="secret",
        )
        smtp = Mock()
        with patch("src.service.notifications.smtplib.SMTP", return_value=smtp):
            dispatcher.send("email", "S", "B", task_id="t", run_id="r")
        smtp.starttls.assert_not_called()
        smtp.login.assert_not_called()

    def test_transport_errors_are_sanitized(self):
        secret = "top-secret-credential"
        dispatcher = NotificationDispatcher(
            console_enabled=False, webhook_url="https://hooks.example.test/notify", webhook_token=secret,
        )
        with patch("src.service.notifications.urllib.request.build_opener") as build_opener:
            build_opener.return_value.open.side_effect = RuntimeError(secret)
            with self.assertRaises(NotificationError) as raised:
                dispatcher.send("webhook", "S", "B", task_id="t", run_id="r")
        self.assertNotIn(secret, str(raised.exception))

    def test_smtp_errors_are_sanitized(self):
        secret = "smtp-private-value"
        dispatcher = NotificationDispatcher(
            console_enabled=False, smtp_host="mail.example.test",
            smtp_from="from@example.test", smtp_to="to@example.test",
        )
        with patch("src.service.notifications.smtplib.SMTP", side_effect=RuntimeError(secret)):
            with self.assertRaises(NotificationError) as raised:
                dispatcher.send("email", "S", "B", task_id="t", run_id="r")
        self.assertNotIn(secret, str(raised.exception))

    def test_non_success_webhook_status_fails_and_response_is_bounded(self):
        dispatcher = NotificationDispatcher(
            console_enabled=False, webhook_url="https://hooks.example.test/notify",
        )
        response = Mock(status=503)
        opener = Mock()
        opener.open.return_value = response
        with patch("src.service.notifications.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(NotificationError):
                dispatcher.send("webhook", "S", "B", task_id="t", run_id="r")
        response.read.assert_not_called()
        response.close.assert_called_once()

    def test_redirects_are_not_followed(self):
        dispatcher = NotificationDispatcher(console_enabled=False, webhook_url="https://hooks.example.test/notify")
        with patch("src.service.notifications.urllib.request.build_opener") as build_opener:
            build_opener.return_value.open.return_value.status = 204
            dispatcher.send("webhook", "S", "B", task_id="t", run_id="r")
        handlers = build_opener.call_args.args
        self.assertEqual(len(handlers), 1)
        self.assertEqual(handlers[0].__class__.__name__, "_NoRedirectHandler")


if __name__ == "__main__":
    unittest.main()
