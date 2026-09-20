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

Formatting checks inspect the MIME structure as well as rendered text. Reads expose `body_format` and warnings for Apple Mail's shared-content wrapper and fully quoted HTML bodies. The plain alternative retains its actual quote markers. New-send verification compares both the plain body and the paragraph structure of a present HTML alternative with the requested text, preserving leading whitespace and paragraph boundaries while normalising transport line endings. Hidden HTML preheaders cannot conceal a fully quoted body. The HTML check handles the simple markup emitted by Mail and is not a cross-client CSS rendering guarantee. A failed verification pauses batches and retains the existing no-resend safeguard.

Earlier content checks removed Apple's quote wrapper and allowed whitespace reflow. Those historical checks did not establish correct message formatting. Version 0.3.0 also replaces native body and attachment entry. Previously delivered messages are not changed.

## Version 0.3.0 live formatting checks

On macOS 27.0, six received internal test messages were checked directly in the Mail cache with transport headers. The set covered a two-message native batch, Gmail and Exchange replies in one conversation, text and binary attachments, and Mail's missing-attachment validation. Received bodies had no added leading blank line or Apple shared-content quote wrapper. Reply headers matched the preceding message. Binary attachment bytes and text attachment contents matched; the provider normalised text-file line endings. The institutional receiving gateway added its usual external-sender notice to the incoming Gmail reply.

A received message was also inspected visually in desktop Mail. No physical iPhone rendering check was performed. The test emails and account identifiers remain private. Native submission measured about two to four seconds on this machine for the small checked messages, excluding provider synchronisation.

Mail can replace a saved draft's Message-ID when sending. The draft identity is retained as preparation evidence; acceptance is corroborated using new Sent records and verified content, and strengthened by a matching delivered copy when available.

The standard-library suite covers read-only SQL access, Gmail labels, literal filenames and search, stale refs, MIME transfer encodings, Unicode, unavailable attachments, filename collisions, nested attached messages, immutable drafts and plans, configured pacing, concurrency, suppression, correlated feedback and crash recovery. CI uses synthetic fixtures and makes no email requests.

The release checks also exercise isolated installation, config migration and reuse of existing credential storage. A working fixture suite does not establish a new provider or macOS version as supported; live validation on that surface is separate.

## Version 0.3.1 rich-reply regression

The 0.3.0 live sample missed a native-selection failure on rich messages whose accessibility children repeat warning-banner and table text. Version 0.3.1 reads the editor through its native full-document text range. It retains the complete-selection guard and saved-MIME checks. Compose windows display the actual subject throughout, and an existing window with that exact subject prevents preparation before editing.

A six-message internal conversation alternated Gmail and institutional Exchange. It covered rich HTML, security notices, signature tables, quoted history, Unicode, a long paragraph, text and binary attachments, and an attached email. Every reply retained the reply prefix and referred to the preceding delivered Message-ID. Received copies were checked with transport headers and visually inspected in desktop Mail. The receiving gateway canonicalised the attached email headers and line endings; its addresses, subject and body were preserved. No physical iPhone check was performed.

The fixture suite now includes duplicate accessibility labels, missing native text ranges, existing-subject collision protection and unchanged-draft retries. A confirmed rejection can be retried with the same request ID; unknown outcomes remain non-retryable.

## Version 0.4.0 native replies and Mail-only sending

Every account now sends through Mail's own compose window; the Gmail API send route and its expiring OAuth dependency are gone (live Gmail reads remain optional). Replies and forwards use Mail's `reply` and `forward` verbs, and amail inserts the reviewed text above the history Mail composed instead of replacing the whole body, so the attribution line, quoted history, forwarded attachments and thread headers are Mail's own. amail no longer writes quote markers or forward headers itself. Draft and sent-copy verification checks that the message begins with the reviewed text as whole paragraphs, with no leading blank line and no share wrapper; extra MIME parts are accepted only for replies and forwards (quoted inline images, forwarded originals). A failed preparation closes the compose window without saving.

Acceptance no longer depends on Exchange server metadata that Mail syncs hours later, nor on a one-hour reconciliation window: a Sent copy filed by Mail with nothing queued in an Outbox is accepted, and reconciliation uses the request time. On this Mac a previous 0.3.1 send stuck at `provider_acceptance_unverified`, which blocked the account, reconciled to `accepted` with the new rule.

Live checks on macOS 27.0 with Mail 16.0 between the maintainer's own Gmail and Exchange accounts: a new Exchange message with an attachment, a Gmail reply through Mail, an Exchange forward carrying the original attachment, and an Exchange reply to a rich message with an institutional banner. Every sent and delivered copy began with the requested text, had no leading blank line or share wrapper, carried Mail's own quoted history below the text, and was accepted within about three seconds. 106 fixture tests pass.

## Public packaging checks

The public release passes 56 fixture tests, including an installation with no configured accounts, registration of a synthetic OAuth client, rejection of unknown accounts, and a repeat upgrade that preserves settings. The reusable agent skill passes its metadata validator.

The existing three-account development installation was upgraded to the public code without changing its existing state files or ledger contents. Both Gmail API identity checks and the Exchange Mail permission check passed. The previous display name, enabled accounts and customised Exchange limits were retained. Per-account send previews and cached reads passed. No new live email was sent as part of packaging.
