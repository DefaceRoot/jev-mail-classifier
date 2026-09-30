from __future__ import annotations

import argparse
import imaplib
import sys
import time
from pathlib import Path

from jev_mail.classify import classify, decide
from jev_mail.config import AppConfig, ConfigError, load_config
from jev_mail.mailbox import Mailbox
from jev_mail.providers import ProviderError, get_jev_client
from jev_mail.providers.base import JevClient


def _paths(args: argparse.Namespace) -> tuple[Path, Path]:
    base = Path(args.dir)
    return base / "config.yaml", base / ".env"


def _process_unprocessed(mailbox: Mailbox, client: JevClient, config: AppConfig, dry_run: bool) -> None:
    emails = mailbox.fetch_unprocessed(limit=config.mailbox.batch_size)
    if len(emails) == config.mailbox.batch_size:
        print(
            f"[jev-mail] hit batch_size ({config.mailbox.batch_size}) -- "
            "there may be more unprocessed mail left for next run"
        )
    for mail in emails:
        probabilities = classify(client, config, mail.state())
        decision = decide(config, probabilities)

        if dry_run:
            print(f"[dry-run] {mail.subject!r}: {', '.join(decision.labels)} -> {decision.destination}")

        if not dry_run:
            mailbox.apply(mail.uid, decision)


def _mailbox_error_message(exc: Exception, config: AppConfig) -> str:
    if isinstance(exc, OSError):
        return f"couldn't connect to {config.mailbox.host}:{config.mailbox.port} -- {exc}"
    return f"IMAP error talking to {config.mailbox.host} -- {exc}"


def cmd_run(args: argparse.Namespace) -> int:
    config_path, env_path = _paths(args)
    try:
        config = load_config(config_path, env_path)
        client = get_jev_client(config.jev)
    except (ConfigError, ProviderError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        with Mailbox(config.mailbox) as mailbox:
            _process_unprocessed(mailbox, client, config, args.dry_run)
    except (OSError, imaplib.IMAP4.error) as exc:
        print(f"error: {_mailbox_error_message(exc, config)}", file=sys.stderr)
        return 1
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    config_path, env_path = _paths(args)
    try:
        config = load_config(config_path, env_path)
        client = get_jev_client(config.jev)
    except (ConfigError, ProviderError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"watching {config.mailbox.folder}@{config.mailbox.host} (ctrl-c to stop)...")
    try:
        with Mailbox(config.mailbox) as mailbox:
            _process_unprocessed(mailbox, client, config, args.dry_run)
            while True:
                if mailbox.supports_idle():
                    mailbox.idle()
                    mailbox.idle_check(timeout=min(config.mailbox.poll_interval_seconds, 600))
                    mailbox.idle_done()
                else:
                    time.sleep(config.mailbox.poll_interval_seconds)
                _process_unprocessed(mailbox, client, config, args.dry_run)
    except (OSError, imaplib.IMAP4.error) as exc:
        print(f"error: {_mailbox_error_message(exc, config)}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jev-mail", description="Classify your inbox with Jev.")
    parser.add_argument("--dir", default=".", help="directory holding config.yaml / .env (default: cwd)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="classify unprocessed mail once and exit")
    run_parser.add_argument("--dry-run", action="store_true", help="classify and print, without applying any action")

    watch_parser = subparsers.add_parser("watch", help="keep classifying new mail as it arrives")
    watch_parser.add_argument("--dry-run", action="store_true", help="classify and print, without applying any action")

    return parser


def main() -> None:
    args = build_parser().parse_args()
    commands = {"run": cmd_run, "watch": cmd_watch}
    sys.exit(commands[args.command](args))


if __name__ == "__main__":
    main()
