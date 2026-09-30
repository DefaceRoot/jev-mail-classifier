import pytest

from jev_mail.config import ConfigError, load_config

MINIMAL = """
mailbox:
  host: imap.example.com
categories:
  - name: scam
    label: SCAM
    description: "Scam"
"""


def _load(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return load_config(path, env_path=tmp_path / "missing.env")


def test_load_full_contract_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("IMAP_USERNAME", "me@example.com")
    monkeypatch.setenv("IMAP_PASSWORD", "secret")
    config = _load(
        tmp_path,
        """
jev:
  provider: openrouter
  model: typesafe/jev-1.13
  default_threshold: 0.7
mailbox:
  host: bridge
  port: 143
  security: starttls
  tls_verify: false
  username: ${IMAP_USERNAME}
  password: ${IMAP_PASSWORD}
  batch_size: 10
  max_fetch_bytes: 1000
  watch_folders: [INBOX, "Folders/Bay Bravo"]
  label_folder: "Labels/JEV-{label}"
  folders:
    archive: Archive
    quarantine: "Folders/JEV Quarantine"
unmatched_label: REVIEW
categories:
  - name: scam
    label: SCAM
    description: "Scam"
    threshold: 0.8
    disposition: quarantine
    disposition_threshold: 0.9
  - name: receipt
    label: RECEIPT
    description: "Receipt"
""",
    )

    mb = config.mailbox
    assert (mb.host, mb.port, mb.security, mb.tls_verify) == ("bridge", 143, "starttls", False)
    assert (mb.username, mb.password, mb.batch_size, mb.max_fetch_bytes) == ("me@example.com", "secret", 10, 1000)
    assert mb.watch_folders == ["INBOX", "Folders/Bay Bravo"]
    assert mb.folders == {"archive": "Archive", "quarantine": "Folders/JEV Quarantine"}
    assert config.jev.model == "typesafe/jev-1.13"
    assert [(c.name, c.label, c.threshold, c.disposition) for c in config.categories] == [
        ("scam", "SCAM", 0.8, "quarantine"),
        ("receipt", "RECEIPT", None, None),
    ]
    assert [c.disposition_threshold for c in config.categories] == [0.9, None]


def test_defaults(tmp_path):
    config = _load(tmp_path, MINIMAL)

    assert config.mailbox.tls_verify is True
    assert config.mailbox.batch_size == 25
    assert config.mailbox.watch_folders == ["INBOX"]
    assert config.mailbox.poll_interval_seconds == 600
    assert config.unmatched_label == "REVIEW"
    assert config.jev.default_threshold == 0.7


@pytest.mark.parametrize(
    "text, message",
    [
        ("mailbox:\n  host: ''\ncategories: []\n", "mailbox.host"),
        (MINIMAL.replace("host: imap.example.com", "host: x\n  security: tls"), "security"),
        (MINIMAL.replace("categories:\n  - name: scam\n    label: SCAM\n    description: \"Scam\"\n", "categories:\n  scam:\n    description: x\n"), "list"),
        (MINIMAL + "    disposition: delete\n", "disposition"),
        (MINIMAL + "    disposition: quarantine\n", "mailbox.folders.quarantine"),
        (MINIMAL.replace("host: imap.example.com", "host: x\n  label_folder: JEV"), "{label}"),
        (MINIMAL.replace("host: imap.example.com", "host: x\n  folder: INBOX"), "replaced by mailbox.watch_folders"),
        (MINIMAL.replace("host: imap.example.com", "host: x\n  watch_folders: []"), "non-empty list"),
        (MINIMAL.replace("host: imap.example.com", "host: x\n  watch_folders: INBOX"), "non-empty list"),
        (MINIMAL.replace("host: imap.example.com", "host: x\n  watch_folders: [INBOX, INBOX]"), "duplicates"),
        (MINIMAL + "    threshold: 0.6\n    disposition: archive\n    disposition_threshold: 0.5\n", "below its label threshold"),
        (MINIMAL + "    disposition_threshold: 0.9\n", "without a disposition"),
        (MINIMAL + '  - name: scam\n    label: X\n    description: "dup"\n', "unique"),
    ],
)
def test_invalid_configs_are_rejected(tmp_path, text, message):
    with pytest.raises(ConfigError, match=message):
        _load(tmp_path, text)


def test_missing_env_var_raises(tmp_path):
    with pytest.raises(ConfigError, match="DEFINITELY_NOT_SET"):
        _load(tmp_path, MINIMAL.replace("host: imap.example.com", "host: x\n  username: ${DEFINITELY_NOT_SET}"))


def test_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.yaml")


def test_writable_folders_are_deduplicated_labels_then_unmatched_then_destinations(tmp_path):
    from tests.factories import make_config

    names = make_config().writable_folders()

    assert names[0] == "Labels/JEV-SCAM"
    assert names.index("Labels/JEV-REVIEW") == len(names) - 3
    assert names[-2:] == ["Archive", "Folders/JEV Quarantine"]
    assert len(set(names)) == len(names)
