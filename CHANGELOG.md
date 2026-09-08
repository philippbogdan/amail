# Changelog

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
