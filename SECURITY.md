# Security and privacy

amail can read sensitive email and submit messages as the signed-in user. Full Disk Access and Automation permission apply to the calling terminal or agent host, not only to this script. The CLI does not implement a separate human-approval security boundary; the user and agent must authorise outbound actions.

Local cache databases are opened read-only. Gmail requests use OAuth, and Exchange stays behind Apple's existing account integration. amail does not collect telemetry or run a hosted backend. Google, the email provider and the configured credential store still process their normal requests.

Private config, drafts, attachment snapshots, feedback, usage history and short-lived access tokens live outside the repository. Gmail refresh tokens stay in gog's configured credential store, with a temporary private export during renewal. Protect the Mac account and its backups. Tokens should never appear in command arguments, diagnostics or issues.

Report a suspected vulnerability through this repository's GitHub private vulnerability reporting feature. Include synthetic reproduction steps and redacted logs. If private reporting is unavailable, open an issue asking for a private contact without disclosing secrets or exploit details. Do not publish mailbox files or credentials.
