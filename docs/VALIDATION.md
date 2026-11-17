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

An independent adversarial pass then drove the working tree black-box between the same accounts (about twenty delivered messages: Unicode subjects, leading spaces, a first line starting with `> `, long paragraphs, alias senders, cross-account replies, concurrent sends, request-id replays) and found five defects, all fixed and re-tested live: image attachments were refused and a process could abort on an image element's accessibility value; an empty body left a compose window open; a `reply_to_ref` draft lost its `Re:` prefix; `draft update` rejected the `draft show` envelope; and a process that died before submitting blocked its account permanently.

Live checks on macOS 27.0 with Mail 16.0 between the maintainer's own Gmail and Exchange accounts: a new Exchange message with an attachment, a Gmail reply through Mail, an Exchange forward carrying the original attachment, and an Exchange reply to a rich message with an institutional banner. Every sent and delivered copy began with the requested text, had no leading blank line or share wrapper, carried Mail's own quoted history below the text, and was accepted within about three seconds. 106 fixture tests pass.

## Version 0.4.1 draft sweep

Deleting a draft that Mail had autosaved but not yet uploaded queued a move Mail retried forever, visible as a stuck "Moving Messages" activity; 43 such dead actions accumulated on the maintainer's Exchange account during the 0.4.0 tests and were removed from Mail's action queue by hand. The sweep now waits for the cache row to carry a server id before deleting, and leaves an unsynced draft alone. A forced failed preparation on the live account closed its window, left no draft and added no action.

## Version 0.4.2 settled drafts

A 32-message Exchange batch on 0.4.1 left Mail's "Copying Messages" activity stuck at 1 of 60. Mail's action log showed the cause on the second message: its compose window was saved twice, the first save was deleted locally, a Drafts sync re-added it from the server moments before Mail deleted it there, and sending then deleted both local copies. Exchange refused the delete of the item already gone, Mail logged it as a connection error and replayed it about 12,000 times in eight minutes, and the 31 later Sent copies queued behind it. All 32 messages had been delivered. Restarting Mail did not clear it; removing that one action from Mail's queue by hand drained the rest in about ten seconds.

With the settle gate, two runs of eight internal Exchange-to-Gmail messages each deleted exactly one draft per send, Mail logged no replay errors, the action queue drained to zero after every message, and three superseded saves re-added by a sync were waited out rather than sent over. Each send took about 3 to 7 seconds instead of 2 to 3. During the first run someone used the Mac: Mail's log shows one compose window sent from Mail's own Send command while amail was still waiting for its draft, and one received a trailing space typed after verification. Both messages were delivered and have been reconciled in the ledger by hand. The editor re-check now stops a send like the second one before submission.

## Version 0.4.4 Mail restarts

Mail 16 hung in the middle of long batches. A hang report from a 62-message Exchange batch showed 63 threads on Mail's compose work queue, each blocked on a future that never resolved, and the dispatch pool at its limit of 64. Sampling a live Mail showed one new permanently blocked thread per compose window, including a window opened with Command-N and closed unsent. A separate freeze on 29 September was macOS pausing Mail when the system ran out of swap (task suspend count 1, no thread ran). With 0.4.4 a seeded compose count of 40 made amail quit and reopen Mail before a test message between the owner's own accounts, which was accepted with `mail_restarted_after_composes: 40`; pause detection was checked against a stopped process. 124 fixture tests pass.

## Version 0.4.3 line-end spaces

An Exchange batch send on 0.4.2 stayed `provider_acceptance_unverified` although Mail had filed the Sent copy and synced it to the server. The only difference from the reviewed text was a trailing space after the final line of the filed plain part, so the whole-paragraph check failed and the ledger gate blocked all further sends on the account. With 0.4.3 the same ledger entry reconciles from its synced Sent item, read-only. 116 fixture tests pass.

## Version 0.5.0 background sends

`amail reply` failed on HTML-heavy mail with "Mail compose accessibility tree exceeded its bound". A reply window above one newsletter held 91,579 accessibility elements, nearly all of them the quoted history inside the editor, and walking them took 13 seconds. The window's own controls are 51 elements. amail 0.4.4 walked the whole tree to find the editor and to count attachments and stopped at 500. A live reply above a second newsletter, with about 1,800 table elements and sent between the maintainer's own accounts, was rejected by 0.4.4 with that error and accepted by 0.5.0 from the same draft.

The editor accepts WebKit's accessibility text operation (`AXTextOperation`, replace, on an empty range at the start of the document) in a window that is not in front. The saved draft is the one a plain-text paste produces, except that the first line is not wrapped in a `div`, which is how typed text is saved. It passes the same whole-paragraph draft and Sent checks. After the operation the insertion point sits at the end of the entered text, so attachments pasted next land directly below it, and each shows as one object character in the editor's text.

Eight live sends on macOS 27.0 with Mail 16.0 between the maintainer's own Gmail accounts were accepted, and the received copies of the first seven were read back: a new message, a native reply, the reply above the heavy newsletter, a message with a text file and an image, a native forward carrying both, two messages with one attachment, and a new message with the text operation disabled to force the paste. Another application stayed in front throughout the four without attachments. The sends that pasted held the front for about half a second, handed it back and left the clipboard as it was. On an idle machine a send took 4.5 to 10.4 seconds: opening the window 0.5 to 2.0, entering the body 0.05 to 0.6, saving 0.3, verifying the draft 0.1 to 0.3, waiting for the draft to settle on the server 1.7 to 7.6, submitting 0.4 to 0.5, observing acceptance 0.7 to 1.6. AppleScript start-up is 0.03 seconds, so the time is Mail's and the server's.

During the last checks another job drove the load average past 100. One attachment send was rejected before any edit because the editor had not taken the keyboard at the single check then made 50 ms after the click, which is why amail now waits up to three seconds for it. The next ended `outcome_unknown` after 36 seconds, because Mail took over a minute to close the window it had submitted, and `amail send-status` settled it as accepted from the Sent copy. The rejected one left its autosaved draft in Drafts: the sweep after a discard gives up when no draft has appeared within five seconds, and under that load Mail saved it later.

Not checked live: Exchange (the account had another session's send awaiting reconciliation), the closing of a prepared but unsubmitted window and of a window Mail would not close (fixture tests only), and macOS versions before 27. 145 fixture tests pass.

## Public packaging checks

The public release passes 56 fixture tests, including an installation with no configured accounts, registration of a synthetic OAuth client, rejection of unknown accounts, and a repeat upgrade that preserves settings. The reusable agent skill passes its metadata validator.

The existing three-account development installation was upgraded to the public code without changing its existing state files or ledger contents. Both Gmail API identity checks and the Exchange Mail permission check passed. The previous display name, enabled accounts and customised Exchange limits were retained. Per-account send previews and cached reads passed. No new live email was sent as part of packaging.
