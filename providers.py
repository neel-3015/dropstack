"""Notification provider layer.

Interfaces: SmsProvider / EmailProvider.
Implementations:
  - MockProvider (default): logs to console AND appends JSON lines to
    notifications.log.jsonl so smoke tests can assert on sends.
  - TwilioSmsProvider / PostmarkEmailProvider: production stubs. They
    read credentials from env vars and raise NotImplementedError until
    wired against the real APIs (documented in README).

Selection via env: SMS_PROVIDER=mock|twilio, EMAIL_PROVIDER=mock|postmark
"""
import json
import os
import time

LOG_FILE = os.environ.get("NOTIFICATIONS_LOG", "notifications.log.jsonl")


class SmsProvider:
    def send(self, to: str, body: str, subject: str = "") -> None:
        raise NotImplementedError


class EmailProvider:
    def send(self, to: str, body: str, subject: str = "") -> None:
        raise NotImplementedError


class _FileConsoleMock:
    channel = "mock"

    def _log(self, kind: str, to: str, subject: str, body: str) -> None:
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "channel": kind,
               "to": to, "subject": subject, "body": body}
        print(f"[notify:{kind}] to={to} subject={subject!r} body={body!r}")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")


class MockSms(_FileConsoleMock, SmsProvider):
    def send(self, to: str, body: str, subject: str = "") -> None:
        self._log("sms", to, subject, body)


class MockEmail(_FileConsoleMock, EmailProvider):
    def send(self, to: str, body: str, subject: str = "") -> None:
        self._log("email", to, subject, body)


class TwilioSmsProvider(SmsProvider):
    """Stub. Wire POST https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json
    Env: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER"""

    def send(self, to: str, body: str, subject: str = "") -> None:
        self.sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
        self.token = os.environ.get("TWILIO_AUTH_TOKEN", "")
        self.src = os.environ.get("TWILIO_FROM_NUMBER", "")
        if not (self.sid and self.token and self.src):
            raise RuntimeError("Twilio credentials missing in env")
        raise NotImplementedError("Twilio integration not built yet (see README)")


class PostmarkEmailProvider(EmailProvider):
    """Stub. Wire POST https://api.postmarkapp.com/email
    Env: POSTMARK_SERVER_TOKEN, POSTMARK_FROM_EMAIL"""

    def send(self, to: str, body: str, subject: str = "") -> None:
        self.token = os.environ.get("POSTMARK_SERVER_TOKEN", "")
        self.src = os.environ.get("POSTMARK_FROM_EMAIL", "")
        if not (self.token and self.src):
            raise RuntimeError("Postmark credentials missing in env")
        raise NotImplementedError("Postmark integration not built yet (see README)")


def get_sms_provider() -> SmsProvider:
    p = os.environ.get("SMS_PROVIDER", "mock").lower()
    if p == "twilio":
        return TwilioSmsProvider()
    return MockSms()


def get_email_provider() -> EmailProvider:
    p = os.environ.get("EMAIL_PROVIDER", "mock").lower()
    if p == "postmark":
        return PostmarkEmailProvider()
    return MockEmail()
