# Quote review reconciliation

PR #5 reapplies the quote brick onto the current paper-runtime line; it does not descend from Claude's branch. Initial import: b3ec2d7f0f73de8e27345c18e36c53361570dab4. Reviewed superseding pin: 004732b7cc8401e022c1ee768a18387ec4a96f0d. Its preceding 818530f commit fixes admission sets and the WAIT histogram assertion; both behaviors were independently fixed in PR #5. The 004732b commit itself adds test evidence only. No original acceptance record is rewritten to claim that later pin was the initial import.

## Recovered evidence

The three JSON summaries in `evidence/quote-integrity-001/upstream/` are preserved byte-for-byte from 004732b. They contain upstream measurements and conclusions, not independent verification by this integration:

- `quote-size-units.json`: 28,000 bid/ask size observations and 12,393 trades. Useful support for a historical SPY/SIP multiplier of 1, but displayed depth need not equal executed trade size. The ratio alone cannot prove units; conversion remains unverified.
- `quote-window-selection.json`: eight sampled windows report 359 same-nanosecond pairs, 329 disagreeing, zero distinct-nanosecond float aliases. Its broader window table has a maximum group size of **5**, correcting the imported comment's 3. Limit 50 is a bounded collection budget, not an empirical upper bound; reaching it quarantines the group.
- `market-quotes-collection.json`: 195 requests, 182 answered, 13 empty, 183 accepted observations. Original raw response bodies remain outside Git and were not recovered here.

The upstream normalized quote document was recovered to ignored `data/upstream-quotes/market-quotes.json` and checked with the current normalizer and quote reader. It reproduced **181 usable decisions, 1 conflict, 13 stale refusals**, with 183 accepted rows and no rejected rows. Its SHA-256 matches the upstream collection summary. See `evidence/quote-integrity-001/recovered-reader-check.json`. This verifies consumer compatibility with recovered historical data; it is not a transport reconstruction or a fresh fetcher commissioning run. Upstream VERIFIED size labels are retained in that original document as claims, not newly endorsed.

## Follow-up repairs

The future-leak guard now uses QUOTE_LATENCY_NS. Quote age uses a duration conversion with an explicitly conservative rounding rule. The admission lookup defaults to an empty set for an unsupported ceiling, while existing LIVE_PAPER and REAL_MONEY requests remain named refusals.

Integer nanosecond stamps remain authoritative. A seconds-only fractional float has already lost its original precision: merely changing floor to ceil cannot recover it. Availability fallback now uses the next representable float as an upper bound and ceilings that to nanoseconds; the decision cutoff still floors. This can defer a seconds-only row near the boundary rather than make it visible early. Exact declared integer clocks remain exact. Event-time fallback remains the declared decimal clock and is not evidence of original provider nanosecond ordering. Real adapters should retain integer stamps for both event and availability.

The capture document now corrects the disputed unit paragraph in place. A synthetic multiplier-100 fixture is arithmetic coverage, not a live-provider unit claim. No risk limits or trade thresholds were relaxed.

## Acceptance

511 tests passed in 673.26 seconds; exit 0. Focused checks: 40 passed. Actual CLI demo and independent verify passed. Source/test hashes remained unchanged through the run. Separate review-002 acceptance, manifest, and pytest log are retained beside the original acceptance evidence.
