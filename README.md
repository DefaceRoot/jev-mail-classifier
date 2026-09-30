# jev-mail-classifier

Sorts an IMAP inbox with [Jev](https://typesafe.ai), TypeSafe's System One model. Each
email gets one Jev call with one yes/no question per category, and Jev returns a
calibrated probability for each. Categories that clear their threshold become visible
labels, and at most one of them may move the email.

Nothing is stored outside the mailbox. Handled mail carries the IMAP keyword
`$JevProcessed`, and fetching never sets `\Seen`.

## What Jev sees

The headers `From`, `Reply-To`, `To`, `Cc`, `Date`, `Subject` and the first
`Authentication-Results`, plus `List-Unsubscribe: present` when that header exists. Then
the whole body: every non-attachment `text/plain` part, or the `text/html` parts
converted to text when there is no plain text. Nothing else is sent.

If the backend reports the input is too long for the model's context, the same email is
retried with the body halved (headers kept) down to 2000 characters.

## Quickstart

```bash
cp config.example.yaml config.yaml   # edit categories, folders, host
cp .env.example .env                 # one Jev API key + IMAP_USERNAME / IMAP_PASSWORD
pip install .

jev-mail check                       # connection, folders, keyword persistence
jev-mail run --dry-run --limit 10    # print decisions for a sample, touch nothing
jev-mail run                         # label and sort everything unprocessed, then exit
jev-mail watch                       # drain, then IMAP IDLE for new mail
```

Docker (config and `.env` live in the mounted `/data`; `watch` is the default command):

```bash
docker build -t jev-mail .
docker run -d --restart unless-stopped -v "$PWD/data:/data" jev-mail
docker run --rm -v "$PWD/data:/data" jev-mail check
```

Options go before the command: `jev-mail --dir DIR --folder NAME <command>`.
`mailbox.watch_folders` lists the folders one process covers. `--folder` is repeatable
and replaces that list for a single invocation:

```bash
jev-mail --folder INBOX --folder "Folders/Bay Bravo" watch
```

Run one process per account. Every label and destination folder is created once at
startup, before any COPY or MOVE, and a folder another process created first is tolerated.

## Commands

| Command | Behaviour |
| --- | --- |
| `run` | Creates any missing label and destination folders, then for each watch folder in order processes batches of `mailbox.batch_size` until a batch comes back smaller, so it backfills whole folders. `--dry-run` creates nothing and processes exactly one batch per folder without writing (`--limit N` sets its size). |
| `watch` | Creates missing folders, drains every watch folder in order, then waits on IDLE in the first one (up to `poll_interval_seconds`, at most 600) and repeats, so the other folders are rechecked at least that often. |
| `check` | Logs in, prints capabilities and the unprocessed count per watch folder, and verifies that custom keywords persist by storing `$JevProbe` on the newest message of the first watch folder, reconnecting, reading it back and removing it. Exits non-zero if a watch folder or `folders.archive` is missing, or the keyword does not persist. |

Each email logs one line to stdout: uid, subject (80 chars), labels, destination and the
top three probabilities. Bodies are never logged.

Failures. Timeouts, connection errors, 429, 5xx, and 401/402/403/404 from Jev stop the
process with a non-zero exit, so a supervisor such as Docker restarts it and the email is
retried. Other 4xx responses, or a body still too long at 2000 characters, give that one
email the unmatched label and mark it processed, so one bad email cannot wedge the
watcher. Lost connections exit non-zero as well. If the server forgets `$JevProcessed`,
`run` and `watch` stop as soon as a handled UID comes back, rather than looping and
billing forever.

## Configuration

See [`config.example.yaml`](config.example.yaml). Categories are an ordered list, first
is highest priority:

```yaml
unmatched_label: REVIEW
categories:
  - name: scam
    label: SCAM
    description: "Scam or phishing"
    threshold: 0.8            # optional, default is jev.default_threshold
    disposition: quarantine   # optional: keep | archive | quarantine
    disposition_threshold: 0.9  # optional, see below
```

**Labels.** Every category that clears its threshold adds its label, in priority order.
If none does, the email gets the `unmatched_label`. Labels are IMAP folders named by
`mailbox.label_folder` (for example `Labels/JEV-{label}`) and are applied with COPY, so
the message stays where it is.

**Dispositions.** The first matched category, in priority order, that has a disposition
and whose probability also clears its `disposition_threshold` decides the single move.
`disposition_threshold` defaults to the category's own threshold and may not be lower
than it, so a category can label at 0.5 but only quarantine at 0.8. A labelled category
below its disposition threshold has no say, so lower-priority categories can still move
the email. `archive` and `quarantine` move to `mailbox.folders.archive` and
`mailbox.folders.quarantine`. `keep` means no move and blocks lower-priority archive or
quarantine. A category with no disposition has no opinion. The marker is set before the
MOVE, so a crash never reprocesses a moved message.

## Proton Bridge

Bridge serves IMAP only on port 143 with STARTTLS and a self-signed certificate, so use
`security: starttls` and `tls_verify: false`. Labels are folders under `Labels/` and
cannot be nested (`Labels/JEV/X` fails), so use `label_folder: "Labels/JEV-{label}"`. Real
folders live under `Folders/`, and the archive folder is `Archive`. Custom keywords such
as `$JevProcessed` persist even though `PERMANENTFLAGS` lacks `\*`. Run `check` to confirm.

Run one `jev-mail` process per Bridge account and list every folder in
`watch_folders` (for example `[INBOX, "Folders/Bay Bravo"]`). Two processes creating
labels and copying at once deadlocked Bridge, hanging every write until it was restarted.

## Gmail

Use an [app password](https://myaccount.google.com/apppasswords) (2-Step Verification
required) with IMAP enabled, `host: imap.gmail.com`, `port: 993`, `security: ssl`.
Labels are nested folders (`label_folder: "JEV/{label}"`). Archiving is a MOVE out of
INBOX to `[Gmail]/All Mail`. COPY into a label folder adds the label and keeps the
message in INBOX.

## Jev providers

Set one key in `.env`, checked in this order, or pin `jev.provider`:

| Env var | Backend |
| --- | --- |
| `TYPESAFE_API_KEY` | TypeSafe API, direct |
| `OPENROUTER_API_KEY` | OpenRouter Decisions endpoint (live-verified; honours `jev.model`) |
| `AI_GATEWAY_API_KEY` | Vercel AI Gateway |

The TypeSafe-direct and Vercel adapters are built from published docs and are untested
against a live key. `jev_mail/providers/` is the place to look if one breaks.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

## License

MIT, see [LICENSE](LICENSE).
