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


def test_parser_folder_is_global_repeatable_and_run_limit_is_parsed():
    args = cli.build_parser().parse_args(
        ["--folder", "INBOX", "--folder", "Folders/Bay Bravo", "run", "--dry-run", "--limit", "3"]
    )

    assert (args.folders, args.command, args.dry_run, args.limit) == (["INBOX", "Folders/Bay Bravo"], "run", True, 3)
    assert cli.build_parser().parse_args(["watch"]).folders is None


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

    cli._process_batch(mailbox, client, make_config(), "INBOX", 25, False, set())

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

    cli._process_batch(mailbox, client, make_config(), "INBOX", 25, False, set())

    applied = [(c.args[0], c.args[1].labels, c.args[1].destination) for c in mailbox.apply.call_args_list]
    assert applied == [(2, ("Labels/JEV-REVIEW",), None), (1, ("Labels/JEV-RECEIPT",), None)]


def test_transient_provider_failure_propagates_without_touching_the_mailbox():
    mailbox = MagicMock()
    mailbox.fetch_unprocessed.return_value = [_mail()]
    client = MagicMock()
    client.decide.side_effect = ProviderError("OpenRouter request failed: 503 Service Unavailable")

    with pytest.raises(ProviderError):
        cli._process_batch(mailbox, client, make_config(), "INBOX", 25, False, set())

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

    cli._drain(mailbox, client, make_config(batch_size=2), False, set())

    assert mailbox.fetch_unprocessed.call_count == 3
    assert [c.args[0] for c in mailbox.apply.call_args_list] == [5, 4, 3, 2, 1]


def test_drain_stops_when_the_server_keeps_returning_handled_mail():
    mailbox = MagicMock()
    mailbox.fetch_unprocessed.return_value = [_mail(uid=2), _mail(uid=1)]
    client = MagicMock()
    client.decide.return_value = probs()

    with pytest.raises(cli.MarkerNotSticking, match=r"'INBOX' uids \[1, 2\]"):
        cli._drain(mailbox, client, make_config(batch_size=2), False, set())

    assert client.decide.call_count == 2


def _folder_mailbox(mails_by_folder: dict[str, list[list[Email]]]) -> MagicMock:
    """Mailbox whose fetch_unprocessed serves the selected folder's queued batches."""
    mailbox = MagicMock()
    mailbox.selected = None
    mailbox.select.side_effect = lambda folder: setattr(mailbox, "selected", folder)
    mailbox.fetch_unprocessed.side_effect = lambda limit: (
        mails_by_folder[mailbox.selected].pop(0) if mails_by_folder[mailbox.selected] else []
    )
    mailbox.apply.side_effect = lambda uid, decision: mailbox.applied.append((mailbox.selected, uid))
    mailbox.applied = []
    return mailbox


def test_drain_visits_watch_folders_in_order_and_applies_inside_each():
    mailbox = _folder_mailbox({"INBOX": [[_mail(uid=2), _mail(uid=1)], []], "Folders/Bay Bravo": [[_mail(uid=1)]]})
    client = MagicMock()
    client.decide.return_value = probs()
    config = make_config(batch_size=2)
    config.mailbox.watch_folders = ["INBOX", "Folders/Bay Bravo"]

    cli._drain(mailbox, client, config, False, set())

    assert [c.args[0] for c in mailbox.select.call_args_list] == ["INBOX", "Folders/Bay Bravo"]
    assert mailbox.applied == [("INBOX", 2), ("INBOX", 1), ("Folders/Bay Bravo", 1)]


def test_same_uid_in_two_folders_is_not_a_stuck_marker():
    mailbox = _folder_mailbox({"INBOX": [[_mail(uid=1)]], "Folders/Bay Bravo": [[_mail(uid=1)]]})
    client = MagicMock()
    client.decide.return_value = probs()
    config = make_config()
    config.mailbox.watch_folders = ["INBOX", "Folders/Bay Bravo"]
    done: set = set()

    cli._drain(mailbox, client, config, False, done)

    assert done == {("INBOX", 1), ("Folders/Bay Bravo", 1)}


def test_dry_run_takes_one_batch_per_folder():
    mailbox = _folder_mailbox({"INBOX": [[_mail(uid=2)], [_mail(uid=1)]], "Archive": [[_mail(uid=9)]]})
    client = MagicMock()
    client.decide.return_value = probs()
    config = make_config(batch_size=1)
    config.mailbox.watch_folders = ["INBOX", "Archive"]

    cli._drain(mailbox, client, config, True, set())

    assert mailbox.fetch_unprocessed.call_count == 2
    mailbox.apply.assert_not_called()


def test_run_creates_all_writable_folders_before_any_fetch_and_dry_run_creates_none(monkeypatch):
    order = []
    mailbox = MagicMock()
    mailbox.__enter__.return_value = mailbox
    mailbox.ensure_folders.side_effect = lambda names: order.append(("ensure", list(names)))
    monkeypatch.setattr(cli, "Mailbox", lambda cfg: mailbox)
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: make_config())
    monkeypatch.setattr(cli, "get_jev_client", lambda *a, **k: MagicMock())
    monkeypatch.setattr(cli, "_drain", lambda *a, **k: order.append(("drain",)))

    cli.cmd_run(_args("run"))
    cli.cmd_run(_args("run", "--dry-run"))

    names = order[0][1]
    assert order[0][0] == "ensure" and order[1] == ("drain",) and order[2] == ("drain",) and len(order) == 3
    assert names[0] == "Labels/JEV-SCAM" and "Labels/JEV-REVIEW" in names
    assert names[-2:] == ["Archive", "Folders/JEV Quarantine"]
    assert len(set(names)) == len(names)


