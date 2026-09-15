# APEX v0.4 — connected edge research and correctness review

Base: `7ca128f9ebd6d4e871973cee6266a6ca952c6875`. This release reviews and improves the **new APEX repository**, including its two pinned reused volatility modules. It does not claim a fresh audit of every legacy APEXAI branch or the current droplet. Existing host captures and release pins remain separate evidence.

## The important architectural gap

Running GARCH and generating a thousand futures does not establish predictive information. The previous live shadow path had one shrunk-mean direction hypothesis and four descriptive replay scores. It had no working multi-session challenger experiment. This release adds that experiment through the same ingestion, Twin, variance simulator and candidate evaluator used by the existing system.

| Layer | Implemented reader/output | Limit |
|---|---|---|
| Reality | Saved APEX_DATA_V1 bars/quotes; existing bounded Alpaca capture | Historical bar completion is an assumption when so labelled; finalized revisions not recoverable |
| Twin | Actual as-of state and contiguous returns | Six research features; weekday clock, no exchange holiday calendar |
| Direction | Zero drift, shrunk empirical mean, training-only standardized ridge | Fixed simple hypotheses; no market edge assumed |
| Conditional variance | GARCH11_T, named EWMA fallback | Fitted likelihood is distinct from bounded simulation tails |
| Multiverse | Common shocks across models, default 1,000 15-minute paths | No parameter uncertainty, jumps or options-implied surface |
| Expression | Current-quote funded stock versus WAIT | Independent hypothetical opportunities; no portfolio ledger in research |
| Experience | First-available target labels, forecast-before-outcome persistence | Bar-close scoring; no invented quote or realized trade |
| Challenger comparison | Development selection frozen before holdout; paired scores | Descriptive evidence only, no automatic promotion or Kelly sizing |
| Audit | Reconstructed features, labels, fits, candidates, scores and selection | Internal consistency, not authenticated provider history or profitability |

Premarket agents, TradingView charts, options, cross-asset fusion, PRIME and broker paper execution are not secretly present in this route. Those components need measured incremental value and lifecycle commissioning before joining it.

## Research plan

`apex research --input data/input.json --plan data/plan.json --out runs/new-id`

The plan is JSON, with explicit UTC epoch-second boundaries `start`, `development_start`, `holdout_start`, `end`. Boundaries must occupy separate sessions in chronological order; development/holdout boundaries must align to the scan grid. Optional frozen defaults:

```json
{
  "scan_minutes": 30,
  "decision_delay_seconds": 5.0,
  "embargo_minutes": 1,
  "minimum_training_labels": 40,
  "training_window": 260,
  "minimum_comparison_sessions": 3,
  "ridge_alpha": 10.0
}
```

Add the four required epoch boundaries to that object. The default 15-minute targets do not overlap on the 30-minute grid. Decisions occur five seconds after each grid minute, allowing a completed bar's measured receipt to arrive; later delivery is refused at that scan. Decision time, bar-price origin, target time and label availability are separately preserved. Forecasts never cross the regular-session boundary. Sparse/missing/conflicting bars remain explicit.

Features are `ret_1`, `ret_5`, `ret_15`, current-session `rv_30`, log spot/VWAP-proxy distance, and session fraction. Missing features refuse; no zero imputation. Ridge standardization uses its training set only. Training labels must have both a target and availability at/before the embargoed cutoff. In holdout, the supervised training cutoff is fixed before the holdout begins. Variance may still update from then-known unlabelled returns, an explicit operational assumption.

All three hypotheses are compared on the **same matured sample IDs**. Development mean CRPS selects a model; ties favor zero drift. Too few paired sessions selects the zero baseline with a named reason. The frozen selection includes the precise development evidence available then. Later-arriving development labels may appear in final descriptive development metrics but cannot rewrite that choice. Holdout never selects or retrains the ridge.

Scores include empirical CRPS (checked against its pairwise definition), Brier, three quantile losses, 90% interval coverage/width, reliability bins and whole-session paired CRPS differences versus zero. No IID confidence interval, significance, multiple-testing adjustment or calibrated probability is inferred. Candidate counts are not trade counts; missing quotes produce WAIT while forecasting/scoring can continue.

## Correctness repairs found during review

1. **False rebound from a stale price.** Candidate prices used a bar mark while entry cost used a newer quote. The same return paths could look profitable simply because price had already fallen. Candidate pricing now starts from the current quote midpoint; the unchanged H-minute return-law rebasing assumption is explicit. Bar-origin forecast labels remain separate from quote-origin execution targets.
2. **Fractional decisions could never mature.** Targets were `decision_time + H`, but target labels exist on completed minute bars. Targets now bind to the bar-price origin. The actual decision timestamp is preserved.
3. **Undefined arithmetic expectation.** Unbounded Student-t log innovations do not have a finite exponential moment. Simulation now continuously truncates standardized shocks at eight, exactly renormalizes their conditional variance, and bounds rejection sampling. Both GARCH and recursive EWMA use that explicit law. It changes extreme-tail behavior and does not certify a real-world loss limit. Cap sensitivity remains unestablished.
4. **A noisy null benchmark.** The known symmetric zero-drift directional baseline is 0.5; sampled frequencies no longer substitute for its analytic directional probability.
5. **Verification trusted disconnected claims.** Rehashed incompatible candidates, altered Twin fields and false summary scores could pass. The verifier now reconstructs actual as-of inputs, model path generation from saved parameters, candidate behavior at reconstructed cash/exposure, quote binding, label scores and summaries. It does not independently refit the variance optimizer.
6. **Torn runtime evidence crashed reporting.** Corrupt or incomplete terminal records now retain their original bytes and report unavailable evidence. New terminal records publish atomically; recovery does not invent a successful completion.
7. **Malformed provenance could abort ingestion.** An unhashable availability label now refuses that row while valid rows survive.

