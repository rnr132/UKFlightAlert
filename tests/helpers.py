"""Shared test doubles. Imported by test modules (tests/ is on sys.path in
pytest's default import mode); fixtures that need monkeypatch live in the
test modules or conftest.py."""
import smtplib

CFG = {
    "notify": {
        "smtp_host": "smtp.test",
        "smtp_port": 587,
        "smtp_username": "resend",
        "from_address": "deals@example.test",
        "from_name": "London Flight Deals",
        "password_env_var": "SMTP_PASSWORD",
        "recipients_env_var": "NOTIFY_RECIPIENTS",
        "reply_to_env_var": "REPLY_TO",
    },
    "detection": {"drop_pct_threshold": 0.2},
    "monitoring": {"staleness_warning_hours": 36},
}


class FakeSMTP:
    """Records what a real SMTP session would have been asked to do."""

    instances = []
    refuse = set()  # addresses that get a 550 for the recipient
    drop_after = None  # raise SMTPServerDisconnected from the Nth sendmail on (0-based)

    def __init__(self, host, port):
        self.sent = []  # (envelope_from, [envelope_to], raw_message)
        self.calls = 0
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        pass

    def login(self, user, password):
        pass

    def sendmail(self, from_addr, to_addrs, raw):
        n = self.calls
        self.calls += 1
        if FakeSMTP.drop_after is not None and n >= FakeSMTP.drop_after:
            raise smtplib.SMTPServerDisconnected("connection unexpectedly closed")
        refused = {a: (550, b"mailbox unavailable") for a in to_addrs if a in FakeSMTP.refuse}
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)
        self.sent.append((from_addr, list(to_addrs), raw))
        return {}

    @classmethod
    def reset(cls):
        cls.instances = []
        cls.refuse = set()
        cls.drop_after = None


def sent_messages():
    return [m for inst in FakeSMTP.instances for m in inst.sent]
