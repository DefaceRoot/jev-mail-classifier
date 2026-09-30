import email
from unittest.mock import MagicMock

from jev_mail.config import MailboxConfig
from jev_mail.mailbox import PROCESSED_KEYWORD, Mailbox


def _raw_message(subject: str, body: str) -> bytes:
    msg = email.message.EmailMessage()
    msg["Subject"] = subject
    msg.set_content(body)
    return msg.as_bytes()


def test_fetch_unprocessed_parses_subject_and_body():
    fake_server = MagicMock()
    fake_server.search.return_value = [1]
    fake_server.fetch.return_value = {1: {b"RFC822": _raw_message("Hello", "World")}}

    with Mailbox(MailboxConfig(host="imap.example.com"), server=fake_server) as mailbox:
        emails = mailbox.fetch_unprocessed()

    fake_server.search.assert_called_once_with(["UNKEYWORD", PROCESSED_KEYWORD])
    assert len(emails) == 1
    assert emails[0].uid == 1
    assert emails[0].subject == "Hello"
    assert "World" in emails[0].body


def test_fetch_unprocessed_sorts_newest_first_before_limit():
    """IMAP UIDs increase with arrival and SEARCH returns them ascending
    (oldest first) -- a capped run must still pick up the newest mail, not
    get stuck on the oldest end of a big backlog."""
    fake_server = MagicMock()
    fake_server.search.return_value = [1, 2, 3, 4, 5]
    fake_server.fetch.return_value = {}

    with Mailbox(MailboxConfig(host="imap.example.com"), server=fake_server) as mailbox:
        mailbox.fetch_unprocessed(limit=2)

    fake_server.fetch.assert_called_once_with([5, 4], ["RFC822"])


def test_fetch_unprocessed_no_limit_still_sorts_newest_first():
    fake_server = MagicMock()
    fake_server.search.return_value = [1, 3, 2]
    fake_server.fetch.return_value = {}

    with Mailbox(MailboxConfig(host="imap.example.com"), server=fake_server) as mailbox:
        mailbox.fetch_unprocessed()

    fake_server.fetch.assert_called_once_with([3, 2, 1], ["RFC822"])


def test_fetch_unprocessed_returns_empty_when_no_uids():
    fake_server = MagicMock()
    fake_server.search.return_value = []

    with Mailbox(MailboxConfig(host="imap.example.com"), server=fake_server) as mailbox:
        emails = mailbox.fetch_unprocessed()

    assert emails == []
    fake_server.fetch.assert_not_called()


def test_mark_processed_adds_keyword():
    fake_server = MagicMock()
    with Mailbox(MailboxConfig(host="imap.example.com"), server=fake_server) as mailbox:
        mailbox.mark_processed(42)
    fake_server.add_flags.assert_called_once_with([42], [PROCESSED_KEYWORD])


def test_move_creates_folder_if_missing():
    fake_server = MagicMock()
    fake_server.list_folders.return_value = [("\\HasNoChildren", "/", "INBOX")]

    with Mailbox(MailboxConfig(host="imap.example.com"), server=fake_server) as mailbox:
        mailbox.move(1, "Invoices")

    fake_server.create_folder.assert_called_once_with("Invoices")
    fake_server.move.assert_called_once_with([1], "Invoices")


def test_move_skips_create_when_folder_exists():
    fake_server = MagicMock()
    fake_server.list_folders.return_value = [("\\HasNoChildren", "/", "Invoices")]

    with Mailbox(MailboxConfig(host="imap.example.com"), server=fake_server) as mailbox:
        mailbox.move(1, "Invoices")

    fake_server.create_folder.assert_not_called()
    fake_server.move.assert_called_once_with([1], "Invoices")


def test_context_manager_logs_out():
    fake_server = MagicMock()
    with Mailbox(MailboxConfig(host="imap.example.com"), server=fake_server):
        pass
    fake_server.logout.assert_called_once()
