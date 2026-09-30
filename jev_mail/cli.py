from __future__ import annotations

import argparse
import imaplib
import os
import sqlite3
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from jev_mail.classify import classify, decide
from jev_mail.config import AppConfig, ConfigError, Decision, load_config
from jev_mail.email_state import MIN_BODY_CHARS, Email
from jev_mail.mailbox import Mailbox
from jev_mail.providers import EmailRejected, InputTooLong, ProviderError, get_jev_client
from jev_mail.providers.base import JevClient
from jev_mail.store import ProcessedStore


MAX_IDLE_SECONDS = 600
RETRY_DELAYS_SECONDS = (10, 30, 90)


def _write_heartbeat(config: AppConfig) -> None:
    """Atomically stamps `heartbeat` next to state_path with the current UTC
    time, so a host watchdog can tell a live watcher from a hung one."""
    path = Path(config.state_path).parent / "heartbeat"
    tmp = path.with_name("heartbeat.tmp")
    tmp.write_text(datetime.now(timezone.utc).isoformat())
    os.replace(tmp, path)


def _paths(args: argparse.Namespace) -> tuple[Path, Path]:
    base = Path(args.dir)
    return base / "config.yaml", base / ".env"


def _load(args: argparse.Namespace) -> AppConfig:
    config_path, env_path = _paths(args)
    config = load_config(config_path, env_path)
    if args.folders:
        config.mailbox.watch_folders = list(dict.fromkeys(args.folders))
    return config


def _classify_with_retry(
    client: JevClient, config: AppConfig, mail: Email, body_limit: int | None, sleep: Callable[[float], None]
) -> dict[str, float]:
    """A plain ProviderError is transient: retry after each delay in
    RETRY_DELAYS_SECONDS, then let the last one propagate."""
    for attempt, delay in enumerate(RETRY_DELAYS_SECONDS, start=1):
        try:
            return classify(client, config, mail.state(body_limit))
        except (InputTooLong, EmailRejected):
            raise
        except ProviderError as exc:
            print(
                f"uid={mail.uid} Jev call failed ({exc}), retry {attempt}/{len(RETRY_DELAYS_SECONDS)} in {delay}s",
                file=sys.stderr,
                flush=True,
            )
            _write_heartbeat(config)
            sleep(delay)
    return classify(client, config, mail.state(body_limit))


