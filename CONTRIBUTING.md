# Contributing

Bug reports should include the amail version, macOS version, Python version, the command shape and a redacted error. Do not attach real mailbox databases, email contents, account IDs, access tokens, OAuth files or unredacted `doctor` output.

The runtime uses Python's standard library, gog for Gmail authorisation, and macOS Mail scripting for the native route. There is no daemon or build step. See AGENTS.md for invariants and docs/COMMANDS.md for the interface.

Run `python3 -m unittest discover -s tests -v`. New tests should use synthetic SQLite/MIME fixtures and mock network or Mail operations. Check changes to installation with an isolated prefix and `AMAIL_STATE_DIR`. The tests must never send mail. macOS-specific source can also be syntax checked with `osacompile`.

When changing Mail schema handling, report the actual macOS version tested and keep missing data explicit. For live integration tests, obtain approval first and publish only sanitised aggregates. Keep compatibility claims narrower than the evidence.
