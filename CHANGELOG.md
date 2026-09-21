# Changelog

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
