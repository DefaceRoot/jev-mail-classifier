from __future__ import annotations

import email
import email.policy
import re
from collections.abc import Iterator
from dataclasses import dataclass
from email.message import Message
from html.parser import HTMLParser

_HEADER_ORDER = ("From", "Reply-To", "To", "Cc", "Date", "Subject")
_BLOCK_TAGS = {"br", "p", "div", "tr", "li", "table", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "hr"}
_SKIPPED_TAGS = {"script", "style"}
MIN_BODY_CHARS = 2000


@dataclass(frozen=True)
class Email:
    uid: int
    subject: str
    headers: str
    body: str

    def state(self, max_body_chars: int | None = None) -> str:
        body = self.body if max_body_chars is None else self.body[:max_body_chars]
        return f"{self.headers}\n\n{body}"


def parse_email(uid: int, raw: bytes) -> Email:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    lines = []
    for name in _HEADER_ORDER:
        value = _one_line(msg.get(name))
        if value:
            lines.append(f"{name}: {value}")
    if msg.get("List-Unsubscribe") is not None:
        lines.append("List-Unsubscribe: present")
    auth = _one_line(msg.get("Authentication-Results"))
    if auth:
        lines.append(f"Authentication-Results: {auth}")
    return Email(uid=uid, subject=_one_line(msg.get("Subject")), headers="\n".join(lines), body=_extract_body(msg))


def _one_line(value: object) -> str:
    return " ".join(str(value).split()) if value is not None else ""


def _leaf_parts(part: Message, content_type: str) -> Iterator[Message]:
    # Attachments are skipped wholesale, including any message/rfc822 they wrap.
    if part.get_content_disposition() == "attachment" or part.get_filename():
        return
    if part.is_multipart():
        for sub in part.iter_parts():
            yield from _leaf_parts(sub, content_type)
    elif part.get_content_type() == content_type:
        yield part


def _decode(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    try:
        return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _extract_body(msg: Message) -> str:
    plain = "\n\n".join(t for t in (_decode(p).strip() for p in _leaf_parts(msg, "text/plain")) if t)
    if plain:
        return plain
    return "\n\n".join(t for t in (html_to_text(_decode(p)) for p in _leaf_parts(msg, "text/html")) if t)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in _SKIPPED_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        if tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._chunks.append(data)

    def text(self) -> str:
        lines = (re.sub(r"[ \t\r\f\v\xa0]+", " ", line).strip() for line in "".join(self._chunks).split("\n"))
        return "\n".join(line for line in lines if line)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return parser.text()
