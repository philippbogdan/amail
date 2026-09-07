# amail

**A fast, local email CLI for agents on a Mac.** Read mail from Apple Mail's local cache, send through the Gmail API or an existing Exchange account, and return structured JSON. Routine commands do not click buttons or open compose windows.

amail is for **macOS users who already have Gmail or Microsoft 365 / Exchange connected to Apple Mail**, and want a coding agent or terminal script to work with those accounts. It is particularly useful when a work or university mailbox is authorised in Apple Mail but a separate API integration is unavailable. It uses that existing permission; it does not bypass an organisation's access controls.

This is an early open-source release. It has been tested on Apple Silicon with macOS 26.5, Python 3.14, two personal Gmail accounts and an institutional Exchange account. Other macOS versions and managed configurations need validation. It is not a Linux mail client, a hosted email service, or a bulk-mail delivery platform.

## Give it to your agent

Copy this prompt:

> Set up amail from https://github.com/philippbogdan/amail on my Mac. Read README.md and docs/SETUP.md first. Discover my available Apple Mail accounts and let me choose which to enable. Keep my credentials and configuration outside the repository. Show me the recipients, subject, body and attachments before sending any email, and wait for approval. Begin with read-only checks.

For agents that load skills, the repository includes [skills/amail/SKILL.md](skills/amail/SKILL.md). It is portable to tools that can run shell commands and read local files; no particular model, subscription or agent framework is required.

## Quick start

You need macOS, Python 3.10 or newer, and the selected accounts already signed into Apple Mail. The terminal or agent host needs Full Disk Access. Mail scripting also requires Automation permission. Gmail's API route needs your own Google OAuth client and [gog](https://github.com/openclaw/gogcli).

```sh
git clone https://github.com/philippbogdan/amail.git
cd amail
python3 install.py
export PATH="$HOME/.local/bin:$PATH"
amail setup
```

The last command lists supported accounts without enabling them or sending mail. Select yours explicitly:

```sh
amail setup --accounts you@gmail.com you@company.example --name 'Your Name'
```

For Gmail, register your downloaded OAuth client JSON and authorise your account:

```sh
amail setup --accounts you@gmail.com you@company.example \
  --name 'Your Name' --gmail-client-json /path/to/google-oauth-client.json
amail connect you@gmail.com
```

`--accounts` sets the complete enabled account list, so include every account you want to keep. Exchange uses the account already signed into Mail. There is no shared OAuth client, supplied mailbox, password extraction, or bundled credential. [The setup guide](docs/SETUP.md) explains the one-time permissions and Google setup.

```sh
amail doctor
amail list --account you@company.example --mailbox inbox --limit 10
amail read REF
amail attachments REF
amail search 'project' --from colleague@example.org
```

Use a `ref` returned by amail. JSON is the default; `--plain` and `--tsv` are also available. Installation uses `~/.local/bin/amail`, preserves prior amail settings and state, and leaves `/usr/bin/mail` alone. No sudo is required.

## What it does

| Area | Commands |
| --- | --- |
| Setup and diagnostics | `setup`, `accounts`, `config`, `connect`, `doctor`, `mailboxes` |
| Read and search | `list`, `unread`, `read`, `thread`, `search`, `attachments`, `index`, `sync` |
| Compose and conversations | `draft create/show/update/list/discard/send`, `send`, `reply`, `forward` |
| Mailbox changes | `mark`, `flag`, `move`, `archive`, `trash`, `restore` |
| Reviewed batches | `batch plan/show/run/status/pause/resume/cancel/export` |
| History and policy | `sent`, `status`, `limits`, `policy`, `suppress` |

[Command reference and examples](docs/COMMANDS.md)

Preview an individual message without sending:

```sh
amail send --from you@company.example --to colleague@example.org \
  --subject 'Meeting notes' --body-file notes.txt --dry-run
```

After reviewing the exact draft and obtaining approval, submit using a stable `--request-id`. Replies and forwards create local drafts unless `--send` is specified. A reviewed batch freezes each recipient's body, subject and attachment bytes before execution. Sending approval belongs to the human and agent workflow; the CLI itself is a capable sending tool, not an approval sandbox.

## How it works

```text
                    amail CLI
                       |
             +---------+---------+
             |                   |
          Fast reads         Sends and changes
             |                   |
      Read-only SQLite      +----+----------------+
      and MIME files        |                     |
             |          Gmail API          Mail scripting
      Apple Mail cache      |                     |
                        Google OAuth      Signed-in Exchange
```

Local reads do not mark messages read. The reader understands Gmail label membership, decoded MIME bodies, HTML, Unicode and separately cached attachments. Missing content is explicit. `amail index` builds a separate body-search snapshot; `amail sync` requests a Mail refresh. Neither implies that every server message is cached.

Gmail acceptance is backed by an API response containing a server message ID. Exchange acceptance is corroborated against a newly synchronised Sent item or a received copy. A successful script call alone is insufficient. Results distinguish `accepted`, `rejected`, `provider_acceptance_unverified` and `outcome_unknown`; ambiguous submissions are never retried automatically.

On the development Mac, small cached reads were typically under 100 ms, and final small-send checks took about 1.0 to 1.5 seconds, including acceptance observation. These are measurements from a small sample, not latency guarantees. Exchange's Mail Undo Send delay must be set to Off for immediate submission; amail reports the setting but does not change it. [Validation and limitations](docs/VALIDATION.md)

## Credentials, privacy and sending limits

Configuration and state live in `~/Library/Application Support/amail`, outside the checkout. Gmail refresh tokens stay in gog's credential store. amail temporarily exports the selected refresh token into a private directory when refreshing access, then removes the export. Short-lived access tokens are cached locally with private permissions. Exchange credentials remain with Apple Mail. amail has no hosted backend or telemetry.

New installations use an illustrative conservative outreach policy of **50 recipients per rolling 24 hours, six messages per rolling hour, and a minimum ten-minute gap per account**, for both Gmail and Exchange. These are local defaults, not provider quotas or a guarantee against filtering. You can configure separate provider profiles in your private config. Existing installations retain their configured limits, including customised Exchange pacing.

All routes share a transaction ledger, account-level concurrency control and configured ceilings. Aliases share the underlying account's budget. Normal personal messages are separate from outreach pacing but still respect configured provider ceilings. Suppression, correlated bounces and explicit opt-outs can stop later outreach. Usage from other clients and tenant restrictions remains partially observable.

## Development

```sh
python3 -m unittest discover -s tests -v
```

The standard-library tests use synthetic SQLite and MIME fixtures, mocked provider failures, concurrent workers and real process-crash recovery. They do not read your mailbox or send mail. CI runs fixture tests on macOS and Linux; Linux CI does not establish runtime support. See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

MIT licensed. Unaffiliated with Apple, Google, Microsoft or any agent vendor.
