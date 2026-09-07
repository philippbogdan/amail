# Command reference

Run `amail --help` or append `--help` to any command. JSON is the default. `--json`, `--plain` and `--tsv` work before or after subcommands. `amail --version` prints the release version.

## Read, search and refresh

```sh
amail accounts
amail accounts --available
amail config
amail doctor --account you@gmail.com
amail mailboxes you@gmail.com
amail unread --account you@gmail.com
amail list --account you@gmail.com --mailbox inbox --page --limit 20
amail list --account you@gmail.com --cursor CURSOR --page --limit 20
amail search 'literal phrase' --account you@gmail.com --from person@example.org
amail search 'literal phrase' --to person@example.org --after 2026-01-01 --before 2026-02-01
amail read REF
amail read REF --body-only
amail read REF --fetch
amail thread REF
amail attachments REF
amail attachments REF --out ./attachments --fetch
amail sync --account you@gmail.com
amail index
amail search 'decoded body phrase' --body
```

List and search also support `--subject`, `--unread`, `--since-hours`, `--mailbox` and `--limit`. `--from` is an alias for `--sender`. Date filters use ISO dates or timestamps. Pagination returns `next_cursor`; reuse the same filters on subsequent pages.

Use opaque refs returned by amail. Local `mail:` refs include an account ID and detect recycled cache records. Server `gmail:` refs remain usable while Mail catches up after a move. Full mailbox paths disambiguate repeated names.

Reads do not mark a message read. Unavailable bodies and attachments are explicit; `--fetch` uses the selected authorised provider or Mail route. Attachment extraction does not overwrite names or allow filename traversal. Attached emails are reconstructed as MIME messages with that fact recorded. Binary nested attachments retain their bytes; header folding or line endings may change.

`index` builds a separate decoded-body FTS snapshot. Its initial run can take minutes for a large mailbox; subsequent runs reuse unchanged files. Search reports the index timestamp. `sync` asks Mail to refresh and does not claim completion of all server synchronisation.

## Draft and send

A message file uses this schema:

```json
{
  "from": "you@company.example",
  "to": ["colleague@example.org"],
  "cc": [],
  "bcc": [],
  "subject": "Meeting notes",
  "body_file": "notes.txt",
  "attachments": ["diagram.pdf"]
}
```

Use exactly one of `body` or `body_file`. Paths are relative to the JSON file. Optional `name` overrides the configured display name. Optional `reply_to_ref` preserves a known conversation.

```sh
amail draft create message.json
amail draft list
amail draft show DRAFT_ID
amail draft update DRAFT_ID revised-message.json
amail draft send DRAFT_ID --dry-run
amail draft discard DRAFT_ID
amail send --from you@company.example --to colleague@example.org \
  --subject 'Meeting notes' --body-file notes.txt --attach diagram.pdf --dry-run
```

After human approval of the exact content:

```sh
amail draft send DRAFT_ID
# Or submit directly with a stable idempotency key:
amail send --from you@company.example --to colleague@example.org \
  --subject 'Meeting notes' --body-file notes.txt --request-id meeting-notes-001
```

To, CC and BCC accept plain addresses. `send` also supports `--name`, `--timeout`, `--purpose personal|outreach` and `--cap` for an additional stricter recipient cap. Draft revisions invalidate prior review. Identical request IDs do not send twice; using one with changed content is rejected. `--retry-rejected` is an explicit retry of a definitive rejection, never an ambiguous outcome.

## Reply and forward

```sh
amail reply REF --body 'Thanks, that works.'
amail reply REF --all --body-file reply.txt
amail forward REF --to colleague@example.org --body 'For context.'
```

These produce local drafts. Review them and approve before `draft send`, or use `--send` only when the exact outgoing message is already authorised. `--from` selects another enabled sender. Cross-account replies using the Mail route require the original to be present in Mail's cache.

## Mailbox changes

```sh
amail mark REF read
amail mark REF unread
amail flag REF on
amail flag REF off
amail move REF 'Full/Mailbox Path'
amail archive REF
amail trash REF
amail restore REF
```

Gmail reports server labels. The Mail route checks application state and separately labels pending provider synchronisation. `delete` is a compatibility alias for trash. No permanent purge command is included. Restore uses the saved pre-trash state when available; otherwise it returns to Inbox.

## Batches

A JSONL manifest contains one complete individual message per line. Include a stable `id` for every item:

```json
{"id":"alice-001","from":"you@company.example","to":["alice@example.org"],"subject":"Your paper","body":"Hi Alice,\nA message specifically for you."}
{"id":"bob-001","from":"you@company.example","to":["bob@example.net"],"subject":"Our discussion","body_file":"bodies/bob.txt"}
```

```sh
amail batch plan campaign.jsonl --policy outreach
amail batch show PLAN_ID
# Review the exact frozen messages and obtain approval before this command:
amail batch run PLAN_ID
amail batch status PLAN_ID
amail batch pause PLAN_ID
amail batch resume PLAN_ID
amail batch cancel PLAN_ID
amail batch export PLAN_ID --out result.json
```

One To recipient per outreach item is the default. CC/BCC also consume recipient budget. Planning snapshots attachments and bodies, rejects duplicate IDs and contact attempts, and reports suppressed recipients and timing assumptions. A changed plan requires a new review. Stable logical item IDs prevent accepted unchanged items from being sent again in another plan.

The worker runs in the foreground. Pause it from another terminal or with Ctrl-C. Pending items survive restart. An unresolved submission pauses that account; resuming cannot blindly resend it. Other ready accounts can progress during normal rate waits, but amail never changes a blocked item's sending account. Cancelling affects unsent work, not messages already submitted.

## History, limits and suppression

```sh
amail status REQUEST_ID
amail sent --account you@company.example --hours 24
amail limits
amail policy show
amail policy show exchange
amail suppress add person@example.org --reason 'Explicit opt-out'
amail suppress list
amail suppress remove person@example.org
amail policy release --account you@company.example --reason 'Reviewed the account hold'
```

History includes requested and observed senders, provider identifiers, acceptance evidence and correlated local feedback. A null observed sender is unknown, not proof of a particular From header. `ledger` aliases `sent`; `send-status` aliases `status`.

A hard bounce suppresses the recipient. Two hard bounces among the latest 20 accepted outreach messages pause the account for review. Explicit opt-out commands from known outreach recipients suppress immediately. These local heuristics cannot observe every complaint or spam signal. Releasing an account hold does not remove recipient suppressions.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Command completed, including an intentional pause or status lookup |
| 1 | Invalid configuration, unavailable data, permission or operation error |
| 2 | CLI argument error, rejected/unverified/unknown send outcome, or batch execution paused |
| 3 | Sending must wait; JSON diagnostics include the reason and retry time |
| 130 | Interrupted by the user |

Diagnostics go to stderr. Non-successful send outcomes can also contain a structured result on stdout. Inspect the result state, not only the process exit code. Every provider call is bounded; a foreground batch intentionally spans its scheduled waits until completion, pause or cancellation.
