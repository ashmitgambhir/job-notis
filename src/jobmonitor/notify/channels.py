"""Delivery channels.

Every channel implements the same tiny contract, so the dispatcher does not
care which one it is talking to. Twilio and SendGrid are called over plain HTTP
rather than through their SDKs — both are a single POST, and avoiding the
dependencies keeps the container small and the install fast.
"""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from abc import ABC, abstractmethod
from email.message import EmailMessage
from typing import Any, Dict, List, Optional

import structlog

from ..config.schema import (
    EmailChannel,
    SmsChannel,
    TelegramChannel,
    WebhookChannel,
)
from ..errors import NotificationError, PermanentError
from ..net.client import HttpClient
from .formatter import Alert

log = structlog.get_logger(__name__)


class Channel(ABC):
    name: str = "base"

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    @property
    @abstractmethod
    def enabled(self) -> bool: ...

    @property
    @abstractmethod
    def min_score(self) -> int: ...

    @property
    @abstractmethod
    def max_per_hour(self) -> int: ...

    @abstractmethod
    async def send(self, alert: Alert) -> None: ...

    async def send_raw(self, subject: str, body_html: str, body_text: str) -> None:
        """Used for digests and self-monitoring alerts."""
        raise NotImplementedError(f"{self.name} does not support raw messages")

    def validate(self) -> Optional[str]:
        """Return a human-readable reason this channel cannot run, if any."""
        return None


