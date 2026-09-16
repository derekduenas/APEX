# APEX v0.5 — regime-aware strategy laboratory

This release turns the forecast laboratory into a strategy laboratory. It asks what state the market is in, which setups are present, how different trading rules behave on the same possible futures, and what actually happened afterward. It does not turn simulated win frequencies into probabilities of real profit.

The current strategy family contains WAIT, funded long hold, momentum breakout, trend pullback and VWAP reversion. It is a fixed, visible set of research hypotheses; parameters were not searched until one won on the evaluation data. Stocks remain the funded expression in this release. Shorting, options, account-aware portfolio allocation and broker execution are separate work.

## The connected flow

| Layer | Actual calculation and downstream use |
|---|---|
| Data | The same APEX ingestion/as-of gate reads bars, quotes and their declared availability. |
| Premarket context | Observed prior closing bar, premarket high/low/volume, opening gap and source age feed gap-continuation setup eligibility. Missing news/calendar remain missing. |
| Regime | Trend efficiency, realized volatility, same-clock relative volume/range and quote spread produce descriptive state and setup flags. The state changes historical-neighbor distances; flags change simulated entry eligibility. |
| World model | The existing chronological zero/shrunk/ridge tournament selects a forecast from previously matured development results; selection and supervised training freeze before holdout. GARCH or a named EWMA fallback supplies innovations. |
| Historical analogues | Training-only standardized features plus a declared regime mismatch penalty retrieve prior states whose entire future paths had already matured. Whole paths are resampled without stitching or volatility rescaling. |
| Strategy simulation | Every strategy consumes the same paths within a world. Entry, stop, target, fees, funding and horizon are evaluated at actual minute-grid prices. |
| Stress comparison | Nine named worlds examine model disagreement, cost, volatility, drift timing and an adverse jump. Seven enter the conservative modeled-growth ranking. |
| Research selector | Maximize the minimum modeled mean log growth across those seven worlds, with WAIT at zero, adequate analogue support, a past-coverage alarm and a declared adverse-tail-loss filter. |
| Experience | Once every bar is available, reconstruct the actual future price path and all five counterfactual strategy outcomes. Preserve no-entry/WAIT opportunities and unresolved paths. |
| Evidence | Equal-session bootstrap comparisons for the complete fixed strategy family, including missing-data refusal and a ten-session minimum. Separate adaptive-selector performance remains descriptive. |
| Verification | Rebuild the exact chronology, inputs, regime, analogue membership, paths, stresses, executions, choices, feedback and summary from persisted artifacts. Numerical primitive tests use independently calculated expectations. |

The existing capture/shadow observer also calls the new regime reader and produces a **single-model strategy preview**. That is the reachable live-shaped bridge. It is explicitly not the full historical-analogue laboratory, a calibrated strategy admission, or a broker order.

## What the nine worlds test

1. Selected conditional forecast.
2. Historical analogue paths.
3. Conditional forecast with 1.5x centered volatility.
4. Conditional forecast with 3x assumed costs.
5. Analogue paths with 3x assumed costs.
6. Front-loaded drift: square-root time schedule.
7. Back-loaded drift: squared time schedule.
8. Adverse jump after the first permitted entry: three terminal-return standard deviations.
9. Zero-drift reference.

The first seven drive the minimum-mean-log-growth comparison. The jump has a separate research CVaR filter; the zero-drift world is a visible reference. These are named scenarios, not a probability-weighted partition of reality. Actual configured multipliers are preserved in each record. The jump is not a bound on possible market losses.

The front/back timing worlds have identical terminal returns. This matters because a model predicting the final return has not learned the timing of a breakout or stop. Tests demonstrate different strategy payoffs despite identical terminal values.

## Timing, capital and prices

A completed bar origin and a later decision instant remain separate. With the normal five-second delivery allowance, no strategy enters at the preceding close: its earliest hypothetical entry is the next minute grid. A delayed trigger shortens the remaining holding period; it cannot extend the original target.

Quantity is calculated once from the decision's reference price and base assumed costs, then preserved across stresses and realized-path checks. If a later trigger costs too much, that path records an unfunded skip; it does not shrink the order using hindsight. Stops execute at the observed grid price, including overshoots. Intraminute stop/target ordering is not inferred from OHLC data.

Historical research economics use a bar-reference price and declared spread/slippage/fee assumptions. An available quote is separately recorded as unused in those economics. No quote being present can silently certify a historical counterfactual as executable. Proper quote revaluation and paper-broker commissioning are still required.

## Evidence and learning limits

One thousand resampled analogue paths do not become one thousand historical examples. Neighbors, distinct sessions, effective weight count, distances, unseen regimes and out-of-support states are recorded. There is no fitted transition probability hidden inside a descriptive regime label.

Coverage feedback uses only already matured paths. A predeclared low-coverage alarm can stop the next research opportunity. It is a misspecification diagnostic, not a conditional calibration guarantee. Forecasts continue to be evaluated while trading hypotheses wait, so the monitor does not learn only from chosen trades.

The bootstrap jointly resamples complete session vectors, preserves within-session dependence and adjusts the maximum-mean comparison for the five registered strategies. It explicitly assumes independent stationary sessions, has no inferential output below ten sessions, and does not control earlier experiments or repeated looks. It is not a full stationary-bootstrap implementation of White's Reality Check or Hansen's SPA. The adaptive selector's descriptive mean is not covered by that fixed-family test.

