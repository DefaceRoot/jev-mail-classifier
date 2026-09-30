import imaplib
from unittest.mock import MagicMock, call

import pytest

from jev_mail.config import Decision, MailboxConfig
from jev_mail.mailbox import PROCESSED_KEYWORD, Mailbox


def _raw_message(subject: str, body: str) -> bytes:
    return f"From: a@example.com\r\nSubject: {subject}\r\n\r\n{body}\r\n".encode()


def _server(folders=("INBOX",)) -> MagicMock:
    server = MagicMock()
    server.list_folders.return_value = [((), "/", name) for name in folders]
    return server


def _mailbox(server, **overrides) -> Mailbox:
    return Mailbox(MailboxConfig(host="imap.example.com", **overrides), server=server)


def test_fetch_uses_peek_with_partial_cap_and_reads_the_partial_body_key():
    server = MagicMock()
    server.search.return_value = [1, 2, 3]
    server.fetch.return_value = {
        3: {b"SEQ": 3, b"BODY[]<0>": _raw_message("Newest", "world")},
        2: {b"SEQ": 2, b"BODY[]<0>": _raw_message("Older", "hello")},
    }

    with _mailbox(server, max_fetch_bytes=1000) as mailbox:
        emails = mailbox.fetch_unprocessed(limit=2)

    server.search.assert_called_once_with(["UNKEYWORD", PROCESSED_KEYWORD])
    server.fetch.assert_called_once_with([3, 2], ["BODY.PEEK[]<0.1000>"])
    assert [(e.uid, e.subject, e.body) for e in emails] == [(3, "Newest", "world"), (2, "Older", "hello")]


def test_fetch_sorts_newest_first_before_limit():
    server = MagicMock()
    server.search.return_value = [1, 5, 3, 2, 4]
    server.fetch.return_value = {}

    with _mailbox(server) as mailbox:
        mailbox.fetch_unprocessed(limit=2)

    assert server.fetch.call_args.args[0] == [5, 4]


def test_fetch_returns_empty_without_fetching_when_nothing_unprocessed():
    server = MagicMock()
    server.search.return_value = []

    with _mailbox(server) as mailbox:
        assert mailbox.fetch_unprocessed() == []

    server.fetch.assert_not_called()


def test_apply_copies_labels_then_marks_processed_then_moves_and_never_creates():
    server = _server()
    decision = Decision(labels=("Labels/JEV-SCAM", "Labels/JEV-SECURITY"), destination="Folders/JEV Quarantine")

    with _mailbox(server) as mailbox:
        mailbox.apply(42, decision)

    assert server.mock_calls == [
        call.copy([42], "Labels/JEV-SCAM"),
        call.copy([42], "Labels/JEV-SECURITY"),
        call.add_flags([42], [PROCESSED_KEYWORD]),
        call.move([42], "Folders/JEV Quarantine"),
        call.logout(),
    ]


def test_apply_without_destination_never_moves():
    server = _server()

    with _mailbox(server) as mailbox:
        mailbox.apply(7, Decision(labels=("Labels/JEV-REVIEW",), destination=None))

    server.move.assert_not_called()
    assert server.add_flags.call_args == call([7], [PROCESSED_KEYWORD])


def test_apply_lets_a_missing_target_folder_error_propagate():
    server = _server()
    server.copy.side_effect = imaplib.IMAP4.error("COPY failed: no such mailbox")

    with _mailbox(server) as mailbox, pytest.raises(imaplib.IMAP4.error):
        mailbox.apply(7, Decision(labels=("Labels/JEV-REVIEW",), destination="Archive"))

    server.add_flags.assert_not_called()
    server.move.assert_not_called()


def test_select_switches_the_folder_fetch_and_apply_operate_on():
    server = MagicMock()

    with _mailbox(server) as mailbox:
        mailbox.select("Folders/Bay Bravo")

    server.select_folder.assert_called_once_with("Folders/Bay Bravo")


def test_ensure_folders_creates_only_missing_names_in_one_pass_in_order():
    server = _server(folders=("INBOX", "Labels/JEV-A"))

    with _mailbox(server) as mailbox:
        mailbox.ensure_folders(["Labels/JEV-A", "Labels/JEV-B", "Archive", "Labels/JEV-B"])

    assert server.list_folders.call_count == 1
    assert server.mock_calls[:3] == [call.list_folders(), call.create_folder("Labels/JEV-B"), call.create_folder("Archive")]
    assert server.create_folder.call_count == 2


def test_ensure_folders_tolerates_another_process_creating_the_folder_first():
    server = _server()
    server.create_folder.side_effect = imaplib.IMAP4.error("CREATE failed: Mailbox already exists")
    server.list_folders.side_effect = [
        [((), "/", "INBOX")],
        [((), "/", "INBOX"), ((), "/", "Labels/JEV-SCAM")],
    ]

    with _mailbox(server) as mailbox:
        mailbox.ensure_folders(["Labels/JEV-SCAM"])
        assert mailbox.folder_exists("Labels/JEV-SCAM")

    assert server.list_folders.call_count == 2


def test_ensure_folders_failure_that_is_not_a_race_propagates():
    server = _server()
    server.create_folder.side_effect = imaplib.IMAP4.error("a label cannot have children")

    with _mailbox(server) as mailbox, pytest.raises(imaplib.IMAP4.error):
        mailbox.ensure_folders(["Labels/JEV/X"])


def test_context_manager_logs_out():
    server = MagicMock()
    with _mailbox(server):
        pass
    server.logout.assert_called_once()


def test_starttls_connects_in_plaintext_then_upgrades_with_unverified_context(monkeypatch):
    import ssl

    client_cls = MagicMock()
    monkeypatch.setattr("jev_mail.mailbox.IMAPClient", client_cls)
    config = MailboxConfig(
        host="bridge", port=143, security="starttls", tls_verify=False, username="u", password="p"
    )

    with Mailbox(config):
        pass

    client_cls.assert_called_once_with("bridge", port=143, ssl=False, use_uid=True)
    server = client_cls.return_value
    context = server.starttls.call_args.args[0]
    assert context.check_hostname is False
    assert context.verify_mode == ssl.CERT_NONE
    server.login.assert_called_once_with("u", "p")


def test_ssl_connects_with_verifying_context_by_default(monkeypatch):
    import ssl

    client_cls = MagicMock()
    monkeypatch.setattr("jev_mail.mailbox.IMAPClient", client_cls)

    with Mailbox(MailboxConfig(host="imap.gmail.com", security="ssl")):
        pass

    kwargs = client_cls.call_args.kwargs
    assert kwargs["ssl"] is True
    assert kwargs["ssl_context"].verify_mode == ssl.CERT_REQUIRED
    client_cls.return_value.starttls.assert_not_called()