def test_watch_idles_on_first_folder_after_draining_all(monkeypatch):
    events = []
    mailbox = MagicMock()
    mailbox.__enter__.return_value = mailbox
    mailbox.select.side_effect = lambda f: events.append(("select", f))
    mailbox.supports_idle.return_value = True
    mailbox.idle.side_effect = lambda: events.append(("idle",))
    mailbox.idle_check.side_effect = [[], KeyboardInterrupt]
    config = make_config(poll_interval_seconds=3000)
    config.mailbox.watch_folders = ["INBOX", "Folders/Bay Bravo"]
    monkeypatch.setattr(cli, "Mailbox", lambda cfg: mailbox)
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: config)
    monkeypatch.setattr(cli, "get_jev_client", lambda *a, **k: MagicMock())
    monkeypatch.setattr(cli, "_drain", lambda *a, **k: events.append(("drain",)))

    with pytest.raises(KeyboardInterrupt):
        cli.cmd_watch(_args("watch"))

    assert events == [("drain",), ("select", "INBOX"), ("idle",), ("drain",), ("select", "INBOX"), ("idle",)]
    assert [c.kwargs["timeout"] for c in mailbox.idle_check.call_args_list] == [600, 600]


def test_dry_run_processes_one_batch_of_the_limit_and_never_writes(capsys):
    mailbox = MagicMock()
    mailbox.fetch_unprocessed.return_value = [_mail(uid=1), _mail(uid=2), _mail(uid=3)]
    client = MagicMock()
    client.decide.return_value = probs(receipt=0.9)

    cli._drain(mailbox, client, make_config(batch_size=25), True, set(), limit=3)

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
    missing: set = set()
    unprocessed = [3, 2]
    newest = 3
    store: set = set()
    selected_folders: list = []

    def __init__(self, config):
        self.config = config

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def select(self, folder):
        _FakeMailbox.selected_folders.append(folder)

    def capabilities(self):
        return ["IDLE", "MOVE"]

    def unprocessed_uids(self):
        return self.unprocessed

    def folder_exists(self, name):
        return name not in self.missing

    def newest_uid(self):
        return self.newest

    def add_keyword(self, uid, kw):
        _FakeMailbox.store.add((self.selected_folders[-1], uid, kw))

    def keywords(self, uid):
        folder = self.selected_folders[-1]
        return {kw for f, u, kw in _FakeMailbox.store if (f, u) == (folder, uid)} if self.keywords_persist else set()

    def remove_keyword(self, uid, kw):
        _FakeMailbox.store.discard((self.selected_folders[-1], uid, kw))

    def ensure_folders(self, names):
        pass


@pytest.fixture
def fake_mailbox(monkeypatch):
    for name, value in dict(keywords_persist=True, missing=set(), newest=3).items():
        monkeypatch.setattr(_FakeMailbox, name, value)
    monkeypatch.setattr(_FakeMailbox, "store", set())
    monkeypatch.setattr(_FakeMailbox, "selected_folders", [])
    monkeypatch.setattr(cli, "Mailbox", _FakeMailbox)
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: make_config(watch_folders=["INBOX", "Folders/Bay Bravo"]))
    return _FakeMailbox


def test_check_passes_reports_each_folder_and_probes_the_first_only(fake_mailbox, capsys):
    assert cli.cmd_check(_args("check")) == 0

    out = capsys.readouterr().out
    assert "capabilities: IDLE MOVE" in out
    assert "'INBOX': 2 unprocessed messages" in out
    assert "'Folders/Bay Bravo': 2 unprocessed messages" in out
    assert fake_mailbox.store == set()
    assert fake_mailbox.selected_folders == ["INBOX", "Folders/Bay Bravo", "INBOX", "INBOX"]


def test_check_fails_when_keywords_do_not_persist_and_still_cleans_up(fake_mailbox, monkeypatch, capsys):
    monkeypatch.setattr(_FakeMailbox, "keywords_persist", False)

    assert cli.cmd_check(_args("check")) == 1

    assert "$JevProbe did not persist" in capsys.readouterr().err
    assert fake_mailbox.store == set()


def test_check_fails_when_a_watch_folder_is_missing(fake_mailbox, monkeypatch, capsys):
    monkeypatch.setattr(_FakeMailbox, "missing", {"Folders/Bay Bravo"})

    assert cli.cmd_check(_args("check")) == 1

    assert "watch folder 'Folders/Bay Bravo' does not exist" in capsys.readouterr().err


def test_check_fails_when_archive_folder_is_missing(fake_mailbox, monkeypatch, capsys):
    monkeypatch.setattr(_FakeMailbox, "missing", {"Archive"})

    assert cli.cmd_check(_args("check")) == 1

    assert "folders.archive 'Archive' does not exist" in capsys.readouterr().err


def test_check_fails_on_an_empty_probe_folder(fake_mailbox, monkeypatch, capsys):
    monkeypatch.setattr(_FakeMailbox, "newest", None)

    assert cli.cmd_check(_args("check")) == 1

    assert "'INBOX' is empty" in capsys.readouterr().err


def test_folder_flags_replace_configured_watch_folders(fake_mailbox, monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "get_jev_client", lambda *a, **k: MagicMock())
    monkeypatch.setattr(cli, "_drain", lambda mailbox, client, config, *a, **k: seen.append(config.mailbox.watch_folders))

    cli.cmd_run(_args("--folder", "Folders/Bay Bravo", "--folder", "Archive", "--folder", "Archive", "run"))
    cli.cmd_run(_args("run"))

    assert seen == [["Folders/Bay Bravo", "Archive"], ["INBOX", "Folders/Bay Bravo"]]


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
