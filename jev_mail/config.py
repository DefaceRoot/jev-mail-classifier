from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, get_args

import yaml
from dotenv import load_dotenv

_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

Disposition = Literal["keep", "archive", "quarantine"]
DISPOSITIONS: tuple[str, ...] = get_args(Disposition)
SECURITY_MODES = ("ssl", "starttls")


class ConfigError(Exception):
    """Raised for a malformed or incomplete config.yaml."""


@dataclass(frozen=True)
class Category:
    name: str
    label: str
    description: str
    threshold: float | None = None
    disposition: Disposition | None = None
    disposition_threshold: float | None = None


@dataclass(frozen=True)
class Decision:
    labels: tuple[str, ...]
    destination: str | None
    probabilities: dict[str, float] = field(default_factory=dict)


@dataclass
class MailboxConfig:
    host: str
    port: int = 993
    security: str = "ssl"
    tls_verify: bool = True
    username: str = ""
    password: str = ""
    watch_folders: list[str] = field(default_factory=lambda: ["INBOX"])
    batch_size: int = 25
    max_fetch_bytes: int = 524288
    poll_interval_seconds: int = 600
    label_folder: str = "JEV/{label}"
    folders: dict[str, str] = field(default_factory=dict)


@dataclass
class JevSettings:
    provider: str = "auto"
    model: str | None = None
    default_threshold: float = 0.7


@dataclass
class AppConfig:
    mailbox: MailboxConfig
    jev: JevSettings
    categories: list[Category]
    unmatched_label: str = "REVIEW"

    def category_threshold(self, category: Category) -> float:
        return category.threshold if category.threshold is not None else self.jev.default_threshold

    def disposition_threshold(self, category: Category) -> float:
        if category.disposition_threshold is not None:
            return category.disposition_threshold
        return self.category_threshold(category)

    def label_folder(self, label: str) -> str:
        return self.mailbox.label_folder.format(label=label)

    def writable_folders(self) -> list[str]:
        """Every folder a Decision can COPY or MOVE into, in a stable order."""
        names = [self.label_folder(c.label) for c in self.categories]
        names.append(self.label_folder(self.unmatched_label))
        names.extend(self.mailbox.folders.values())
        return list(dict.fromkeys(names))


def _interpolate(value: str, env: dict) -> str:
    def repl(match: re.Match) -> str:
        var = match.group(1)
        if var not in env:
            raise ConfigError(f"config references ${{{var}}} but it isn't set (check your .env)")
        return env[var]

    return _VAR_PATTERN.sub(repl, value) if isinstance(value, str) else value


def _parse_category(raw: object) -> Category:
    if not isinstance(raw, dict):
        raise ConfigError(f"each entry in categories must be a mapping, got {raw!r}")
    for key in ("name", "label", "description"):
        if not raw.get(key):
            raise ConfigError(f"category {raw.get('name', raw)!r} is missing {key!r}")
    disposition = raw.get("disposition")
    if disposition is not None and disposition not in DISPOSITIONS:
        raise ConfigError(
            f"category {raw['name']!r} has disposition {disposition!r}; expected one of {', '.join(DISPOSITIONS)}"
        )
    threshold = raw.get("threshold")
    disposition_threshold = raw.get("disposition_threshold")
    if disposition_threshold is not None and disposition is None:
        raise ConfigError(f"category {raw['name']!r} sets disposition_threshold without a disposition")
    return Category(
        name=raw["name"],
        label=raw["label"],
        description=raw["description"],
        threshold=float(threshold) if threshold is not None else None,
        disposition=disposition,
        disposition_threshold=float(disposition_threshold) if disposition_threshold is not None else None,
    )


def load_config(config_path: str | Path, env_path: str | Path | None = None) -> AppConfig:
    config_path = Path(config_path)
    load_dotenv(env_path or config_path.parent / ".env", override=False)

    if not config_path.exists():
        raise ConfigError(f"no config file at {config_path}")

    raw = yaml.safe_load(config_path.read_text()) or {}
    env = dict(os.environ)

    mb_raw = raw.get("mailbox") or {}
    if "folder" in mb_raw:
        raise ConfigError("mailbox.folder was replaced by mailbox.watch_folders (a list)")
    mailbox = MailboxConfig(
        host=_interpolate(mb_raw.get("host", ""), env),
        port=int(mb_raw.get("port", 993)),
        security=mb_raw.get("security", "ssl"),
        tls_verify=bool(mb_raw.get("tls_verify", True)),
        username=_interpolate(mb_raw.get("username", ""), env),
        password=_interpolate(mb_raw.get("password", ""), env),
        watch_folders=mb_raw.get("watch_folders", ["INBOX"]),
        batch_size=int(mb_raw.get("batch_size", 25)),
        max_fetch_bytes=int(mb_raw.get("max_fetch_bytes", 524288)),
        poll_interval_seconds=int(mb_raw.get("poll_interval_seconds", 600)),
        label_folder=mb_raw.get("label_folder", "JEV/{label}"),
        folders=dict(mb_raw.get("folders") or {}),
    )
    if not mailbox.host:
        raise ConfigError("mailbox.host is empty")
    folders = mailbox.watch_folders
    if not isinstance(folders, list) or not folders or not all(isinstance(f, str) and f for f in folders):
        raise ConfigError("mailbox.watch_folders must be a non-empty list of folder names")
    if len(set(folders)) != len(folders):
        raise ConfigError("mailbox.watch_folders has duplicates")
    if mailbox.security not in SECURITY_MODES:
        raise ConfigError(f"mailbox.security must be one of {', '.join(SECURITY_MODES)}, got {mailbox.security!r}")
    if "{label}" not in mailbox.label_folder:
        raise ConfigError("mailbox.label_folder must contain {label}")
    if mailbox.batch_size < 1:
        raise ConfigError("mailbox.batch_size must be at least 1")
    unknown_folders = set(mailbox.folders) - {"archive", "quarantine"}
    if unknown_folders:
        raise ConfigError(f"unknown mailbox.folders key(s): {', '.join(sorted(unknown_folders))}")

    jev_raw = raw.get("jev") or {}
    jev = JevSettings(
        provider=jev_raw.get("provider", "auto"),
        model=jev_raw.get("model"),
        default_threshold=float(jev_raw.get("default_threshold", 0.7)),
    )

    raw_categories = raw.get("categories")
    if isinstance(raw_categories, dict):
        raise ConfigError("categories must be a list of {name, label, description, ...} entries, not a mapping")
    categories = [_parse_category(c) for c in raw_categories or []]
    if not categories:
        raise ConfigError("config has no categories")
    names = [c.name for c in categories]
    if len(set(names)) != len(names):
        raise ConfigError("category names must be unique")
    for category in categories:
        label_threshold = category.threshold if category.threshold is not None else jev.default_threshold
        if category.disposition_threshold is not None and category.disposition_threshold < label_threshold:
            raise ConfigError(
                f"category {category.name!r} has disposition_threshold {category.disposition_threshold} "
                f"below its label threshold {label_threshold}"
            )
        if category.disposition in ("archive", "quarantine") and category.disposition not in mailbox.folders:
            raise ConfigError(
                f"category {category.name!r} uses disposition {category.disposition!r} "
                f"but mailbox.folders.{category.disposition} isn't set"
            )

    return AppConfig(
        mailbox=mailbox,
        jev=jev,
        categories=categories,
        unmatched_label=raw.get("unmatched_label", "REVIEW"),
    )
