import pytest

from jev_mail.config import Decision
from jev_mail.email_state import message_key
from jev_mail.store import ProcessedStore


@pytest.mark.parametrize(
    "header, expected",
    [
        (b"Message-ID: <Abc.123@Mail.Example.COM>\r\n", "abc.123@mail.example.com"),
        (b"Message-ID:   <abc@x.test>  \r\n", "abc@x.test"),
        (b"Message-ID:\r\n <folded-id@x.test>\r\n", "folded-id@x.test"),
        (b"message-id: bare-id@x.test\r\n", "bare-id@x.test"),
    ],
)
def test_message_id_is_normalised(header, expected):
    assert message_key(header + b"Subject: s\r\n\r\n") == expected


def test_without_a_message_id_the_key_is_a_hash_of_date_from_subject():
    header = b"From: a@example.com\r\nDate: Tue, 29 Sep 2026 10:00:00 +0000\r\nSubject: Hello\r\n\r\n"
    reordered = b"Subject:  Hello\r\nDate: Tue, 29 Sep 2026 10:00:00 +0000\r\nFrom: a@example.com\r\nX-Other: 1\r\n\r\n"
    blank_id = b"Message-ID: <>\r\n" + header

    key = message_key(header)

    assert key == "sha256:77c25e8689d9ada80b0584631c69d19fa17d534befb1f3fc7c51228463768db5"
    assert message_key(reordered) == key
    assert message_key(blank_id) == key
    assert message_key(header.replace(b"Hello", b"Other")) != key


def test_record_survives_reopening_the_file(tmp_path):
    path = tmp_path / "s.sqlite"
    with ProcessedStore(path) as store:
        store.map_uids("INBOX", 1, {5: "a@x", 6: "b@x"})
        store.record("a@x", Decision(labels=("Labels/JEV-SCAM",), destination="Archive"))

    with ProcessedStore(path) as store:
        assert store.is_processed("a@x")
        assert not store.is_processed("b@x")
        assert store.unprocessed_uids("INBOX", 1, [5, 6]) == [6]


def test_unprocessed_uids_are_newest_first_and_limited_to_mapped_requested_uids(tmp_path):
    with ProcessedStore(tmp_path / "s.sqlite") as store:
        store.map_uids("INBOX", 1, {1: "a", 2: "b", 3: "c", 4: "d"})
        store.record("c", Decision(labels=(), destination=None))

        assert store.unprocessed_uids("INBOX", 1, [1, 2, 3, 4, 99]) == [4, 2, 1]
        assert store.unprocessed_uids("INBOX", 1, [1, 2]) == [2, 1]
        assert store.unprocessed_uids("INBOX", 2, [1, 2, 3, 4]) == []


def test_keys_lookup_spans_more_uids_than_one_sql_chunk(tmp_path):
    with ProcessedStore(tmp_path / "s.sqlite") as store:
        mapping = {uid: f"k{uid}" for uid in range(1, 1201)}
        store.map_uids("INBOX", 1, mapping)

        assert store.keys("INBOX", 1, range(1, 1201)) == mapping
