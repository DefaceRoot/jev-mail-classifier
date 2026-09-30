import imaplib
from unittest.mock import MagicMock, call

import pytest

from jev_mail.config import Decision, MailboxConfig
from jev_mail.mailbox import Mailbox
from jev_mail.store import ProcessedStore
from tests.fake_imap import FakeImapServer, raw_message


def _server(folders=("INBOX",)) -> MagicMock:
    server = MagicMock()
    server.list_folders.return_value = [((), "/", name) for name in folders]
    return server


def _mailbox(server, store=None, **overrides) -> Mailbox:
    return Mailbox(MailboxConfig(host="imap.example.com", **overrides), store, server=server)


@pytest.fixture
def store(tmp_path):
    with ProcessedStore(tmp_path / "s.sqlite") as store:
        yield store


def test_fetch_uses_peek_with_partial_cap_newest_first_and_carries_message_keys(store):
    server = FakeImapServer()
    server.add_message("INBOX", raw_message("Oldest", "<1@x.test>"))
    server.add_message("INBOX", raw_message("Older", "<2@x.test>", body="hello " * 10))
    server.add_message("INBOX", raw_message("Newest", "<3@x.test>"))

    with _mailbox(server, store, max_fetch_bytes=1000) as mailbox:
        mailbox.select("INBOX")
        emails = mailbox.fetch_unprocessed(limit=2)

    assert [(e.uid, e.subject, e.message_key) for e in emails] == [(3, "Newest", "3@x.test"), (2, "Older", "2@x.test")]
    assert server.fetches[-1] == ("INBOX", [3, 2], "BODY.PEEK[]<0.1000>")


def test_fetch_skips_recorded_messages_and_fetches_no_bodies_when_nothing_is_left(store):
    server = FakeImapServer()
    server.add_message("INBOX", raw_message("Done", "<1@x.test>"))

    with _mailbox(server, store) as mailbox:
        mailbox.select("INBOX")
        (mail,) = mailbox.fetch_unprocessed()
        store.record(mail.message_key, Decision(labels=(), destination=None))
        server.fetches.clear()

        assert mailbox.fetch_unprocessed() == []

    assert server.fetches == []


def test_identity_headers_are_fetched_in_chunks_of_500_and_only_for_new_uids(store):
    server = FakeImapServer()
    for n in range(1200):
        server.add_message("INBOX", raw_message(f"m{n}", f"<{n}@x.test>"))

    with _mailbox(server, store) as mailbox:
        mailbox.select("INBOX")
        assert mailbox.counts() == (1200, 1200)
        first = [len(uids) for _, uids, item in server.fetches if item.startswith("BODY.PEEK[HEADER")]
        server.add_message("INBOX", raw_message("new", "<new@x.test>"))
        server.fetches.clear()
        assert mailbox.counts() == (1201, 1201)

    assert first == [500, 500, 200]
    assert [(uids, item[:23]) for _, uids, item in server.fetches] == [([1201], "BODY.PEEK[HEADER.FIELDS")]


def test_apply_copies_each_label_then_moves_and_never_creates_or_flags():
    server = _server()
    decision = Decision(labels=("Labels/JEV-SCAM", "Labels/JEV-SECURITY"), destination="Folders/JEV Quarantine")

    with _mailbox(server) as mailbox:
        mailbox.apply(42, decision)

    assert server.mock_calls == [
        call.copy([42], "Labels/JEV-SCAM"),
        call.copy([42], "Labels/JEV-SECURITY"),
        call.move([42], "Folders/JEV Quarantine"),
        call.logout(),
    ]


def test_apply_without_destination_never_moves():
    server = _server()

    with _mailbox(server) as mailbox:
        mailbox.apply(7, Decision(labels=("Labels/JEV-REVIEW",), destination=None))

    server.move.assert_not_called()
    server.copy.assert_called_once_with([7], "Labels/JEV-REVIEW")


def test_apply_lets_a_missing_target_folder_error_propagate():
    server = _server()
    server.copy.side_effect = imaplib.IMAP4.error("COPY failed: no such mailbox")

    with _mailbox(server) as mailbox, pytest.raises(imaplib.IMAP4.error):
        mailbox.apply(7, Decision(labels=("Labels/JEV-REVIEW",), destination="Archive"))

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
