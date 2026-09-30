from unittest.mock import MagicMock

import pytest

import jev_mail.cli as cli
from jev_mail.config import Decision
from jev_mail.email_state import Email
from jev_mail.providers import EmailRejected, InputTooLong, ProviderError
from tests.factories import make_config, probs


def _mail(uid=1, subject="Invoice #1", body="Please pay") -> Email:
    return Email(uid=uid, subject=subject, headers=f"Subject: {subject}", body=body)


def _args(*argv: str):
    return cli.build_parser().parse_args(["--dir", ".", *argv])


def test_parser_folder_is_global_and_run_limit_is_parsed():
    args = cli.build_parser().parse_args(["--folder", "Folders/Bay Bravo", "run", "--dry-run", "--limit", "3"])

    assert (args.folder, args.command, args.dry_run, args.limit) == ("Folders/Bay Bravo", "run", True, 3)
    assert cli.build_parser().parse_args(["watch"]).folder is None


def test_limit_without_dry_run_is_rejected(monkeypatch):
    monkeypatch.setattr(cli.sys, "argv", ["jev-mail", "run", "--limit", "3"])
    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 2


def test_batch_applies_decision_and_logs_one_line_without_body(capsys):
    mailbox = MagicMock()
    mailbox.fetch_unprocessed.return_value = [_mail(uid=9, subject="S" * 100, body="TOP SECRET BODY")]
    client = MagicMock()
    client.decide.return_value = probs(scam=0.91, security=0.4, receipt=0.75, action=0.1)

    cli._process_batch(mailbox, client, make_config(), 25, False, set())

    decision = mailbox.apply.call_args.args[1]
    assert mailbox.apply.call_args.args[0] == 9
    assert decision.labels == ("Labels/JEV-SCAM", "Labels/JEV-RECEIPT")
    assert decision.destination == "Folders/JEV Quarantine"
    out = capsys.readouterr().out
    assert out == (
        f"uid=9 subject={'S' * 80!r} labels=Labels/JEV-SCAM,Labels/JEV-RECEIPT "
        "dest=Folders/JEV Quarantine top=scam:0.91 receipt:0.75 security:0.40\n"
    )


def test_overflow_retries_with_halved_body_and_keeps_headers():
    bodies = []

    def decide(state, categories):
        bodies.append(state.split("\n\n", 1)[1])
        if len(bodies[-1]) > 2500:
            raise InputTooLong("too long")
        return probs(scam=0.9)

    client = MagicMock()
    client.decide.side_effect = decide
    mail = _mail(body="x" * 10000)

    decision = cli._decide_for(client, make_config(), mail)

    assert [len(b) for b in bodies] == [10000, 5000, 2500]
    assert all(call.args[0].startswith("Subject: Invoice #1\n\n") for call in client.decide.call_args_list)
    assert decision.labels == ("Labels/JEV-SCAM",)


def test_body_still_too_long_at_the_floor_gets_review_label(capsys):
    sizes = []

    def decide(state, categories):
        sizes.append(len(state.split("\n\n", 1)[1]))
        raise InputTooLong("too long")

    client = MagicMock()
    client.decide.side_effect = decide

    decision = cli._decide_for(client, make_config(), _mail(body="x" * 5000))

    assert sizes == [5000, 2500, 2000]
    assert decision == Decision(labels=("Labels/JEV-REVIEW",), destination=None)
    assert "uid=1" in capsys.readouterr().err


def test_poison_email_is_labelled_review_and_marked_processed_and_batch_continues():
    mailbox = MagicMock()
    mailbox.fetch_unprocessed.return_value = [_mail(uid=2, subject="poison"), _mail(uid=1, subject="fine")]
    client = MagicMock()
    client.decide.side_effect = [EmailRejected("422 Unprocessable"), probs(receipt=0.9)]

    cli._process_batch(mailbox, client, make_config(), 25, False, set())

    applied = [(c.args[0], c.args[1].labels, c.args[1].destination) for c in mailbox.apply.call_args_list]
    assert applied == [(2, ("Labels/JEV-REVIEW",), None), (1, ("Labels/JEV-RECEIPT",), None)]


def test_transient_provider_failure_propagates_without_touching_the_mailbox():
    mailbox = MagicMock()
    mailbox.fetch_unprocessed.return_value = [_mail()]
    client = MagicMock()
    client.decide.side_effect = ProviderError("OpenRouter request failed: 503 Service Unavailable")

    with pytest.raises(ProviderError):
        cli._process_batch(mailbox, client, make_config(), 25, False, set())

    mailbox.apply.assert_not_called()


def test_drain_loops_until_a_short_batch():
    mailbox = MagicMock()
    mailbox.fetch_unprocessed.side_effect = [
        [_mail(uid=5), _mail(uid=4)],
        [_mail(uid=3), _mail(uid=2)],
        [_mail(uid=1)],
    ]
    client = MagicMock()
    client.decide.return_value = probs()

    cli._drain(mailbox, client, make_config(batch_size=2), dry_run=False)

    assert mailbox.fetch_unprocessed.call_count == 3
    assert [c.args[0] for c in mailbox.apply.call_args_list] == [5, 4, 3, 2, 1]


def test_drain_stops_when_the_server_keeps_returning_handled_mail():
    mailbox = MagicMock()
    mailbox.fetch_unprocessed.return_value = [_mail(uid=2), _mail(uid=1)]
    client = MagicMock()
    client.decide.return_value = probs()

    with pytest.raises(cli.MarkerNotSticking, match=r"\[1, 2\]"):
        cli._drain(mailbox, client, make_config(batch_size=2), dry_run=False)

    assert client.decide.call_count == 2