class SmsChannelImpl(Channel):
    name = "sms"

    def __init__(self, config: SmsChannel, http: HttpClient) -> None:
        super().__init__(http)
        self.config = config

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    @property
    def min_score(self) -> int:
        return self.config.min_score

    @property
    def max_per_hour(self) -> int:
        return self.config.max_per_hour

    def validate(self) -> Optional[str]:
        c = self.config
        missing = [
            n
            for n, v in (
                ("TWILIO_ACCOUNT_SID", c.account_sid),
                ("TWILIO_AUTH_TOKEN", c.auth_token),
                ("TWILIO_FROM_NUMBER", c.from_number),
            )
            if not v
        ]
        if missing:
            return f"missing {', '.join(missing)}"
        if not c.to_numbers:
            return "no recipient numbers (set ALERT_PHONE_NUMBER)"
        return None

    async def send(self, alert: Alert) -> None:
        problem = self.validate()
        if problem:
            raise NotificationError(problem, channel=self.name, permanent=True)

        url = (
            f"https://api.twilio.com/2010-04-01/Accounts/"
            f"{self.config.account_sid}/Messages.json"
        )
        body = alert.sms_text()
        import base64

        token = base64.b64encode(
            f"{self.config.account_sid}:{self.config.auth_token}".encode()
        ).decode()

        for number in self.config.to_numbers:
            response, _ = await self.http.request(
                "POST",
                url,
                data={"From": self.config.from_number, "To": number, "Body": body},
                headers={
                    "Authorization": f"Basic {token}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                use_cache=False,
            )
            if response.status_code >= 300:
                raise NotificationError(
                    f"twilio returned {response.status_code}: {response.text[:200]}",
                    channel=self.name,
                )


class EmailChannelImpl(Channel):
    name = "email"

    def __init__(self, config: EmailChannel, http: HttpClient) -> None:
        super().__init__(http)
        self.config = config

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    @property
    def min_score(self) -> int:
        return self.config.min_score

    @property
    def max_per_hour(self) -> int:
        return self.config.max_per_hour

    def validate(self) -> Optional[str]:
        c = self.config
        if not c.from_address:
            return "missing ALERT_FROM_EMAIL"
        if not c.to_addresses:
            return "no recipients (set ALERT_EMAIL)"
        if c.provider == "sendgrid" and not c.sendgrid_api_key:
            return "missing SENDGRID_API_KEY"
        if c.provider == "smtp":
            # Name only what is actually absent — listing all three sends
            # people hunting for settings that are already correct.
            missing = [
                name
                for name, value in (
                    ("SMTP_HOST", c.smtp_host),
                    ("SMTP_USERNAME", c.smtp_username),
                    ("SMTP_PASSWORD", c.smtp_password),
                )
                if not value
            ]
            if missing:
                hint = ""
                if missing == ["SMTP_PASSWORD"]:
                    hint = " (Gmail needs an App Password: myaccount.google.com/apppasswords)"
                return f"missing {', '.join(missing)}{hint}"
        return None

    async def send(self, alert: Alert) -> None:
        await self.send_raw(alert.email_subject(), alert.email_html(), alert.plain_text())

    async def send_raw(self, subject: str, body_html: str, body_text: str) -> None:
        problem = self.validate()
        if problem:
            raise NotificationError(problem, channel=self.name, permanent=True)
        if self.config.provider == "sendgrid":
            await self._send_sendgrid(subject, body_html, body_text)
        else:
            # smtplib is blocking; keep it off the event loop.
            await asyncio.to_thread(self._send_smtp, subject, body_html, body_text)

    async def _send_sendgrid(self, subject: str, body_html: str, body_text: str) -> None:
        payload: Dict[str, Any] = {
            "personalizations": [
                {"to": [{"email": address} for address in self.config.to_addresses]}
            ],
            "from": {"email": self.config.from_address},
            "subject": subject,
            "content": [
                # SendGrid requires ascending MIME preference: plain first.
                {"type": "text/plain", "value": body_text},
                {"type": "text/html", "value": body_html},
            ],
            # Same filtering handles as the SMTP path.
            "headers": {
                "X-JobMonitor": "alert",
                "List-Id": "jobmonitor alerts <jobmonitor.local>",
                "Auto-Submitted": "auto-generated",
            },
        }
        try:
            response, _ = await self.http.request(
                "POST",
                "https://api.sendgrid.com/v3/mail/send",
                json_body=payload,
                headers={
                    "Authorization": f"Bearer {self.config.sendgrid_api_key}",
                    "Content-Type": "application/json",
                },
                use_cache=False,
            )
        except PermanentError as exc:
            # The HTTP layer turns any 4xx into PermanentError before we can
            # read the status. SendGrid's 401/403 mean a bad key or an
            # unverified sender — worth saying so, and worth not retrying.
            raise NotificationError(
                f"sendgrid rejected the request ({exc}). Check that SENDGRID_API_KEY "
                "is valid and that the From address is a verified sender in SendGrid.",
                channel=self.name,
                permanent=True,
            ) from exc

        # SendGrid accepts with 202 and an empty body.
        if response.status_code >= 300:
            raise NotificationError(
                f"sendgrid returned {response.status_code}: {response.text[:300]}",
                channel=self.name,
            )

    def _send_smtp(self, subject: str, body_html: str, body_text: str) -> None:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self.config.from_address
        message["To"] = ", ".join(self.config.to_addresses)
        # Filtering handles. These matter because the alerts are usually sent
        # from an address to itself, so they land in both Inbox and Sent and
        # there is otherwise nothing to distinguish them from real mail.
        # List-Id in particular is what mail clients key their
        # "mute this list" and rule features off.
        message["X-JobMonitor"] = "alert"
        message["List-Id"] = "jobmonitor alerts <jobmonitor.local>"
        message["Auto-Submitted"] = "auto-generated"
        message.set_content(body_text)
        message.add_alternative(body_html, subtype="html")

        context = ssl.create_default_context()
        try:
            if self.config.smtp_port == 465:
                with smtplib.SMTP_SSL(
                    self.config.smtp_host, self.config.smtp_port, context=context, timeout=30
                ) as server:
                    server.login(self.config.smtp_username, self.config.smtp_password)
                    server.send_message(message)
            else:
                with smtplib.SMTP(
                    self.config.smtp_host, self.config.smtp_port, timeout=30
                ) as server:
                    if self.config.use_tls:
                        server.starttls(context=context)
                    server.login(self.config.smtp_username, self.config.smtp_password)
                    server.send_message(message)
        except smtplib.SMTPAuthenticationError as exc:
            # Wrong credentials will not fix themselves; do not burn retries.
            raise NotificationError(
                f"SMTP authentication failed: {exc}", channel=self.name, permanent=True
            ) from exc
        except smtplib.SMTPException as exc:
            raise NotificationError(f"SMTP error: {exc}", channel=self.name) from exc


class _WebhookBase(Channel):
    def __init__(self, config: WebhookChannel, http: HttpClient) -> None:
        super().__init__(http)
        self.config = config

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    @property
    def min_score(self) -> int:
        return self.config.min_score

    @property
    def max_per_hour(self) -> int:
        return self.config.max_per_hour

    def validate(self) -> Optional[str]:
        if not self.config.url:
            return "no webhook URL configured"
        return None

    async def _post(self, payload: Dict[str, Any]) -> None:
        problem = self.validate()
        if problem:
            raise NotificationError(problem, channel=self.name, permanent=True)
        headers = {"Content-Type": "application/json"}
        headers.update(self.config.headers or {})
        response, _ = await self.http.request(
            "POST", self.config.url, json_body=payload, headers=headers, use_cache=False
        )
        if response.status_code >= 300:
            raise NotificationError(
                f"{self.name} webhook returned {response.status_code}: {response.text[:200]}",
                channel=self.name,
            )


class SlackChannel(_WebhookBase):
    name = "slack"

    async def send(self, alert: Alert) -> None:
        await self._post(
            {
                "text": f"{alert.posting.company}: {alert.posting.title} "
                f"({alert.match.score}%)",
                "blocks": [
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": alert.markdown()},
                    }
                ],
            }
        )

    async def send_raw(self, subject: str, body_html: str, body_text: str) -> None:
        await self._post({"text": f"*{subject}*\n{body_text}"})