def _decide_for(
    client: JevClient, config: AppConfig, mail: Email, sleep: Callable[[float], None] = time.sleep
) -> Decision:
    """Ask Jev, halving the body until it fits. Mail Jev can't take at all
    gets the unmatched label, so one poison email never wedges the watcher.
    Transient failures are retried, then propagate."""
    body_limit: int | None = None
    try:
        while True:
            try:
                return decide(config, _classify_with_retry(client, config, mail, body_limit, sleep))
            except InputTooLong:
                size = len(mail.body) if body_limit is None else body_limit
                if size <= MIN_BODY_CHARS:
                    raise
                body_limit = max(size // 2, MIN_BODY_CHARS)
    except (InputTooLong, EmailRejected) as exc:
        print(f"uid={mail.uid} can't be classified, labelling it for review: {exc}", file=sys.stderr, flush=True)
        return Decision(labels=(config.label_folder(config.unmatched_label),), destination=None)


def _log_line(mail: Email, decision: Decision, dry_run: bool) -> str:
    top = sorted(decision.probabilities.items(), key=lambda item: -item[1])[:3]
    return (
        f"{'[dry-run] ' if dry_run else ''}uid={mail.uid} subject={mail.subject[:80]!r} "
        f"labels={','.join(decision.labels)} dest={decision.destination or '-'} "
        f"top={' '.join(f'{name}:{p:.2f}' for name, p in top) or '-'}"
    )


def _process_batch(
    mailbox: Mailbox,
    store: ProcessedStore,
    client: JevClient,
    config: AppConfig,
    limit: int,
    dry_run: bool,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    emails = mailbox.fetch_unprocessed(limit=limit)
    for mail in emails:
        decision = _decide_for(client, config, mail, sleep)
        if not dry_run:
            mailbox.apply(mail.uid, decision)
            store.record(mail.message_key, decision)
        print(_log_line(mail, decision, dry_run), flush=True)
        _write_heartbeat(config)
    return len(emails)


def _drain(
    mailbox: Mailbox,
    store: ProcessedStore,
    client: JevClient,
    config: AppConfig,
    dry_run: bool,
    limit: int | None = None,
) -> None:
    """Each watch folder in order: process batches until a short one. A dry run
    records nothing, so it would see the same mail forever: it handles exactly
    one batch per folder."""
    batch_size = limit or config.mailbox.batch_size
    for folder in config.mailbox.watch_folders:
        mailbox.select(folder)
        while True:
            count = _process_batch(mailbox, store, client, config, batch_size, dry_run)
            if dry_run or count < batch_size:
                break


def _open_store(config: AppConfig) -> ProcessedStore:
    Path(config.state_path).parent.mkdir(parents=True, exist_ok=True)
    return ProcessedStore(config.state_path)


def _open_store_or_report(config: AppConfig) -> ProcessedStore | None:
    try:
        return _open_store(config)
    except (OSError, sqlite3.Error) as exc:
        print(f"error: can't open state_path {config.state_path!r}: {exc}", file=sys.stderr)
        return None


def _mailbox_error_message(exc: Exception, config: AppConfig) -> str:
    if isinstance(exc, OSError):
        return f"couldn't connect to {config.mailbox.host}:{config.mailbox.port} -- {exc}"
    return f"IMAP error talking to {config.mailbox.host} -- {exc}"


def _start(args: argparse.Namespace) -> tuple[AppConfig, JevClient] | None:
    try:
        config = _load(args)
        return config, get_jev_client(config.jev)
    except (ConfigError, ProviderError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None


def cmd_run(args: argparse.Namespace) -> int:
    started = _start(args)
    if started is None:
        return 1
    config, client = started
    store = _open_store_or_report(config)
    if store is None:
        return 1

    try:
        with store, Mailbox(config.mailbox, store) as mailbox:
            if not args.dry_run:
                mailbox.ensure_folders(config.writable_folders())
            _drain(mailbox, store, client, config, args.dry_run, args.limit)
    except (OSError, imaplib.IMAP4.error) as exc:
        print(f"error: {_mailbox_error_message(exc, config)}", file=sys.stderr)
        return 1
    except (ProviderError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    started = _start(args)
    if started is None:
        return 1
    config, client = started
    store = _open_store_or_report(config)
    if store is None:
        return 1

    folders = config.mailbox.watch_folders
    print(f"watching {folders} on {config.mailbox.host} (ctrl-c to stop)...", flush=True)
    try:
        with store, Mailbox(config.mailbox, store) as mailbox:
            _write_heartbeat(config)
            if not args.dry_run:
                mailbox.ensure_folders(config.writable_folders())
            while True:
                _drain(mailbox, store, client, config, args.dry_run)
                mailbox.select(folders[0])
                if mailbox.supports_idle():
                    mailbox.idle()
                    mailbox.idle_check(timeout=min(config.mailbox.poll_interval_seconds, MAX_IDLE_SECONDS))
                    mailbox.idle_done()
                else:
                    time.sleep(min(config.mailbox.poll_interval_seconds, MAX_IDLE_SECONDS))
                _write_heartbeat(config)
    except (OSError, imaplib.IMAP4.error) as exc:
        print(f"error: {_mailbox_error_message(exc, config)}", file=sys.stderr)
        return 1
    except (ProviderError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def cmd_check(args: argparse.Namespace) -> int:
    """Makes no mailbox writes. Verifies the watch and archive folders exist and
    that the state store can be opened for writing, and reports per-folder counts."""
    try:
        config = _load(args)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    problems: list[str] = []
    store: ProcessedStore | None = None
    try:
        store = _open_store(config)
    except (OSError, sqlite3.Error) as exc:
        problems.append(f"state_path {config.state_path!r} is not writable: {exc}")

    try:
        with Mailbox(config.mailbox, store) as mailbox:
            print(f"connected to {config.mailbox.host}:{config.mailbox.port}")
            print(f"capabilities: {' '.join(mailbox.capabilities())}")
            for folder in config.mailbox.watch_folders:
                if not mailbox.folder_exists(folder):
                    problems.append(f"watch folder {folder!r} does not exist on the server")
                elif store is not None:
                    mailbox.select(folder)
                    total, unprocessed = mailbox.counts()
                    print(f"{folder!r}: {total} messages, {unprocessed} unprocessed")
            archive = config.mailbox.folders.get("archive")
            if archive is not None and not mailbox.folder_exists(archive):
                problems.append(f"folders.archive {archive!r} does not exist on the server")
    except (OSError, imaplib.IMAP4.error) as exc:
        print(f"error: {_mailbox_error_message(exc, config)}", file=sys.stderr)
        return 1
    finally:
        if store is not None:
            store.close()

    for problem in problems:
        print(f"FAIL: {problem}", file=sys.stderr)
    if not problems:
        print(f"ok: folders exist and {config.state_path!r} is writable")
    return 1 if problems else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jev-mail", description="Label and sort your inbox with Jev.")
    parser.add_argument("--dir", default=".", help="directory holding config.yaml / .env (default: cwd)")
    parser.add_argument(
        "--folder",
        dest="folders",
        action="append",
        help="IMAP folder to process, repeatable; replaces mailbox.watch_folders (e.g. 'Folders/Bay Bravo')",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="classify all unprocessed mail, then exit")
    run_parser.add_argument("--dry-run", action="store_true", help="classify one batch and print, without touching the mailbox")
    run_parser.add_argument("--limit", type=int, help="dry-run sample size (default: mailbox.batch_size)")

    watch_parser = subparsers.add_parser("watch", help="drain the backlog, then keep classifying new mail (IMAP IDLE)")
    watch_parser.add_argument("--dry-run", action="store_true", help="classify and print, without touching the mailbox")

    subparsers.add_parser("check", help="verify the connection, folders and state store without writing to the mailbox")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "run" and args.limit is not None and not args.dry_run:
        parser.error("--limit only applies with --dry-run")
    commands = {"run": cmd_run, "watch": cmd_watch, "check": cmd_check}
    sys.exit(commands[args.command](args))


if __name__ == "__main__":
    main()
