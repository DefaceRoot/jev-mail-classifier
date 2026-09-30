from unittest.mock import MagicMock

import jev_mail.cli as cli
from tests.factories import make_config, probs
from jev_mail.email_state import Email


_config = make_config


def test_build_parser_defaults_and_dry_run_flag():
    parser = cli.build_parser()

    args = parser.parse_args(["run", "--dry-run"])
    assert args.command == "run"
    assert args.dry_run is True

    args = parser.parse_args(["watch"])
    assert args.command == "watch"
    assert args.dry_run is False



def test_process_unprocessed_passes_limit_to_fetch():
    mailbox = MagicMock()
    mailbox.fetch_unprocessed.return_value = []
    client = MagicMock()

    config = _config()
    config.mailbox.batch_size = 7
    cli._process_unprocessed(mailbox, client, config, dry_run=False)

    mailbox.fetch_unprocessed.assert_called_once_with(limit=7)


def test_process_unprocessed_warns_when_limit_is_hit(capsys):
    mailbox = MagicMock()
    mailbox.fetch_unprocessed.return_value = [Email(uid=1, subject="One", headers="Subject: One", body="body")]
    client = MagicMock()
    client.decide.return_value = probs(receipt=0.9)

    config = _config()
    config.mailbox.batch_size = 1
    cli._process_unprocessed(mailbox, client, config, dry_run=False)

    assert "batch_size" in capsys.readouterr().out


def test_process_unprocessed_dry_run_does_not_mutate_mailbox(capsys):
    mailbox = MagicMock()
    mailbox.fetch_unprocessed.return_value = [Email(uid=1, subject="Invoice #1", headers="Subject: Invoice #1", body="Please pay")]
    client = MagicMock()
    client.decide.return_value = probs(receipt=0.9)

    cli._process_unprocessed(mailbox, client, _config(), dry_run=True)

    mailbox.apply.assert_not_called()
    assert "dry-run" in capsys.readouterr().out


def test_process_unprocessed_marks_processed():
    mailbox = MagicMock()
    mailbox.fetch_unprocessed.return_value = [Email(uid=1, subject="Invoice #1", headers="Subject: Invoice #1", body="Please pay")]
    client = MagicMock()
    client.decide.return_value = probs(receipt=0.9)

    cli._process_unprocessed(mailbox, client, _config(), dry_run=False)

    mailbox.apply.assert_called_once()
    assert mailbox.apply.call_args.args[0] == 1


def test_process_unprocessed_below_threshold_no_actions():
    mailbox = MagicMock()
    mailbox.fetch_unprocessed.return_value = [Email(uid=1, subject="Newsletter", headers="Subject: Newsletter", body="...")]
    client = MagicMock()
    client.decide.return_value = probs()

    cli._process_unprocessed(mailbox, client, _config(), dry_run=False)

    mailbox.apply.assert_called_once()
    assert mailbox.apply.call_args.args[0] == 1


def test_cmd_run_reports_config_error(monkeypatch, capsys):
    from jev_mail.config import ConfigError

    def raise_config_error(*a, **k):
        raise ConfigError("no config file")

    monkeypatch.setattr(cli, "load_config", raise_config_error)
    args = cli.build_parser().parse_args(["run"])
    args.dir = "."

    exit_code = cli.cmd_run(args)

    assert exit_code == 1
    assert "no config file" in capsys.readouterr().err


def test_cmd_run_reports_connection_error_not_a_traceback(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: _config())
    monkeypatch.setattr(cli, "get_jev_client", lambda *a, **k: MagicMock())

    class FakeMailbox:
        def __init__(self, mailbox_config):
            pass

        def __enter__(self):
            raise ConnectionRefusedError("[Errno 61] Connection refused")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(cli, "Mailbox", FakeMailbox)
    args = cli.build_parser().parse_args(["run"])
    args.dir = "."

    exit_code = cli.cmd_run(args)

    err = capsys.readouterr().err
    assert exit_code == 1
    assert "Traceback" not in err
    assert "imap.example.com" in err
