from __future__ import annotations

import imaplib
import ssl

from imapclient import IMAPClient

from jev_mail.config import Decision, MailboxConfig
from jev_mail.email_state import Email, parse_email

PROCESSED_KEYWORD = "$JevProcessed"


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
    """Thin wrapper around imapclient.IMAPClient: fetch unprocessed mail and
    apply a Decision to it. Use as a context manager so the connection always
    gets closed."""

    def __init__(self, config: MailboxConfig, server: IMAPClient | None = None):
        self._config = config
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
        self._server.select_folder(folder)

    def capabilities(self) -> list[str]:
        return sorted(c.decode() if isinstance(c, bytes) else c for c in self._server.capabilities())

    def unprocessed_uids(self) -> list[int]:
        return sorted(self._server.search(["UNKEYWORD", PROCESSED_KEYWORD]), reverse=True)

    def fetch_unprocessed(self, limit: int | None = None) -> list[Email]:
        """Newest unprocessed mail first (UIDs grow with arrival). Uses
        BODY.PEEK so fetching never sets \\Seen, and a partial fetch so one
        huge message can't blow the bandwidth budget."""
        uids = self.unprocessed_uids()
        if limit is not None:
            uids = uids[:limit]
        if not uids:
            return []
        response = self._server.fetch(uids, [f"BODY.PEEK[]<0.{self._config.max_fetch_bytes}>"])
        emails = []
        for uid in sorted(response, reverse=True):
            # imapclient keys a partial fetch as BODY[]<origin>, not BODY.PEEK[]
            raw = next(v for k, v in response[uid].items() if k.startswith(b"BODY[]"))
            emails.append(parse_email(uid, raw))
        return emails

    def apply(self, uid: int, decision: Decision) -> None:
        """Order matters. Label COPYs are idempotent, so a crash before the
        marker only repeats them. The marker is set before the MOVE so a crash
        in between never leaves a moved message looking unprocessed."""
        for label in decision.labels:
            self._server.copy([uid], label)
        self._server.add_flags([uid], [PROCESSED_KEYWORD])
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

    def newest_uid(self) -> int | None:
        uids = self._server.search(["ALL"])
        return max(uids) if uids else None

    def keywords(self, uid: int) -> set[str]:
        flags = self._server.get_flags([uid]).get(uid, ())
        return {f.decode() if isinstance(f, bytes) else f for f in flags}

    def add_keyword(self, uid: int, keyword: str) -> None:
        self._server.add_flags([uid], [keyword])

    def remove_keyword(self, uid: int, keyword: str) -> None:
        self._server.remove_flags([uid], [keyword])

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
