# Changelog

## 0.5.0

- Send in the background. The reviewed text now goes into the compose window through the editor's accessibility text operation: Mail stays behind whatever is in front, nothing is typed and the clipboard is not touched. Only attachments are still pasted, which brings Mail forward for under a second, and amail then hands the keyboard back to the application that had it. An editor that refuses the operation gets the text by paste as before. The result records `body_entry`.
- Reply to and forward HTML-heavy mail. The quoted history of a newsletter puts tens of thousands of elements in the compose window (91,579 in one case), and every search for the editor walked them until it hit a bound of 500: "Mail compose accessibility tree exceeded its bound". Searches now stay out of the quoted history, and attachments are counted from the editor's text.
- Give every sending command `--timeout`. `draft send`, `reply --send`, `forward --send`, `batch run` and `batch resume` had none. The default is 60 seconds instead of 30 and the maximum 300 instead of 120, for a Mac under load.
- Settle earlier sends before refusing a new one. A send left `provider_acceptance_unverified`, or `submitting` by a process that died, blocked its account until someone ran `amail status`. A new send now runs that reconciliation itself, and only an outcome that is still unknown blocks the account.
- Close what a failed send leaves behind. A window that was prepared but never submitted, because the deadline ran out first or Mail was too slow to show it, is now closed. Before, it stayed open with its saved draft, which is the compose window with a save prompt that a timed-out `draft send` left on a loaded Mac, and it blocked every retry with the same subject. If Mail will not close a window at all, the result carries `compose_window_left_open`, and `amail status REQUEST_ID` or the next send from the account closes it. amail closes only the window it opened: that window's id in the Mail that opened it, with the same subject and sender, where before any window with the subject was closed.
- Remove the draft a failed send leaves. The sweep after a discarded window first asked Mail for that window, which no longer existed, so it failed every time and each failed send left its saved copy in Drafts. It now runs without the window, deletes only while Mail has no Drafts action queued, never deletes the same server copy twice, and waits for a save Mail writes after the window has closed.
- Wait up to three seconds for the editor to take the keyboard before a paste, instead of checking once after 50 ms. A Mail brought forward on a busy Mac needs longer, and the single check rejected the send.
- Keep a slow status check from overwriting a newer result. A reconciliation now stores its conclusion only if no other process has changed the request since it read it.
- Report where a send's time went. `timings_ms` gives the compose, body, save, draft verification, draft settling, submission and acceptance stages.
- Print usage errors as one JSON line on stderr like every other error, with the command, its `-h` and, for a mistyped option or command, the closest real one. Nested commands such as `draft send` and `batch run` now reject abbreviated options as the top-level ones do.
- Clean up after a killed send and a finished batch. Scratch directories left in the state directory by a killed send, each holding that message's text, are removed before the next composition, and a finished batch no longer leaves its worker lock behind.

## 0.4.4

- Restart Mail between messages before it hangs. Mail 16 leaves one worker thread blocked for good behind every compose window it opens, sent or not (its own Command-N included); around sixty exhaust its dispatch thread pool and Mail stops responding, which a long batch reached every time. amail counts the compose windows it opens per Mail process and, after 40, quits and reopens Mail before the next message when no compose window is open. With a window of yours open it waits, and past 55 it refuses with a message instead of risking the hang. The restart does not count against the message's timeout; the send result records `mail_restarted_after_composes`.
- Refuse to compose when macOS has paused Mail. Once the system runs out of swap, the kernel suspends large apps; a paused Mail answers nothing, so sends used to time out. amail now says so and how to resume Mail.
- Wait up to five seconds for the compose editor to load, so the first message after Mail starts is not rejected with "Cannot identify one Mail body editor".
- `amail doctor` reports `mail_paused`, `mail_composes_since_launch` and `mail_restart_after_composes`.

## 0.4.3

- Confirm a sent copy whose filed plain text carries a trailing space on a line. Mail can end the last line of the plain part with a space; verification compared it character for character, so an accepted send stayed `provider_acceptance_unverified` and the reconciliation gate then refused every later send on the account. Trailing spaces and tabs at line ends are now ignored when checking that a message begins with the reviewed text; leading space, extra words and paragraph layout still fail verification.

## 0.4.2

- Submit only once Mail's Drafts state has settled: the verified draft is the compose window's only saved copy, the server knows it, and Mail has no Drafts action queued (up to 20 seconds). Mail saves a window more than once; a Drafts sync landing between a superseded save's local and server deletes re-added it locally, sending then asked the server to delete it a second time, and Mail retried that failing delete forever. Every later Sent copy, move and delete for the account queued behind it, seen as a "Copying Messages" activity that never finished. Delivery was never affected. Results record `drafts_settled`.
- Stop the send and close the window if the editor text changes while the drafts settle, so a stray keystroke is never sent unreviewed.
- `amail doctor` reports `mail_queued_server_actions` per account; a count that stays above zero while Mail is idle means a queued action is failing.
- Remove an unreachable duplicate of the discard and stale-window handlers from the compose script.

## 0.4.1

