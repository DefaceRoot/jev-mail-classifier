from __future__ import annotations

import email
import imaplib


def raw_message(subject: str, message_id: str | None = None, body: str = "hello", sender: str = "a@example.com") -> bytes:
    lines = [f"From: {sender}", "Date: Tue, 29 Sep 2026 10:00:00 +0000", f"Subject: {subject}"]
    if message_id:
        lines.insert(0, f"Message-ID: {message_id}")
    return ("\r\n".join(lines) + f"\r\n\r\n{body}\r\n").encode()


class FakeImapServer:
    """Just enough IMAPClient for Mailbox: folders with UIDs and UIDVALIDITY,
    SEARCH ALL, the two FETCH shapes Mailbox uses, COPY and MOVE. Every COPY and
    MOVE is appended to `writes` as (op, source_folder, source_uid, target)."""

    def __init__(self) -> None:
        self.messages: dict[str, dict[int, bytes]] = {}
        self.validity: dict[str, int] = {}
        self._next_uid: dict[str, int] = {}
        self.selected = ""
        self.writes: list[tuple] = []
        self.fetches: list[tuple] = []
        self.add_folder("INBOX")

    def add_folder(self, name: str, validity: int = 1) -> None:
        self.messages.setdefault(name, {})
        self.validity.setdefault(name, validity)
        self._next_uid.setdefault(name, 1)

    def add_message(self, folder: str, raw: bytes) -> int:
        self.add_folder(folder)
        uid = self._next_uid[folder]
        self._next_uid[folder] += 1
        self.messages[folder][uid] = raw
        return uid

    def renumber(self, folder: str, validity: int, start_uid: int) -> None:
        old = self.messages[folder]
        self.messages[folder] = {start_uid + i: raw for i, raw in enumerate(old.values())}
        self._next_uid[folder] = start_uid + len(old)
        self.validity[folder] = validity

    def list_folders(self):
        return [((), "/", name) for name in self.messages]

    def create_folder(self, name: str) -> None:
        if name in self.messages:
            raise imaplib.IMAP4.error("already exists")
        self.add_folder(name)

    def select_folder(self, name: str) -> dict:
        self.selected = name
        return {b"UIDVALIDITY": self.validity[name]}

    def search(self, criteria: list) -> list[int]:
        assert criteria == ["ALL"]
        return sorted(self.messages[self.selected])

    def fetch(self, uids: list[int], items: list[str]) -> dict:
        (item,) = items
        self.fetches.append((self.selected, list(uids), item))
        out = {}
        for uid in uids:
            raw = self.messages[self.selected].get(uid)
            if raw is None:
                continue
            if item.startswith("BODY.PEEK[HEADER.FIELDS"):
                msg = email.message_from_bytes(raw)
                block = "".join(f"{name}: {msg[name]}\r\n" for name in ("Message-ID", "Date", "From", "Subject") if msg[name])
                out[uid] = {b"SEQ": uid, b"BODY[HEADER.FIELDS (MESSAGE-ID DATE FROM SUBJECT)]": (block + "\r\n").encode()}
            else:
                cap = int(item.rsplit(".", 1)[1].rstrip(">"))
                out[uid] = {b"SEQ": uid, b"BODY[]<0>": raw[:cap]}
        return out

    def copy(self, uids: list[int], folder: str) -> None:
        for uid in uids:
            self.writes.append(("copy", self.selected, uid, folder))
            self.add_message(folder, self.messages[self.selected][uid])

    def move(self, uids: list[int], folder: str) -> None:
        for uid in uids:
            self.writes.append(("move", self.selected, uid, folder))
            self.add_message(folder, self.messages[self.selected].pop(uid))

    def logout(self) -> None:
        pass
