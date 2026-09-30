import email.message

from jev_mail.email_state import Email, parse_email


def _msg(**headers) -> email.message.EmailMessage:
    msg = email.message.EmailMessage()
    for name, value in headers.items():
        msg[name.replace("_", "-")] = value
    return msg


def test_state_has_sender_headers_then_blank_line_then_full_body():
    msg = _msg(
        From="Alice <alice@example.com>",
        Reply_To="billing@evil.test",
        To="colin@baybravo.test",
        Date="Tue, 29 Sep 2026 10:00:00 +0000",
        Subject="Invoice due",
    )
    msg.set_content("Pay now.\n" + "x" * 50000)

    parsed = parse_email(7, msg.as_bytes())

    assert parsed.uid == 7
    assert parsed.subject == "Invoice due"
    assert parsed.headers == (
        "From: Alice <alice@example.com>\n"
        "Reply-To: billing@evil.test\n"
        "To: colin@baybravo.test\n"
        "Date: Tue, 29 Sep 2026 10:00:00 +0000\n"
        "Subject: Invoice due"
    )
    state = parsed.state()
    assert state.startswith(parsed.headers + "\n\nPay now.")
    assert state.endswith("x" * 50000)


def test_rfc2047_subject_and_sender_are_decoded():
    raw = b"From: =?utf-8?q?J=C3=BCrgen?= <j@example.com>\r\nSubject: =?utf-8?q?Caf=C3=A9_menu?=\r\n\r\nhi\r\n"

    parsed = parse_email(1, raw)

    assert parsed.subject == "Café menu"
    assert "From: Jürgen <j@example.com>" in parsed.headers


def test_list_unsubscribe_is_reduced_to_a_flag_and_auth_results_to_one_line():
    raw = (
        b"From: a@example.com\r\n"
        b"List-Unsubscribe: <https://tracker.example/u/abc123>\r\n"
        b"Authentication-Results: mx.example.com;\r\n\tspf=pass smtp.mailfrom=a@example.com;\r\n\tdkim=fail\r\n"
        b"Authentication-Results: second.example.com; none\r\n"
        b"\r\nbody\r\n"
    )

    headers = parse_email(1, raw).headers

    assert "List-Unsubscribe: present" in headers
    assert "tracker.example" not in headers
    assert "Authentication-Results: mx.example.com; spf=pass smtp.mailfrom=a@example.com; dkim=fail" in headers
    assert "second.example.com" not in headers


def test_html_only_message_is_converted_to_text_without_script_or_style():
    msg = _msg(Subject="Sale")
    msg.set_content(
        "<html><head><style>p{color:red}</style></head><body>"
        "<script>track()</script><p>Big   <b>sale</b> &amp; more</p><div>Buy now</div></body></html>",
        subtype="html",
    )

    parsed = parse_email(1, msg.as_bytes())

    assert parsed.body == "Big sale & more\nBuy now"


def test_plain_part_wins_over_html_alternative():
    msg = _msg(Subject="Hi")
    msg.set_content("plain version")
    msg.add_alternative("<p>html version</p>", subtype="html")

    assert parse_email(1, msg.as_bytes()).body == "plain version"


def test_attachments_are_excluded_but_inline_text_parts_are_joined():
    msg = _msg(Subject="Files")
    msg.set_content("first part")
    msg.add_attachment(b"SECRET attachment text", maintype="text", subtype="plain", filename="notes.txt")
    msg.add_attachment(b"<p>attached html</p>", maintype="text", subtype="html", filename="page.html")

    body = parse_email(1, msg.as_bytes()).body

    assert body == "first part"


def test_blank_plain_part_falls_back_to_html():
    msg = _msg(Subject="Hi")
    msg.set_content("  \n")
    msg.add_alternative("<p>real content</p>", subtype="html")

    assert parse_email(1, msg.as_bytes()).body == "real content"


def test_state_truncates_body_but_keeps_headers():
    mail = Email(uid=1, subject="s", headers="From: a@example.com", body="abcdefghij")

    assert mail.state(max_body_chars=4) == "From: a@example.com\n\nabcd"
    assert mail.state() == "From: a@example.com\n\nabcdefghij"