- Remove Mail's autosaved copy of a discarded compose window only once the server knows it. Deleting an unsynced draft left Mail a "Moving Messages" action it could never finish, which jammed every later move for the account until the queue was cleared by hand.

## 0.4.0

- Compose replies and forwards with Mail's own `reply` and `forward` verbs and insert the reviewed text above the history Mail built, instead of replacing the whole body. Quoted history, attribution lines, forwarded attachments and thread headers are now Mail's own; amail no longer writes quote markers or forward headers.
- Send every account through Mail's compose window. The Gmail API send route and its expiring OAuth dependency are removed; `gog` remains optional for live Gmail reads only, and `doctor` reports it separately from sending.
- Verify drafts and sent copies by whole-paragraph prefix: the message must begin with the reviewed text, with no leading blank line or share wrapper. Extra MIME parts are accepted only for replies and forwards.
- Accept a send once Mail has filed its Sent copy with nothing queued in an Outbox, and reconcile from the request time rather than a one-hour window, so slow Exchange metadata no longer leaves sends unverified and the account blocked.
- Close amail's compose window without saving when preparation fails, so a retry never finds a stray window with the same subject.
- Default a reply's sender to the alias the original was addressed to. Add `forward_ref` to the message schema. Accept `draft show` output unchanged in `draft update`.
- Reject abbreviated flags (`--since` no longer silently means `--since-hours`), add `--after`, `--before` and `--limit` to `sent`, accept `attachment` as an alias of `attachments`, and list the available names in unknown `--fields` errors.
- Send images: Mail shows a pasted image inline, so the editor check accepts images and the saved draft's MIME parts are the gate. Reading an image element's accessibility value no longer aborts the process.
- A draft with `reply_to_ref` or `forward_ref` gets the `Re:` or `Fwd:` prefix Mail would add; `draft update` accepts the whole `draft show` output; new messages need some body text; recipient, timeout and cap problems each get their own message; Mail's error text accompanies compose failures.
- Resolve dead sends honestly: a request whose process died before a draft was prepared, or whose prepared draft still sits unsent in Drafts two minutes later, becomes `rejected` on the next `status` check (and its stale compose window is closed) instead of blocking the account forever. A death after submission started stays `outcome_unknown`.

## 0.3.1

- Read native editor text from one document range so rich replies containing tables and repeated accessibility labels pass complete-selection verification.
- Keep the real subject, including reply prefixes, visible throughout composition. Stop before touching an already-open compose window with the same subject.
- Add `draft send --retry-rejected` for retrying an unchanged draft after a confirmed rejection, preserving its request ID and unknown-outcome safeguards.

## 0.3.0

- Use Mail's editor for native message bodies and attachments, avoiding the scripted compose path that wrapped new text as a quotation. Native sends now require Accessibility access for the invoking host and briefly show their own compose window.
- Verify the saved draft's body, MIME alternatives, recipients, reply header and attachments before submission. Serialise native compositions, preserve clipboard formats, and stop on focus changes.
- Keep compose validation visible and handle Mail's missing-attachment warning when the reviewed request deliberately contains no attachments.
- Preserve quote markers in plain-text alternatives instead of concealing Mail's shared-content wrapper during reads.
- Report MIME formatting diagnostics and reject full-body HTML quotations, added leading blank lines and paragraph changes during new-send verification.
- Keep a batch paused on a formatting verification failure without resending the ambiguous message on resume.
- Preserve configuration, authentication, account budgets, drafts and send history during upgrades. Honour explicitly granted standing permission for internal test mail.

## 0.2.1

- Restrict field projection to reading commands so output-field errors cannot obscure a completed send or mailbox change.
- Render list/search plain and TSV output as rows, retaining scope metadata on stderr; support multiple body-only reads in text output.

## 0.2.0

- List/search now return explicit scope, count and pagination metadata. Use `--bare` for the old bare-array contract; `--page` remains accepted.
- Add `--all-mailboxes`, field-only search, `--fields`, multi-ref reads and `thread --full` / `--fetch`.
- Add account-independent `schema message` and `schema batch` commands and descriptive command help.
- Make batch status compact by default, retaining full evidence with `--details` and checking real worker presence.
- Make no-argument status run account diagnostics; request-specific status is unchanged.
- Add frozen bulk marking previews with explicit scope, cutoff, bounded operations, identity checks and resumable results.
- Preserve existing configuration, credentials, pacing, send ledger and ambiguous-outcome safeguards.


## 0.1.0

Initial public release for macOS users with Google or Exchange accounts connected to Apple Mail.

- Read-only cache access, decoded body search and explicit content availability.
- Gmail API and authorised Mail scripting, with corroborated send outcomes.
- Local drafts, replies, forwards, mailbox operations and reviewed JSONL batches.
- Shared pacing, suppression, send history and duplicate protection.
- Explicit first-run account selection and private OAuth setup.
- Upgrade migration that preserves existing account selection, display name, policies and credential storage.
- Agent skill, setup guide, synthetic tests and CI.
- Homebrew distribution through `philippbogdan/tap/amail`.

Runtime validation is currently limited to the Mac and account types described in docs/VALIDATION.md. No private mail, account configuration or credentials are distributed.
