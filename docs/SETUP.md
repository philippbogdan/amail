# Setup

## Requirements and permissions

- A Mac with Python 3.10 or newer, including SQLite FTS5 support. The current tested machine uses macOS 26.5 and Apple Silicon. Other Mail database versions may need changes.
- Google or Exchange accounts signed into **Apple Mail**, with mail downloaded locally. Add accounts in Mail's account settings first. amail does not create system Internet Accounts or recover their passwords.
- Full Disk Access for the actual invoking host, such as Terminal or your agent's desktop app. Grant it under System Settings > Privacy & Security > Full Disk Access, then restart that host if necessary.
- Automation permission for the host to control Mail, when macOS asks during `amail doctor` or a supported Mail operation. Scripting permission is different from Full Disk Access. No Accessibility or browser automation is required.
- Gmail API operations additionally need `gog`, your own Google OAuth client, and account consent. Exchange uses Mail's existing authorised connection.

Gather these permissions and any required Google sign-ins before a long agent task. If organisational policy blocks an account or permission, respect the restriction and report the actual error. A working Apple Mail connection does not mean another Microsoft app will be permitted.

## Install and choose accounts

With Homebrew:

```sh
brew install philippbogdan/tap/amail
amail setup
```

This downloads a checksummed release from the project's third-party tap. Homebrew manages the CLI and its Python runtime. It does not create accounts, collect credentials, send mail or modify your existing amail state. If Homebrew requests trust, grant it only to this formula. For a manual tap installation, use `brew tap philippbogdan/tap`, then `brew trust --formula philippbogdan/tap/amail` if required by your Homebrew version.

Alternatively, install from source:

```sh
git clone https://github.com/philippbogdan/amail.git
cd amail
python3 install.py
export PATH="$HOME/.local/bin:$PATH"
amail setup
```

Add `~/.local/bin` to your normal shell PATH if it is not already there. The installer creates an immutable release under `~/.local/lib/amail/releases` and atomically switches `~/.local/bin/amail`. It does not modify `/usr/bin/mail` or require Mail access merely to install.

`amail setup` discovers supported Mail accounts. `amail accounts --available` also lists them. Both are read-only. No account is silently enabled on a fresh installation.

```sh
amail setup --accounts you@gmail.com you@company.example --name 'Your Name'
amail accounts
amail config
```

`--accounts` replaces the entire enabled set. Include all accounts you intend to use. It accepts known addresses, aliases and stable account IDs. Setup preserves an existing display name and policy overrides unless you explicitly change them. Leaving the display name empty uses the address alone.

This creates a private `config.json` under `~/Library/Application Support/amail`. Do not put account configuration or OAuth files in the repository.

## Gmail OAuth

