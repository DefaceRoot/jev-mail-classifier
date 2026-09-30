from __future__ import annotations

from jev_mail.config import AppConfig, Category, JevSettings, MailboxConfig

DEFAULT_STATE_PATH = "/state/jev-mail.sqlite"
PROTON_FOLDERS = {"archive": "Archive", "quarantine": "Folders/JEV Quarantine"}
GMAIL_FOLDERS = {"archive": "[Gmail]/All Mail", "quarantine": "JEV/QUARANTINE"}

CATEGORIES = [
    Category("scam", "SCAM", "Scam or phishing", threshold=0.8, disposition="quarantine"),
    Category("security", "SECURITY", "Security alert", disposition="quarantine"),
    Category("phishing", "PHISHING", "Likely phishing", threshold=0.5, disposition="quarantine", disposition_threshold=0.8),
    Category("action", "ACTION", "Needs a reply from me", disposition="keep"),
    Category("cold_outreach", "COLD", "Cold sales pitch", disposition="archive"),
    Category("receipt", "RECEIPT", "Receipt or invoice"),
    Category("spam", "SPAM", "Spam", threshold=0.7, disposition="archive", disposition_threshold=0.9),
]


def make_config(gmail: bool = False, state_path: str | None = None, **mailbox_overrides) -> AppConfig:
    mailbox = MailboxConfig(
        host="imap.example.com",
        label_folder="JEV/{label}" if gmail else "Labels/JEV-{label}",
        folders=dict(GMAIL_FOLDERS if gmail else PROTON_FOLDERS),
        **mailbox_overrides,
    )
    return AppConfig(
        mailbox=mailbox,
        jev=JevSettings(default_threshold=0.7),
        categories=list(CATEGORIES),
        state_path=state_path or DEFAULT_STATE_PATH,
    )


def probs(**overrides: float) -> dict[str, float]:
    base = {c.name: 0.0 for c in CATEGORIES}
    base.update(overrides)
    return base