The new research implementation also caught and repaired its own exact-minute receipt starvation, delayed-development display/selection ambiguity, off-grid overlapping phase boundary and extreme-ridge numeric overflow. An unsupported ridge output is refused while baseline forecasts continue; it is not clipped into eligibility.

## Verification and operation

`apex verify-research --run runs/new-id` reads the captured input and ledger. It reconstructs chronological events, features, labels, training membership, ridge fits, saved common-shock variants, candidates, metrics and the frozen selection. Raw/synthetic data and full path artifacts stay outside Git. Small reports and test evidence are retained with this release.

Previous captures must be verified with their retained release code. This verifier expects the new origin/simulation contract; compatibility with old artifacts is not claimed. Updating code is not deploying it. The shadow timer, broker state and old APEXAI services are not changed by this release.

## Executed results — September 15, 2026

Final combined suite: **168 passed**, no failures or skips. The real CLI lifecycle (GARCH), accounting reconstruction, synthetic capture/replay, three synthetic tournament worlds and market tournament/reconstruction all exited zero. [Test results](evidence/edge-research-001/tests.xml).

**Market experiment:** one Massive connector request returned 11,982 SPY minute bars for August 24–September 11, including 5,460 regular-session bars across 14 dates. All normalized without rejection. Warmup began August 24; development August 31; holdout September 4; evaluation ended September 11. Settings were fixed before evaluation. [Plan](evidence/edge-research-001/market-plan.json), [source scope](evidence/edge-research-001/market-data-scope.json), [results and reconstruction](evidence/edge-research-001/market.json).

The real path produced 99 forecast points and 297 model scores. GARCH11_T fitted and supplied every one of the 99 base simulations. There were 44 paired development observations and 55 paired holdout observations. The learned ridge lost to zero drift in development, so **ZERO_DRIFT was selected and remained selected**. Ridge subsequently had 3.57% lower empirical CRPS than zero on the holdout, but that inspected improvement does not authorize replacing the original selection or claiming a profitable edge. A genuinely new evaluation is required. No executable quotes were supplied: all 297 candidate observations were WAIT; no trades or trading P&L exist for this experiment.

| Control | Development selection | Holdout finding | Meaning |
|---|---|---|---|
| Engineered persistence | Ridge | Lower CRPS than zero; 90% interval coverage only 40.9% | Learns a relationship, but volatility coverage is inadequate |
| Independent zero-mean noise | Shrunk mean | Slightly worse CRPS than zero | Development selection can choose noise; no automatic promotion |
| Persistence then reversal | Ridge | Materially worse CRPS than zero | A changed relationship invalidates the learned advantage |

The persistence control requested GARCH, which refused and used EWMA on these autoregressive synthetic returns. That fallback is reported; the separate market run used GARCH in all 99 fits. Synthetic success here means a faithful test and reconstruction, not a calibrated or profitable model. [Persistence](evidence/edge-research-001/persistent.json), [null](evidence/edge-research-001/null.json), [reversal](evidence/edge-research-001/reversal.json).

Captured data and complete binary paths are retained outside Git with checksums. The evidence archive SHA-256 is `c611054885b2a6ffc0d8fdbb9d079b167b5c90e80386ddec4729b0d191a134a5`. The archive contains market input, full runs, captured provenance and CLI results; code remains in this repository.

## Next work

1. Finish the actual host capture commissioning, including endpoint-specific quote-size semantics. Keep its provenance and models visible.
2. Run this frozen experiment on enough retained market sessions; inspect null and regime-break behavior, uncertainty coverage, after-cost execution sensitivity and missing-data selection effects. Preserve failed hypotheses. More model complexity earns inclusion by beating the same baseline on genuinely new data.
3. Add one increment of causal information (for example relative strength versus market/sector or a timestamped catalyst feature), then run an ablation on the same experiment contract. This is the next plausible information advantage; a larger simulation alone cannot create it.
4. Commission persistent paper positions, exits, broker/account constraints and independent P&L before treating hypothetical candidates as trades. Allocate by uncertainty and survival constraints only after predictive evidence exists.

Primary methodological reference: training preprocessing must not learn from held-out data, and repeated model tuning can leak test information; see [scikit-learn's cross-validation guidance](https://scikit-learn.org/stable/modules/cross_validation.html). The implementation uses NumPy ridge algebra, not scikit-learn. No citation establishes edge for APEX.
