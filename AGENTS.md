# Working on amail

Read README.md and docs/SETUP.md for the supported audience and runtime. The reusable email-operation skill is skills/amail/SKILL.md.

- Keep account addresses, OAuth clients, credentials, mailbox contents, message IDs, local state and development evidence out of Git. Use synthetic fixtures and example.org/example.net addresses.
- Never write to Apple's Mail or Accounts SQLite databases. Keep local access read-only; changes go through authorised provider or Mail scripting routes.
- Do not equate a queue acknowledgement or Mail boolean with provider acceptance. Preserve unknown-outcome handling, request-id deduplication and the shared ledger.
- Upgrades must retain the private config, account selection, display name, pacing overrides, credentials and send history. Test both an empty installation and a legacy upgrade.
- Do not use UI automation or extract Apple credentials. Do not add automatic account switching after a send restriction.
- The fixture suite must not need real accounts, authentication, Mail access or outbound messages. Run `python3 -m unittest discover -s tests -v` for changes that affect behaviour.
- Obtain the human's approval of recipients, subject, body and attachments before any actual test email. Use their own accounts when authorised, verify receipt, and clean up exact test messages when permitted.