Returns are opportunity-level counterfactual returns on fixed illustrative research capital. They are not a compounded portfolio equity curve, fills, broker fees or actual P&L. Nothing here supplies Kelly sizing or live capital authorization.

## Run the actual path

```sh
apex strategy-demo --world positive --out runs/strategy-positive
apex verify-strategy-lab --run runs/strategy-positive
apex strategy-demo --world persistent --out runs/strategy-persistent
apex verify-strategy-lab --run runs/strategy-persistent
apex strategy-demo --world null --out runs/strategy-null
apex strategy-demo --world reversal --out runs/strategy-reversal
apex strategy-lab --input data/recording.json --plan data/frozen-plan.json --out runs/strategy-market
```

The plan schema is the existing `ResearchPlan`. Each directory is created exclusively; a failed run cannot be replaced by a successful one under the same name. `forecast/` contains the real upstream research run and full paths, while the laboratory ledger binds the actual downstream results.

## Review findings addressed in this build

- Cost stresses originally recomputed share quantity: all worlds now preserve the proposed order.
- A HOLD at the bar origin would have entered before the delayed signal existed: first permitted grid entry is now explicit.
- A jump before that delayed entry failed to stress an open HOLD: it now lands after the first permitted entry.
- Terminal-model accuracy did not test barrier timing: front/back drift worlds now challenge that assumption.
- Undefined log growth under capital exhaustion needed a named refusal rather than an exception.
- Forecast-refused WAIT outcomes were initially excluded from a selected-policy denominator: all completed held-out opportunities now remain in it.

## Why this mathematics

The system uses conditional stochastic paths, nearest-state empirical retrieval, first-passage strategy evaluation, log-growth ranking, tail-loss diagnostics and selection-aware session resampling because each addresses a specific failure mode. Calling an algorithm physics-inspired would add no evidence.

Repeated backtest searches create false discoveries; all hypotheses and dataset reuse must remain visible. [Bailey et al.](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf). The joint centered maximum comparison is inspired by [White (2000)](https://users.ssc.wisc.edu/~bhansen/718/White2000.pdf), with the narrower session-IID adaptation stated above. Time-series uncertainty under changing conditions motivates separate coverage diagnostics; no conformal guarantee is claimed. [Zaffran et al.](https://proceedings.mlr.press/v162/zaffran22a.html).

## Next intelligence that must earn its place

The next high-value inputs are cross-sectional relative strength, catalyst reaction and executable microstructure, followed by an ablation showing whether each adds out-of-sample after-cost value. News summarization, TradingView imagery and more complex models should enter as testable inputs or challengers, rather than receiving authority from impressive language or visual patterns.

## Completion failure found on the longer market evaluation

The first June–July SPY run at `c96147c` returned successfully and wrote a
`COMPLETE` marker, but its retained forecast ledger contained only 685 records,
ending at a June 26 `RESEARCH_SAMPLE`. All 1,276 forecast array files existed.
The separate verifier refused `RESEARCH_RUN_LIFECYCLE_INVALID`; this run is not
an accepted backtest. Original bytes are preserved, with hashes and a separate
assessment in `docs/evidence/strategy-lab-001/market-new-failed-assessment.json`.
The actor or mechanism that interrupted retention is undetermined.

The confirmed code defect was accepting a valid ledger prefix as a completed
run. Completion now requires the declared terminal event at the declared end,
and the retained head and record count must match the writer. Each append also
checks that the retained pathname still identifies the writer's open file and
that its size has not changed externally. The strategy lab verifies its nested
forecast's terminal record and completion marker before consuming it. These
checks detect loss/replacement; they do not authenticate storage or provide
an atomic multi-file commit against an adversary. A corrected evaluation uses
a new run directory with the same frozen input, plan and economic thresholds.

## Recovered evaluation and completion review — September 16

The corrected `strategy-v05-market-new-r1` evaluation is now verified: 2,337
forecast-ledger records and 1,268 laboratory records reach their declared
terminal events. The separate reader reconstructed 319 forecasts/tournaments
and 473 outcomes. It reports `VALID`; the original failed run remains failed.

All 319 research choices were WAIT: 304 lacked positive modeled growth and 15
were outside analogue support. No eligible non-WAIT hypothesis had positive
mean modeled log growth even in the unstressed selected-model world. The
15-session holdout supplies no profitable-edge finding. Missing quotes remain
an execution-evidence limitation; they did not directly cause these research
WAIT decisions. There were no orders or fills.

Independent review also reproduced an accounting-reader gap in the replay
engine: it could verify a completed ledger, reread a truncated prefix, and
write that prefix's head to COMPLETE. The engine now requires the accounting
reader to agree with the retained writer head and writes that anchored head.
An actual-entry regression injects the truncation before real accounting.
Explicit LF writes keep byte-size checks independent of host newline translation.

The corrected historical run predates those last two persistence changes;
its source manifest is preserved and the verifier truthfully reports a current
code mismatch. The research/model/scenario mathematics used for that run is
unchanged by this completion repair. This is verification of the retained run,
not a claim that the final release reran the entire market study. See the
separate `market-new-r1-review.json` evidence for scope and source differences.
