from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(Exception):
    """Raised for a malformed or incomplete config.yaml."""


@dataclass
class Category:
    name: str
    description: str
    threshold: float | None = None


@dataclass
class MailboxConfig:
    host: str
    port: int = 993
    username: str = ""
    password: str = ""
    folder: str = "INBOX"
    poll_interval_seconds: int = 60
    max_emails_per_run: int = 25


@dataclass
class JevSettings:
    provider: str = "auto"
    default_threshold: float = 0.6


@dataclass
class AppConfig:
    mailbox: MailboxConfig
    jev: JevSettings
    categories: list[Category]

    def category_threshold(self, category: Category) -> float:
        return category.threshold if category.threshold is not None else self.jev.default_threshold


def _interpolate(value: str, env: dict) -> str:
    def repl(match: re.Match) -> str:
        var = match.group(1)
        if var not in env:
            raise ConfigError(f"config references ${{{var}}} but it isn't set (check your .env)")
        return env[var]

    return _VAR_PATTERN.sub(repl, value) if isinstance(value, str) else value


def load_config(config_path: str | Path, env_path: str | Path | None = None) -> AppConfig:
    config_path = Path(config_path)
    load_dotenv(env_path or config_path.parent / ".env", override=False)

    if not config_path.exists():
        raise ConfigError(f"no config file at {config_path}")

    raw = yaml.safe_load(config_path.read_text()) or {}
    env = dict(os.environ)

    mb_raw = raw.get("mailbox", {})
    mailbox = MailboxConfig(
        host=_interpolate(mb_raw.get("host", ""), env),
        port=int(mb_raw.get("port", 993)),
        username=_interpolate(mb_raw.get("username", ""), env),
        password=_interpolate(mb_raw.get("password", ""), env),
        folder=mb_raw.get("folder", "INBOX"),
        poll_interval_seconds=int(mb_raw.get("poll_interval_seconds", 60)),
        max_emails_per_run=int(mb_raw.get("max_emails_per_run", 25)),
    )
    if not mailbox.host:
        raise ConfigError("mailbox.host is empty")

    jev_raw = raw.get("jev", {})
    jev = JevSettings(
        provider=jev_raw.get("provider", "auto"),
        default_threshold=float(jev_raw.get("default_threshold", 0.6)),
    )

    categories = [
        Category(name=name, description=cat_raw.get("description", ""), threshold=cat_raw.get("threshold"))
        for name, cat_raw in (raw.get("categories") or {}).items()
    ]
    if not categories:
        raise ConfigError("config has no categories")

    return AppConfig(mailbox=mailbox, jev=jev, categories=categories)
