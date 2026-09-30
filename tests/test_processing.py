"""End to end: real Mailbox, real ProcessedStore, fake IMAP server, stub Jev."""
from unittest.mock import MagicMock

import pytest

import jev_mail.cli as cli
from jev_mail.mailbox import Mailbox
from jev_mail.store import ProcessedStore
from tests.factories import make_config, probs
from tests.fake_imap import FakeImapServer, raw_message

BRAVO = "Folders/Bay Bravo"


@pytest.fixture
def store(tmp_path):
    with ProcessedStore(tmp_path / "s.sqlite") as store:
        yield store


def _client(**probabilities: float) -> MagicMock:
    client = MagicMock()
    client.decide.return_value = probs(**probabilities)
    return client


def _drain(server, store, client, *, dry_run=False, folders=("INBOX",)):
    config = make_config(watch_folders=list(folders), batch_size=10)
    with Mailbox(config.mailbox, store, server=server) as mailbox:
        cli._drain(mailbox, store, client, config, dry_run)


def test_a_handled_message_dragged_into_another_watch_folder_is_not_sorted_again(store):
    server = FakeImapServer()
    server.add_folder(BRAVO)
    raw = raw_message("Quote request", "<quote-1@customer.test>")
    server.add_message("INBOX", raw)
    client = _client(receipt=0.9)

    _drain(server, store, client, folders=["INBOX", BRAVO])
    assert server.writes == [("copy", "INBOX", 1, "Labels/JEV-RECEIPT")]

    server.add_message(BRAVO, raw)
    server.add_message(BRAVO, raw_message("Different mail", "<other@customer.test>"))
    _drain(server, store, client, folders=["INBOX", BRAVO])

    assert server.writes == [
        ("copy", "INBOX", 1, "Labels/JEV-RECEIPT"),
        ("copy", BRAVO, 2, "Labels/JEV-RECEIPT"),
    ]
    assert client.decide.call_count == 2


def test_a_uidvalidity_change_remaps_uids_without_reprocessing(store):
    server = FakeImapServer()
    server.add_message("INBOX", raw_message("One", "<1@x.test>"))
    server.add_message("INBOX", raw_message("Two", "<2@x.test>"))
    client = _client(receipt=0.9)
    _drain(server, store, client)
    assert client.decide.call_count == 2

    server.renumber("INBOX", validity=2, start_uid=100)
    server.add_message("INBOX", raw_message("Three", "<3@x.test>"))
    _drain(server, store, client)

    assert client.decide.call_count == 3
    assert [w[1:3] for w in server.writes] == [("INBOX", 2), ("INBOX", 1), ("INBOX", 102)]
    assert store.keys("INBOX", 2, [100, 101, 102]) == {100: "1@x.test", 101: "2@x.test", 102: "3@x.test"}


def test_a_crash_between_apply_and_record_repeats_the_same_copies_once(store, monkeypatch):
    server = FakeImapServer()
    server.add_message("INBOX", raw_message("Receipt", "<r1@x.test>"))
    client = _client(receipt=0.9)
    real_record = store.record
    monkeypatch.setattr(store, "record", MagicMock(side_effect=RuntimeError("killed")))

    with pytest.raises(RuntimeError):
        _drain(server, store, client)
    monkeypatch.setattr(store, "record", real_record)
    _drain(server, store, client)
    _drain(server, store, client)

    assert server.writes == [("copy", "INBOX", 1, "Labels/JEV-RECEIPT")] * 2
    assert store.is_processed("r1@x.test")


def test_a_moved_message_is_recorded_so_its_copy_elsewhere_is_left_alone(store):
    server = FakeImapServer()
    server.add_message("INBOX", raw_message("Sale", "<s1@x.test>"))
    _drain(server, store, _client(cold_outreach=0.95))

    assert server.writes == [("copy", "INBOX", 1, "Labels/JEV-COLD"), ("move", "INBOX", 1, "Archive")]
    assert server.messages["INBOX"] == {}
    assert store.is_processed("s1@x.test")


def test_dry_run_records_nothing_and_writes_nothing_to_the_mailbox(store):
    server = FakeImapServer()
    server.add_message("INBOX", raw_message("Receipt", "<r1@x.test>"))

    _drain(server, store, _client(receipt=0.9), dry_run=True)

    assert store.processed_count() == 0
    assert server.writes == []
    assert store.keys("INBOX", 1, [1]) == {1: "r1@x.test"}


def test_a_message_without_message_id_is_recognised_by_its_hash_key(store):
    server = FakeImapServer()
    server.add_message("INBOX", raw_message("No id"))
    client = _client(receipt=0.9)

    _drain(server, store, client)
    _drain(server, store, client)

    assert client.decide.call_count == 1
    assert store.keys("INBOX", 1, [1])[1].startswith("sha256:")
