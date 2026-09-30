from __future__ import annotations

import argparse
import imaplib
import sys
import time
from pathlib import Path

from jev_mail.classify import classify, decide
from jev_mail.config import AppConfig, ConfigError, Decision, load_config
from jev_mail.email_state import MIN_BODY_CHARS, Email
from jev_mail.mailbox import PROCESSED_KEYWORD, Mailbox
from jev_mail.providers import EmailRejected, InputTooLong, ProviderError, get_jev_client
from jev_mail.providers.base import JevClient


class MarkerNotSticking(Exception):
    """The same (folder, UID) came back as unprocessed after being handled."""


PROBE_KEYWORD = "$JevProbe"
MAX_IDLE_SECONDS = 600


def _paths(args: argparse.Namespace) -> tuple[Path, Path]:
    base = Path(args.dir)
    return base / "config.yaml", base / ".env"


def _load(args: argparse.Namespace) -> AppConfig:
    config_path, env_path = _paths(args)
    config = load_config(config_path, env_path)
    if args.folders:
        config.mailbox.watch_folders = list(dict.fromkeys(args.folders))
    return config


def _decide_for(client: JevClient, config: AppConfig, mail: Email) -> Decision:
    """Ask Jev, halving the body until it fits. Mail Jev can't take at all
    gets the unmatched label, so one poison email never wedges the watcher.
    Transient failures are plain ProviderErrors and propagate."""
    body_limit: int | None = None
    try:
        while True:
            try:
                return decide(config, classify(client, config, mail.state(body_limit)))
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
    client: JevClient,
    config: AppConfig,
    folder: str,
    limit: int,
    dry_run: bool,
    done: set[tuple[str, int]],
) -> int:
    emails = mailbox.fetch_unprocessed(limit=limit)
    repeated = sorted(uid for uid in (mail.uid for mail in emails) if (folder, uid) in done)
    if repeated and not dry_run:
        raise MarkerNotSticking(
            f"{folder!r} uids {repeated} are still unprocessed after being handled; the server isn't keeping "
            f"{PROCESSED_KEYWORD} (run `jev-mail check`)"
        )
    for mail in emails:
        decision = _decide_for(client, config, mail)
        if not dry_run:
            mailbox.apply(mail.uid, decision)
        print(_log_line(mail, decision, dry_run), flush=True)
        done.add((folder, mail.uid))
    return len(emails)


def _drain(
    mailbox: Mailbox,
    client: JevClient,
    config: AppConfig,
    dry_run: bool,
    done: set[tuple[str, int]],
    limit: int | None = None,
) -> None:
    """Each watch folder in order: process batches until a short one. A dry run
    sets no markers, so it would see the same mail forever: it handles exactly
    one batch per folder. `done` holds (folder, uid) pairs already handled;
    seeing one again means the server dropped our keyword, and continuing would
    reclassify (and re-bill) the same mail forever."""
    batch_size = limit or config.mailbox.batch_size
    for folder in config.mailbox.watch_folders:
        mailbox.select(folder)
        while True:
            count = _process_batch(mailbox, client, config, folder, batch_size, dry_run, done)
            if dry_run or count < batch_size:
                break


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

    try:
        with Mailbox(config.mailbox) as mailbox:
            if not args.dry_run:
                mailbox.ensure_folders(config.writable_folders())
            _drain(mailbox, client, config, args.dry_run, set(), args.limit)
    except (OSError, imaplib.IMAP4.error) as exc:
        print(f"error: {_mailbox_error_message(exc, config)}", file=sys.stderr)
        return 1
    except (ProviderError, MarkerNotSticking) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    started = _start(args)
    if started is None:
        return 1
    config, client = started

    folders = config.mailbox.watch_folders
    print(f"watching {folders} on {config.mailbox.host} (ctrl-c to stop)...", flush=True)
    done: set[tuple[str, int]] = set()
    try:
        with Mailbox(config.mailbox) as mailbox:
            if not args.dry_run:
                mailbox.ensure_folders(config.writable_folders())
            while True:
                _drain(mailbox, client, config, args.dry_run, done)
                mailbox.select(folders[0])
                if mailbox.supports_idle():
                    mailbox.idle()
                    mailbox.idle_check(timeout=min(config.mailbox.poll_interval_seconds, MAX_IDLE_SECONDS))
                    mailbox.idle_done()
                else:
                    time.sleep(min(config.mailbox.poll_interval_seconds, MAX_IDLE_SECONDS))
    except (OSError, imaplib.IMAP4.error) as exc:
        print(f"error: {_mailbox_error_message(exc, config)}", file=sys.stderr)
        return 1
    except (ProviderError, MarkerNotSticking) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def cmd_check(args: argparse.Namespace) -> int:
    """Read-only apart from a $JevProbe keyword on the newest message, which is
    removed again. Proves the server keeps custom keywords across sessions,
    because $JevProcessed is the only state this tool has."""
    try:
        config = _load(args)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    problems: list[str] = []
    folders = config.mailbox.watch_folders
    probe_uid: int | None = None
    try:
        with Mailbox(config.mailbox) as mailbox:
            print(f"connected to {config.mailbox.host}:{config.mailbox.port}")
            print(f"capabilities: {' '.join(mailbox.capabilities())}")
            for folder in folders:
                if not mailbox.folder_exists(folder):
                    problems.append(f"watch folder {folder!r} does not exist on the server")
                    continue
                mailbox.select(folder)
                print(f"{folder!r}: {len(mailbox.unprocessed_uids())} unprocessed messages")
            archive = config.mailbox.folders.get("archive")
            if archive is not None and not mailbox.folder_exists(archive):
                problems.append(f"folders.archive {archive!r} does not exist on the server")
            if mailbox.folder_exists(folders[0]):
                mailbox.select(folders[0])
                probe_uid = mailbox.newest_uid()
                if probe_uid is None:
                    problems.append(f"{folders[0]!r} is empty, so keyword persistence can't be checked")
                else:
                    mailbox.add_keyword(probe_uid, PROBE_KEYWORD)

        if probe_uid is not None:
            with Mailbox(config.mailbox) as mailbox:
                mailbox.select(folders[0])
                try:
                    if PROBE_KEYWORD not in mailbox.keywords(probe_uid):
                        problems.append(
                            f"{PROBE_KEYWORD} did not persist across sessions; {PROCESSED_KEYWORD} would not "
                            "stick and every email would be reclassified"
                        )
                finally:
                    mailbox.remove_keyword(probe_uid, PROBE_KEYWORD)
    except (OSError, imaplib.IMAP4.error) as exc:
        print(f"error: {_mailbox_error_message(exc, config)}", file=sys.stderr)
        return 1

    for problem in problems:
        print(f"FAIL: {problem}", file=sys.stderr)
    if not problems:
        print(f"ok: {PROBE_KEYWORD} persisted across sessions and was removed")
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

    subparsers.add_parser("check", help="verify the connection, folders and keyword persistence")

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
