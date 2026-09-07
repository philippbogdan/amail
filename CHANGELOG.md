# Changelog

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