Install [gog](https://github.com/openclaw/gogcli) using its [official quickstart](https://gogcli.sh/quickstart.html). The command contract was tested with gog 0.10.0. Later versions should be checked with `gog auth credentials set --help` and `gog auth tokens export --help`.

```sh
brew install openclaw/tap/gogcli
```

Create or choose your own Google Cloud project, enable the Gmail API, configure the OAuth consent screen, and download a Desktop app OAuth client JSON. Follow the official quickstart for the current Google Console steps and testing-mode token expiry rules. Managed Google accounts may need administrator approval. amail does not provide a shared client or ask you to paste secrets into chat.

Register that local file for amail and connect each selected Google account:

```sh
amail setup --accounts you@gmail.com you@company.example \
  --name 'Your Name' --gmail-client-json /path/to/google-oauth-client.json
amail connect you@gmail.com
amail doctor
```

Alternatively, add `--connect` to the setup command to initiate connections for the complete selected account set. Google may open a browser for this explicit setup sign-in. Routine reads and sends do not automate that browser.

amail uses gog's named client `amail`. Existing gog clients are separate. The equivalent credential registration is:

```sh
gog auth credentials set /path/to/google-oauth-client.json --client amail
```

gog stores `credentials-amail.json` in its user config directory and refresh tokens in its configured credential store. See [gog's named-client documentation](https://gogcli.sh/auth-clients.html). amail supports the default macOS directory and an explicit `GOG_HOME` used consistently for gog and amail. The Gmail backend temporarily exports only the requested account's token when it needs an access token, verifies the API identity, and removes the temporary export. It does not print credentials or put tokens on command lines.

If authorisation expires, run `amail connect ADDRESS` again. It requests Gmail services explicitly. Never delete the shared send ledger as an authentication fix: doing so discards duplicate protection and usage history.

## Exchange and Microsoft 365

Connect the account in Apple Mail and allow it to sync. Then select it with `amail setup --accounts ...` and run `amail doctor`.

amail sends using Mail's scripting interface and corroborates the outcome with synchronised server metadata. It does not need a separate Microsoft Graph app registration. Whether this route is allowed depends on your organisation's existing Mail authorisation and macOS permissions. Credentials remain in Apple's account system.

For immediate submission, manually choose **Mail > Settings > Composing > Undo send delay > Off**. This also changes manual Mail sends. With a delay enabled, amail waits for corroboration and may report an unverified outcome before the message leaves Mail. Check `amail status REQUEST_ID`; do not blindly resend.

## Configure pacing

`amail config` shows the file location and current settings. Edit that private JSON file to change policy. Partial overrides are accepted; omitted values retain public defaults. An example with faster Exchange pacing is:

```json
{
  "schema_version": 1,
  "accounts": ["you@gmail.com", "you@company.example"],
  "display_name": "Your Name",
  "policies": {
    "exchange": {
      "recipients_per_day": 100,
      "messages_per_window": 10,
      "window_seconds": 600,
      "gap_min_seconds": 15,
      "gap_max_seconds": 30
    }
  }
}
```

This is a configuration example, not a recommended volume for your account. Provider and tenant rules can be stricter. `amail limits` distinguishes local outreach policy, configured provider ceilings, ledger usage, observed Sent data and unknown restrictions. A random gap never overrides a rolling cap. Existing frozen batch plans refuse a changed account policy until you review a new plan.

## Upgrade and state

For a Homebrew installation:

```sh
brew update
brew upgrade philippbogdan/tap/amail
```

Check `command -v amail` if you also installed from source: the earlier launcher in your PATH may still take precedence. Both versions share the same private config and ledger. Keep one installation method as your normal command. Removing a launcher or uninstalling the Homebrew formula does not require deleting account settings or credentials.

For source installations, run `git pull --ff-only` in a clean checkout, inspect changes as appropriate, then `python3 install.py`. The installer preserves the existing config, send ledger, drafts, snapshots, OAuth cache and gog credentials. It never resets your history.

For the earlier account-specific amail release, the installer detects the old installed `accounts.json`, reads its display-name default and policy constants as data, and writes those settings into the new private config. It does not execute the old code or include those settings in a release directory. Later upgrades keep the private config. The earlier named `amail` Gmail client continues to work.

Paths:

| Item | Default location |
| --- | --- |
| Executable | `~/.local/bin/amail` |
| Versioned code and old executable backups | `~/.local/lib/amail/` |
| Private config, ledger, drafts, assets and access-token cache | `~/Library/Application Support/amail/` |
| Gmail OAuth client file | `~/Library/Application Support/gogcli/credentials-amail.json` |
| Gmail refresh token | gog credential store, client `amail` |
| Mail cache and Exchange credentials | Existing macOS Mail and account storage |

`AMAIL_STATE_DIR` selects another state directory; the earlier `MAIL_STATE_DIR` name remains supported. `AMAIL_CONFIG` can select a separate config file. Use isolated state for fixture work, not to evade sending limits or resend an ambiguous request. `--prefix` changes where code is installed, not the user's state location.

## First checks

```sh
amail doctor
amail accounts
amail mailboxes you@company.example
amail list --account you@company.example --limit 5
amail read REF
```

Start with these read-only checks. Before a real test email, show the human its sender, all recipients, subject, body and attachments and obtain approval. Prefer their own accounts, verify the received content, and clean up the exact test messages afterwards if authorised.
