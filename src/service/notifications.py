"""Configured notification transports for scheduled task results."""

from __future__ import annotations

import json
import smtplib
import urllib.error
import urllib.parse
import urllib.request
import ssl
from email.message import EmailMessage
from typing import Any


class NotificationError(ValueError):
    """A notification channel is unavailable or delivery failed."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def _no_header_newlines(name: str, value: str) -> None:
    if "\r" in value or "\n" in value:
        raise ValueError(f"{name} must not contain line breaks")


class NotificationDispatcher:
    """Send notifications through console, configured SMTP, or a webhook.

    Destinations are read only from constructor configuration. Callers choose a
    channel name and cannot supply an address or URL per notification.
    """

    def __init__(
        self,
        *,
        console_enabled: bool = True,
        smtp_host: str = "",
        smtp_port: int = 587,
        smtp_from: str = "",
        smtp_to: str = "",
        smtp_user: str = "",
        smtp_password: str = "",
        smtp_security: str = "starttls",
        webhook_url: str = "",
        webhook_token: str = "",
        timeout: float = 15,
    ) -> None:
        if not isinstance(console_enabled, bool):
            raise ValueError("console_enabled must be a boolean")
        if isinstance(smtp_port, bool) or not isinstance(smtp_port, int) or not 1 <= smtp_port <= 65535:
            raise ValueError("smtp_port must be between 1 and 65535")
        if smtp_security not in {"ssl", "starttls", "plain"}:
            raise ValueError("smtp_security must be ssl, starttls, or plain")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 300:
            raise ValueError("timeout must be greater than 0 and no more than 300 seconds")

        config_values = {
            "smtp_host": smtp_host,
            "smtp_from": smtp_from,
            "smtp_to": smtp_to,
            "smtp_user": smtp_user,
            "smtp_password": smtp_password,
            "webhook_url": webhook_url,
            "webhook_token": webhook_token,
        }
        for name, value in config_values.items():
            if not isinstance(value, str):
                raise ValueError(f"{name} must be a string")
            _no_header_newlines(name, value)

        if bool(smtp_user) != bool(smtp_password):
            raise ValueError("smtp_user and smtp_password must be configured together")
        smtp_configured = bool(smtp_host or smtp_from or smtp_to or smtp_user or smtp_password)
        if smtp_configured and not (smtp_host and smtp_from and smtp_to):
            raise ValueError("SMTP requires smtp_host, smtp_from, and smtp_to")
        if webhook_token and not webhook_url:
            raise ValueError("webhook_token requires webhook_url")
        if webhook_url:
            parsed = urllib.parse.urlsplit(webhook_url)
            local_http = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            if (parsed.scheme != "https" and not local_http) or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("webhook_url must use HTTPS or localhost HTTP and contain no user information")
            if parsed.fragment:
                raise ValueError("webhook_url must not contain a fragment")
            try:
                _ = parsed.port
            except ValueError:
                raise ValueError("webhook_url has an invalid port") from None

        self.console_enabled = console_enabled
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_from = smtp_from
        self.smtp_to = smtp_to
        self.smtp_user = smtp_user
        self.smtp_password = smtp_password
        self.smtp_security = smtp_security
        self.webhook_url = webhook_url
        self.webhook_token = webhook_token
        self.timeout = float(timeout)

    def available_channels(self) -> list[str]:
        channels: list[str] = []
        if self.console_enabled:
            channels.append("console")
        if self.smtp_host and self.smtp_from and self.smtp_to:
            channels.append("email")
        if self.webhook_url:
            channels.append("webhook")
        return channels

    def validate_channel(self, channel: str) -> None:
        if channel not in self.available_channels():
            raise NotificationError(f"notification channel is unavailable: {channel!r}")

    def send(self, channel: str, subject: str, body: str, *, task_id: str, run_id: str) -> None:
        self.validate_channel(channel)
        for name, value in (("subject", subject), ("body", body), ("task_id", task_id), ("run_id", run_id)):
            if not isinstance(value, str):
                raise ValueError(f"{name} must be a string")
        _no_header_newlines("subject", subject)
        _no_header_newlines("run_id", run_id)

        if channel == "console":
            print(f"\n[{subject}]\n{body}\n(Task: {task_id}; run: {run_id})", flush=True)
        elif channel == "email":
            self._send_email(subject, body)
        elif channel == "webhook":
            self._send_webhook(subject, body, task_id, run_id)

    def _send_email(self, subject: str, body: str) -> None:
        message = EmailMessage()
        message["From"] = self.smtp_from
        message["To"] = self.smtp_to
        message["Subject"] = subject
        message.set_content(body, charset="utf-8")
        client = None
        try:
            if self.smtp_security == "ssl":
                client = smtplib.SMTP_SSL(
                    self.smtp_host, self.smtp_port, timeout=self.timeout, context=ssl.create_default_context()
                )
            else:
                client = smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=self.timeout)
                if self.smtp_security == "starttls":
                    client.starttls(context=ssl.create_default_context())
            if self.smtp_security != "plain" and self.smtp_user:
                client.login(self.smtp_user, self.smtp_password)
            client.send_message(message)
        except Exception:
            raise NotificationError("email notification delivery failed") from None
        finally:
            if client is not None:
                try:
                    client.quit()
                except Exception:
                    pass

    def _send_webhook(self, subject: str, body: str, task_id: str, run_id: str) -> None:
        payload = json.dumps(
            {"subject": subject, "body": body, "task_id": task_id, "run_id": run_id},
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            self.webhook_url,
            data=payload,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Idempotency-Key": run_id,
                **({"Authorization": f"Bearer {self.webhook_token}"} if self.webhook_token else {}),
            },
            method="POST",
        )
        opener = urllib.request.build_opener(_NoRedirectHandler())
        response = None
        try:
            response = opener.open(request, timeout=self.timeout)
            status = getattr(response, "status", getattr(response, "code", 200))
            if not 200 <= status < 300:
                raise NotificationError("webhook notification returned a non-success status")
            # Read a bounded amount so responses are consumed without allowing
            # an endpoint to make this worker download an unbounded body.
            response.read(4096)
        except NotificationError:
            raise
        except Exception:
            raise NotificationError("webhook notification delivery failed") from None
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