def test_dry_run_processes_one_batch_of_the_limit_and_never_writes(capsys):
    mailbox = MagicMock()
    mailbox.fetch_unprocessed.return_value = [_mail(uid=1), _mail(uid=2), _mail(uid=3)]
    client = MagicMock()
    client.decide.return_value = probs(receipt=0.9)

    cli._drain(mailbox, client, make_config(batch_size=25), dry_run=True, limit=3)

    mailbox.fetch_unprocessed.assert_called_once_with(limit=3)
    mailbox.apply.assert_not_called()
    assert capsys.readouterr().out.count("[dry-run] uid=") == 3


def test_cmd_run_reports_config_error(monkeypatch, capsys):
    from jev_mail.config import ConfigError

    def raise_config_error(*a, **k):
        raise ConfigError("no config file")

    monkeypatch.setattr(cli, "load_config", raise_config_error)

    assert cli.cmd_run(_args("run")) == 1
    assert "no config file" in capsys.readouterr().err


class _FakeMailbox:
    """Stands in for Mailbox; class-level state survives reconnects."""

    keywords_persist = True
    archive_exists = True
    unprocessed = [3, 2]
    newest = 3
    store: set = set()
    opened_folders: list = []

    def __init__(self, config):
        self.config = config
        _FakeMailbox.opened_folders.append(config.folder)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def capabilities(self):
        return ["IDLE", "MOVE"]

    def unprocessed_uids(self):
        return self.unprocessed

    def folder_exists(self, name):
        return self.archive_exists

    def newest_uid(self):
        return self.newest

    def add_keyword(self, uid, kw):
        _FakeMailbox.store.add((uid, kw))

    def keywords(self, uid):
        return {kw for u, kw in _FakeMailbox.store if u == uid} if self.keywords_persist else set()

    def remove_keyword(self, uid, kw):
        _FakeMailbox.store.discard((uid, kw))


@pytest.fixture
def fake_mailbox(monkeypatch):
    for name, value in dict(keywords_persist=True, archive_exists=True, newest=3).items():
        monkeypatch.setattr(_FakeMailbox, name, value)
    monkeypatch.setattr(_FakeMailbox, "store", set())
    monkeypatch.setattr(_FakeMailbox, "opened_folders", [])
    monkeypatch.setattr(cli, "Mailbox", _FakeMailbox)
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: make_config())
    return _FakeMailbox


def test_check_passes_and_removes_its_probe(fake_mailbox, capsys):
    assert cli.cmd_check(_args("check")) == 0

    out = capsys.readouterr().out
    assert "capabilities: IDLE MOVE" in out
    assert "unprocessed messages: 2" in out
    assert fake_mailbox.store == set()


def test_check_fails_when_keywords_do_not_persist_and_still_cleans_up(fake_mailbox, monkeypatch, capsys):
    monkeypatch.setattr(_FakeMailbox, "keywords_persist", False)

    assert cli.cmd_check(_args("check")) == 1

    assert "$JevProbe did not persist" in capsys.readouterr().err
    assert fake_mailbox.store == set()


def test_check_fails_when_archive_folder_is_missing(fake_mailbox, monkeypatch, capsys):
    monkeypatch.setattr(_FakeMailbox, "archive_exists", False)

    assert cli.cmd_check(_args("check")) == 1

    assert "folders.archive 'Archive' does not exist" in capsys.readouterr().err


def test_check_fails_on_an_empty_folder(fake_mailbox, monkeypatch, capsys):
    monkeypatch.setattr(_FakeMailbox, "newest", None)

    assert cli.cmd_check(_args("check")) == 1

    assert "is empty" in capsys.readouterr().err


def test_folder_flag_overrides_configured_folder_for_run_and_check(fake_mailbox, monkeypatch):
    monkeypatch.setattr(cli, "get_jev_client", lambda *a, **k: MagicMock())
    monkeypatch.setattr(cli, "_drain", lambda *a, **k: None)

    cli.cmd_run(_args("--folder", "Folders/Bay Bravo", "run"))
    cli.cmd_check(_args("--folder", "Folders/Bay Bravo", "check"))
    cli.cmd_check(_args("check"))

    assert fake_mailbox.opened_folders == ["Folders/Bay Bravo"] * 3 + ["INBOX"] * 2


def test_cmd_run_reports_connection_error_not_a_traceback(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: make_config())
    monkeypatch.setattr(cli, "get_jev_client", lambda *a, **k: MagicMock())

    class RefusingMailbox:
        def __init__(self, mailbox_config):
            pass

        def __enter__(self):
            raise ConnectionRefusedError("[Errno 111] Connection refused")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(cli, "Mailbox", RefusingMailbox)

    assert cli.cmd_run(_args("run")) == 1

    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "imap.example.com" in err


def test_cmd_run_exits_nonzero_on_transient_provider_failure(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: make_config())
    monkeypatch.setattr(cli, "get_jev_client", lambda *a, **k: MagicMock())
    monkeypatch.setattr(cli, "Mailbox", _FakeMailbox)

    def boom(*a, **k):
        raise ProviderError("OpenRouter request failed: 429 Too Many Requests")

    monkeypatch.setattr(cli, "_drain", boom)

    assert cli.cmd_run(_args("run")) == 1
    assert "429" in capsys.readouterr().err
