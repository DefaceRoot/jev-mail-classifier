from __future__ import annotations

import imaplib
import ssl
from dataclasses import replace

from imapclient import IMAPClient

from jev_mail.config import Decision, MailboxConfig
from jev_mail.email_state import Email, message_key, parse_email
from jev_mail.store import ProcessedStore

HEADER_CHUNK = 500
IDENTITY_FETCH = "BODY.PEEK[HEADER.FIELDS (MESSAGE-ID DATE FROM SUBJECT)]"


def _ssl_context(config: MailboxConfig) -> ssl.SSLContext:
    context = ssl.create_default_context()
    if not config.tls_verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def _connect(config: MailboxConfig) -> IMAPClient:
    context = _ssl_context(config)
    if config.security == "ssl":
        server = IMAPClient(config.host, port=config.port, ssl=True, ssl_context=context, use_uid=True)
    else:
        server = IMAPClient(config.host, port=config.port, ssl=False, use_uid=True)
        server.starttls(context)
    server.login(config.username, config.password)
    return server


class Mailbox:
    """Thin wrapper around imapclient.IMAPClient: fetch mail the store hasn't
    recorded and apply a Decision to it. Use as a context manager so the
    connection always gets closed. `store` may be None only for read-only
    folder checks."""

    def __init__(
        self, config: MailboxConfig, store: ProcessedStore | None = None, server: IMAPClient | None = None
    ):
        self._config = config
        self._store = store
        self._folder = ""
        self._uidvalidity = 0
        # `server` is an injection point for tests; production code always
        # leaves it unset and lets __enter__ create the real connection.
        self._server = server
        self._folder_names: set[str] | None = None

    def __enter__(self) -> "Mailbox":
        if self._server is None:
            self._server = _connect(self._config)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._server is not None:
            try:
                self._server.logout()
            except Exception:
                pass

    def select(self, folder: str) -> None:
        info = self._server.select_folder(folder)
        self._folder = folder
        self._uidvalidity = int(info[b"UIDVALIDITY"])

    def capabilities(self) -> list[str]:
        return sorted(c.decode() if isinstance(c, bytes) else c for c in self._server.capabilities())

    def _sync_identities(self) -> list[int]:
        """Map every UID in the selected folder to its message key, fetching
        headers only for UIDs not seen under this UIDVALIDITY. Returns all UIDs."""
        uids = self._server.search(["ALL"])
        known = self._store.mapped_uids(self._folder, self._uidvalidity)
        missing = sorted(set(uids) - known, reverse=True)
        for start in range(0, len(missing), HEADER_CHUNK):
            response = self._server.fetch(missing[start : start + HEADER_CHUNK], [IDENTITY_FETCH])
            keys = {
                uid: message_key(next(v for k, v in data.items() if k.startswith(b"BODY[HEADER.FIELDS")))
                for uid, data in response.items()
            }
            self._store.map_uids(self._folder, self._uidvalidity, keys)
        return uids

    def counts(self) -> tuple[int, int]:
        """(messages in the selected folder, messages the store hasn't recorded)."""
        uids = self._sync_identities()
        return len(uids), len(self._store.unprocessed_uids(self._folder, self._uidvalidity, uids))

    def fetch_unprocessed(self, limit: int | None = None) -> list[Email]:
        """Newest unrecorded mail first (UIDs grow with arrival). Uses
        BODY.PEEK so fetching never sets \\Seen, and a partial fetch so one
        huge message can't blow the bandwidth budget."""
        uids = self._store.unprocessed_uids(self._folder, self._uidvalidity, self._sync_identities())
        if limit is not None:
            uids = uids[:limit]
        if not uids:
            return []
        keys = self._store.keys(self._folder, self._uidvalidity, uids)
        response = self._server.fetch(uids, [f"BODY.PEEK[]<0.{self._config.max_fetch_bytes}>"])
        emails = []
        for uid in sorted(response, reverse=True):
            # imapclient keys a partial fetch as BODY[]<origin>, not BODY.PEEK[]
            raw = next(v for k, v in response[uid].items() if k.startswith(b"BODY[]"))
            emails.append(replace(parse_email(uid, raw), message_key=keys[uid]))
        return emails

    def apply(self, uid: int, decision: Decision) -> None:
        """The caller records the message in the store afterwards. A crash
        before that only repeats idempotent COPYs on a message still in the
        folder, and a moved message has already left the watched folder."""
        for label in decision.labels:
            self._server.copy([uid], label)
        if decision.destination:
            self._server.move([uid], decision.destination)

    def ensure_folders(self, names: list[str]) -> None:
        """Create the missing folders, one at a time, before any COPY or MOVE.
        Proton Bridge deadlocks every later write on the account when label
        creation overlaps other traffic (observed: a burst of label creates,
        then UID COPY/MOVE hung until Bridge was restarted)."""
        self._folder_names = None
        for name in names:
            if name in self._folders():
                continue
            try:
                self._server.create_folder(name)
            except imaplib.IMAP4.error:
                self._folder_names = None
                if name not in self._folders():
                    raise
                continue
            self._folder_names.add(name)

    def folder_exists(self, name: str) -> bool:
        return name in self._folders()

    def _folders(self) -> set[str]:
        if self._folder_names is None:
            self._folder_names = {name for _, _, name in self._server.list_folders()}
        return self._folder_names

    def supports_idle(self) -> bool:
        return bool(self._server.has_capability("IDLE"))

    def idle(self) -> None:
        self._server.idle()

    def idle_check(self, timeout: int = 30) -> list:
        return self._server.idle_check(timeout=timeout)

    def idle_done(self) -> None:
        self._server.idle_done()
