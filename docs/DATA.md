# APEX_DATA_V1

The input is a JSON object with `schema: APEX_DATA_V1`, `source` and `observations`. Source labels are claims, not independently authenticated provenance.

Every observation has `kind`, `symbol`, `event_epoch`, `available_epoch`, and `availability_basis`. Times are UTC Unix seconds. Bars additionally carry `open`, `high`, `low`, `close`, `volume`; quote rows carry `bid`, `ask`, `bid_size`, `ask_size`. Quote sizes are **shares**, not lots. An adapter must explicitly convert provider units before writing this contract.

- Bar `event_epoch` is the minute start. Availability cannot precede minute completion.
- `MEASURED_RECEIPT` requires a real capture adapter's receipt in future operations; the JSON label alone authenticates nothing.
- `SYNTHETIC_CLOCK` is for fixtures.
- `BAR_COMPLETION_ASSUMPTION_V1` is permitted for retrospective bars only, and must compute exactly `event_epoch + 60`. It is not valid for quotes.
- A quote's availability cannot precede its event. Both must precede its execution use; freshness is measured from the quote event, not its receipt.

Exact duplicates collapse. Equal market values with different provenance retain the earliest available observation as the representative. All original rows remain in the captured document. Conflicting values at a timestamp are excluded once both have become available, with their identities recorded by the as-of query. A future conflicting observation cannot invalidate an earlier decision.

This release supports regular US-equity weekday clock windows, not an exchange holiday/early-close calendar. No overnight return is constructed. Missing minutes break adjacency; they are not forward-filled. Corporate actions and adjusted-history selection are not modeled. A production adapter must supply a session calendar and corporate-action contract before deployment.

Every run captures input bytes once and records their SHA-256, executable source hashes, numeric-library versions, configuration, saved path arrays and a chronological ledger. `verify` binds these artifacts and independently reconstructs accounting. It does not claim to rerun and independently derive every model parameter, prove authentic vendor history, or defend against an attacker able to replace every artifact and retained digest.

Output directories are exclusive. Failures after claiming a directory leave `FAILED.json`. Interrupted processes can leave partial ledgers; they are not resumable live sessions. Successful runs write `COMPLETE`; that marker means the replay ended, while the session may still carry an outstanding position and null total P&L.

## Historical quote repair

`QUOTE_LATENCY_ASSUMPTION_V1` means an exact declared event + 1 second, checked with integer `event_ns` and `available_ns`; it is not a measured receipt. The reader uses integer nanoseconds for visibility and event grouping. Incomplete collections are refused, including through merges. Decision snapshots are research-only evidence and cannot enter the execution replay or paper account. Quote-size conversion remains operator-declared and unverified; see [QUOTE_INTEGRITY_001.md](QUOTE_INTEGRITY_001.md).
