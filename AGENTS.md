# Working on amail

Read README.md and docs/SETUP.md for the supported audience and runtime. The reusable email-operation skill is skills/amail/SKILL.md.

- Keep account addresses, OAuth clients, credentials, mailbox contents, message IDs, local state and development evidence out of Git. Use synthetic fixtures and example.org/example.net addresses.
- Never write to Apple's Mail or Accounts SQLite databases. Keep local access read-only; changes go through authorised provider or Mail scripting routes.
- Do not equate a queue acknowledgement or Mail boolean with provider acceptance. Preserve unknown-outcome handling, request-id deduplication and the shared ledger.
- Upgrades must retain the private config, account selection, display name, pacing overrides, credentials and send history. Test both an empty installation and a legacy upgrade.
- Native sends use targeted Accessibility operations on amail's own compose window to avoid Mail's scripted-body quoting bug. Keep composition serialised, preserve the clipboard, and verify the saved draft before submission. Never extract Apple credentials or add automatic account switching after a send restriction.
- The fixture suite must not need real accounts, authentication, Mail access or outbound messages. Run `python3 -m unittest discover -s tests -v` for changes that affect behaviour.
- Obtain approval before test emails unless the account owner has explicitly granted standing permission for tests exclusively between their own verified accounts. Honour that standing permission without repeatedly asking. Verify receipt, and clean up exact test messages when permitted.
