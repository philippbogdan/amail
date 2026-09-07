# Validation and compatibility

## Tested surface

The initial implementation was tested on an Apple Silicon Mac running macOS 26.5, with Python 3.14, two personal Gmail accounts, gog 0.10.0, and one institutional Exchange account already authorised in Apple Mail. The public packaging adds synthetic tests for first-run setup, credential registration, account selection, configuration validation and preservation of legacy settings.

Runtime compatibility with every older or future macOS Mail schema is not established. Accounts4.sqlite and the versioned Mail cache are private Apple formats. Mail must have downloaded the requested content. Generic IMAP, POP, iCloud-only use, Windows and Linux runtime operation are not part of the supported account matrix in this release. Gmail Workspace quota differences and organisation-specific restrictions remain partly unknown.

## Initial live results

- A census found all 31,441 expected files in the inspected cache across three accounts. This was local-store coverage, not a count of every server message.
- All 450 sampled messages had decoded bodies. Locally available sampled attachments were extracted and hashed; absent data was reported explicitly.
- Final cached metadata/body reads had medians around 56 to 62 ms and observed p95 values below 100 ms on that Mac.
- Three final small sends, with a binary attachment, reached observed acceptance in about 1.0 to 1.5 seconds. Fifteen accepted integration-test sends had matching received copies with expected content and attachments.
- Exchange's Mail Undo Send delay caused a prior fixed hold. After the user disabled it, small-message acceptance took about 1 to 2 seconds. Forwarding and attachment preparation can take longer; one native forward took about 3.15 seconds.
- The first production body index took about 115 seconds for roughly 31,000 records, with six unavailable bodies and no parsing errors. A later incremental refresh took about 3.2 seconds.
- Test messages were removed from the active inboxes and drafts. Private account identifiers, message IDs, raw mail and logs are deliberately absent from this repository.

These are small-sample development observations. They are not latency, server completeness, deliverability or inbox-placement guarantees. Public installation does not automatically run live tests.

## Automated checks

The standard-library suite covers read-only SQL access, Gmail labels, literal filenames and search, stale refs, MIME transfer encodings, Unicode, unavailable attachments, filename collisions, nested attached messages, immutable drafts and plans, configured pacing, concurrency, suppression, correlated feedback and crash recovery. CI uses synthetic fixtures and makes no email requests.

The release checks also exercise isolated installation, config migration and reuse of existing credential storage. A working fixture suite does not establish a new provider or macOS version as supported; live validation on that surface is separate.

## Public packaging checks

The public release passes 56 fixture tests, including an installation with no configured accounts, registration of a synthetic OAuth client, rejection of unknown accounts, and a repeat upgrade that preserves settings. The reusable agent skill passes its metadata validator.

The existing three-account development installation was upgraded to the public code without changing its existing state files or ledger contents. Both Gmail API identity checks and the Exchange Mail permission check passed. The previous display name, enabled accounts and customised Exchange limits were retained. Per-account send previews and cached reads passed. No new live email was sent as part of packaging.
