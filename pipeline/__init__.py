"""The DECISION PATH. `.autopilot.sh` executes these on every cycle.

`core` holds the thresholds and the queue, `verdict` turns a finished encode
into the exit code that authorises deleting a ~90 GB library original,
`next_title` picks what encodes next, and `record` appends the ledger.

Pause the driver before editing anything in here — see CLAUDE.md. Nothing in
this package may import from `dashboard`; `tests/test_layering.py` enforces it,
because the driver must never load the web server to decide on a deletion.
"""