class DiscordChannel(_WebhookBase):
    name = "discord"

    async def send(self, alert: Alert) -> None:
        fields = [
            {"name": label, "value": str(value)[:1024], "inline": True}
            for label, value in alert.fields()
            if label not in {"Company", "Title"}
        ]
        if alert.match.top_reasons:
            fields.append(
                {
                    "name": "Why it matched",
                    "value": "\n".join(f"• {r}" for r in alert.match.top_reasons)[:1024],
                    "inline": False,
                }
            )
        await self._post(
            {
                "content": f"**New job — {alert.match.score}% match**",
                "embeds": [
                    {
                        "title": alert.posting.title[:256],
                        "description": alert.posting.company,
                        "url": alert.posting.url,
                        "color": 0x0A7D33 if alert.match.score >= 85 else 0xB06A00,
                        "fields": fields[:25],
                    }
                ],
            }
        )

    async def send_raw(self, subject: str, body_html: str, body_text: str) -> None:
        await self._post({"content": f"**{subject}**\n{body_text[:1800]}"})


class GenericWebhookChannel(_WebhookBase):
    name = "webhook"

    async def send(self, alert: Alert) -> None:
        await self._post(alert.webhook_payload())

    async def send_raw(self, subject: str, body_html: str, body_text: str) -> None:
        await self._post({"event": "digest", "subject": subject, "body": body_text})


class TelegramChannelImpl(Channel):
    name = "telegram"

    def __init__(self, config: TelegramChannel, http: HttpClient) -> None:
        super().__init__(http)
        self.config = config

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    @property
    def min_score(self) -> int:
        return self.config.min_score

    @property
    def max_per_hour(self) -> int:
        return self.config.max_per_hour

    def validate(self) -> Optional[str]:
        if not self.config.bot_token:
            return "missing TELEGRAM_BOT_TOKEN"
        if not self.config.chat_id:
            return "missing TELEGRAM_CHAT_ID"
        return None

    async def send(self, alert: Alert) -> None:
        await self._send_text(alert.telegram_text())

    async def send_raw(self, subject: str, body_html: str, body_text: str) -> None:
        await self._send_text(f"<b>{subject}</b>\n\n{body_text}")

    async def _send_text(self, text: str) -> None:
        problem = self.validate()
        if problem:
            raise NotificationError(problem, channel=self.name, permanent=True)
        response, _ = await self.http.request(
            "POST",
            f"https://api.telegram.org/bot{self.config.bot_token}/sendMessage",
            json_body={
                "chat_id": self.config.chat_id,
                "text": text[:4096],
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            use_cache=False,
        )
        if response.status_code >= 300:
            raise NotificationError(
                f"telegram returned {response.status_code}: {response.text[:200]}",
                channel=self.name,
            )


def build_channels(notifications: Any, http: HttpClient) -> List[Channel]:
    """Instantiate every channel; the dispatcher filters by `enabled`."""
    return [
        SmsChannelImpl(notifications.sms, http),
        EmailChannelImpl(notifications.email, http),
        SlackChannel(notifications.slack, http),
        DiscordChannel(notifications.discord, http),
        TelegramChannelImpl(notifications.telegram, http),
        GenericWebhookChannel(notifications.webhook, http),
    ]
