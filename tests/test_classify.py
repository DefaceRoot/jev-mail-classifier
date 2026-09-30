from unittest.mock import MagicMock

from jev_mail.classify import classify, matched_categories
from jev_mail.config import AppConfig, Category, JevSettings, MailboxConfig


def _config(**category_overrides) -> AppConfig:
    return AppConfig(
        mailbox=MailboxConfig(host="imap.example.com"),
        jev=JevSettings(default_threshold=0.6),
        categories=[
            Category(name="invoice", description="Invoice"),
            Category(
                name="urgent",
                description="Urgent",
                threshold=category_overrides.get("urgent_threshold", 0.6),
            ),
        ],
    )


def test_classify_builds_category_dict_and_delegates_to_client():
    fake_client = MagicMock()
    fake_client.decide.return_value = {"invoice": 0.9, "urgent": 0.1}
    config = _config()

    result = classify(fake_client, config, "some email state")

    fake_client.decide.assert_called_once_with("some email state", {"invoice": "Invoice", "urgent": "Urgent"})
    assert result == {"invoice": 0.9, "urgent": 0.1}


def test_matched_categories_uses_default_threshold():
    config = _config()
    matched = matched_categories(config, {"invoice": 0.9, "urgent": 0.5})
    assert [c.name for c in matched] == ["invoice"]


def test_matched_categories_uses_per_category_threshold_override():
    config = _config(urgent_threshold=0.3)
    matched = matched_categories(config, {"invoice": 0.1, "urgent": 0.5})
    assert [c.name for c in matched] == ["urgent"]
