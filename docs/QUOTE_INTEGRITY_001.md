# Historical quote integrity repair

This integrates the source and tests from Claude's `b3ec2d7f0f73de8e27345c18e36c53361570dab4` onto the current APEX paper-runtime and continuation work, then repairs the reproduced review failures. The upstream branch diverged before v0.7: it was six commits behind the published `bdeb3560f4a17c23565a390ddeb4c12a869aeb2c` base. Its absence of `paper_runtime.py` was a checkout difference. The existing live-paper measured-receipt gate remains in place.

## Corrected behavior

- Historical quote availability remains an explicit one-second assumption. The fetcher compares provider timestamps against the decision cutoff in integer nanoseconds. Normalization verifies exact latency and consistency of the derived seconds. The reader independently checks integer availability and groups by integer event time, so bypassing the fetcher cannot admit a one-nanosecond future quote. Floating-point seconds remain in the broader engine; this does not claim nanosecond precision for every scheduler or account operation.
- The fetcher retains the complete final nanosecond timestamp group. Distinct nanosecond events no longer become a fabricated conflict through float aliasing. Same-timestamp disagreeing records still refuse. A group filling the page is quarantined, and any truncated group, rejected row or failed request makes the collection INCOMPLETE. Normalization, admission and merge readers refuse incomplete collections, including nested provenance. Aborted collections retain a failure marker; they are not completion evidence.
- Admission uses explicit evidence categories. Recorded observations cannot qualify as synthetic controls, and synthetic/recorded mixtures refuse even when component metadata is absent. Raw observation bases and nested provenance participate in classification so normalization cannot silently remove the only evidence of a mixture.
- The execution replay engine enforces admission before creating a ledger or forecasting. Research and Director input readers enforce their appropriate category. Strategy Lab runs through the guarded research reader. The existing paper runtime retains its own commissioning/source checks and rejects mixed provenance.
- Decision-only evidence is explicitly labeled in the fetched document and rows and remains visible through merges. Both execution replay and the paper runtime refuse it. Research summaries distinguish hypothetical candidates from trades and show WAIT reasons. A sparse quote input cannot supply account P&L, fills, path-dependent exits or stop ordering.
- Historical quote sizes are labeled operator-declared and unverified. Distribution magnitudes do not establish units. The candidate evaluator retains a conservative raw-provider-size ceiling, including compatibility with the old raw-lot field names. Invalid raw sizes refuse normalization. This does not independently establish displayed liquidity, queue position, execution capacity or provider units. Actual provider-size verification remains an operational dependency.
- Transport diagnostics use fixed certificate/DNS/refusal/timeout/generic codes. TLS verification and credential protection remain intact.

The ingestion timestamp parser is shared with capture. Live-capture rows now retain the same integer event and availability fields, while still preserving their provider timestamp and measured parent-receipt metadata. No historical quote is relabeled as measured receipt.

## Regression evidence

`tests/test_quote_integrity.py` directly exercises the real fetcher, normalizer, quote reader, admission reader, merge and execution engine. Cases cover a valid one-second latency ending one nanosecond after a decision; exact-cutoff acceptance; distinct timestamps sharing one float representation; truncated-page quarantine; incomplete nested components; recorded/mixed synthetic classification; refused inputs producing no execution ledger; and decision snapshots being ineligible for execution.

The first broad run exposed an older lifecycle fixture that labeled a simulated stale quote as MEASURED_RECEIPT among synthetic rows. That now correctly refuses. Its label was corrected to SYNTHETIC_CLOCK, preserving the late-availability and stale-event assertions. The original failed run remains retained and is not acceptance evidence. A subsequent full run exposed two older research-test expectations: an exact summary dictionary without the new WAIT fields, and another simulated-delivery fixture claiming measured receipts. Those expectations were corrected without changing application code. A final complete suite run covers the corrected tests; prior failed/interrupted logs remain retained.

The real CLI `fetch-decision-quotes` is also exercised with an explicitly nonexistent credential directory. This is a deliberate no-auth control: no credential contents are accessed and no successful market acquisition is claimed. Every planned request must remain accounted as failed, the collection incomplete and the command nonzero. Synthetic demo and paper account controls remain explicitly synthetic.

Final acceptance results and source hashes are retained in `docs/evidence/quote-integrity-001/`. These tests establish bounded implementation and accounting behavior, not a market edge. Claude's 195-request real capture has not been re-fetched or independently reconstructed here; its old summary is not acceptance of this repaired version. Re-run the repaired importer/collector on the credentialed host with preserved raw evidence before claiming real-data commissioning.

## Commands

```sh
apex fetch-decision-quotes --plan docs/evidence/edge-research-001/market-plan.json --out runs/NEW_CAPTURE --feed sip --round-lot-shares 1
apex merge-inputs --input data/bars.json --input runs/NEW_CAPTURE/quotes.json --out data/NEW_MERGED.json --retrieved-utc TIMESTAMP_WITH_OFFSET
```

The multiplier is an explicit unverified declaration, not a provider-unit recommendation. Incomplete captures exit 3 and cannot be merged into an eligible study. Use unique output paths and retain raw licensed captures outside Git. A successful input repair does not activate the AI brain, commission the always-on feed, or authorize real-money trading.

Final acceptance: **506 tests passed in 580.51 seconds** on the retained source manifest. The actual CLI replay/demo verifier and synthetic paper-account verifier both returned VALID. The deliberate 195-request no-auth control remained INCOMPLETE and exited 3. Application source and tests matched the retained manifest after the full suite completed.
