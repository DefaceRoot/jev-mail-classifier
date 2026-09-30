import os

import pytest

from jev_mail.config import (
    AppConfig,
    Category,
    ConfigError,
    JevSettings,
    MailboxConfig,
    load_config,
)


def test_load_config_interpolates_env_and_parses_categories(tmp_path, monkeypatch):
    monkeypatch.setenv("IMAP_USERNAME", "me@example.com")
    monkeypatch.setenv("IMAP_PASSWORD", "secret")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
mailbox:
  host: imap.example.com
  username: ${IMAP_USERNAME}
  password: ${IMAP_PASSWORD}
jev:
  default_threshold: 0.7
categories:
  invoice:
    description: "Invoice or billing"
"""
    )

    config = load_config(config_path, env_path=tmp_path / "does-not-exist.env")

    assert config.mailbox.username == "me@example.com"
    assert config.mailbox.password == "secret"
    assert config.jev.default_threshold == 0.7
    assert len(config.categories) == 1
    assert config.categories[0].name == "invoice"


def test_load_config_max_emails_per_run_defaults_and_overrides(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text('mailbox:\n  host: imap.example.com\ncategories:\n  spam:\n    description: spam\n    actions: []\n')
    config = load_config(config_path, env_path=tmp_path / "does-not-exist.env")
    assert config.mailbox.max_emails_per_run == 25

    config_path.write_text(
        "mailbox:\n  host: imap.example.com\n  max_emails_per_run: 5\n"
        "categories:\n  spam:\n    description: spam\n    actions: []\n"
    )
    config = load_config(config_path, env_path=tmp_path / "does-not-exist.env")
    assert config.mailbox.max_emails_per_run == 5


def test_load_config_empty_host_raises(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("mailbox:\n  host: ''\ncategories:\n  spam:\n    description: spam\n    actions: []\n")
    with pytest.raises(ConfigError):
        load_config(config_path, env_path=tmp_path / "does-not-exist.env")


def test_load_config_missing_env_var_raises(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
mailbox:
  host: imap.example.com
  username: ${DEFINITELY_NOT_SET}
categories:
  spam:
    description: spam
"""
    )
    with pytest.raises(ConfigError):
        load_config(config_path, env_path=tmp_path / "does-not-exist.env")


def test_load_config_no_categories_raises(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("mailbox:\n  host: imap.example.com\ncategories: {}\n")
    with pytest.raises(ConfigError):
        load_config(config_path, env_path=tmp_path / "does-not-exist.env")


def test_load_config_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.yaml")
