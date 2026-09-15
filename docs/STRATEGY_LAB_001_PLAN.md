# Regime-aware strategy laboratory — design frozen before evaluation

Base: APEX v0.4, main `7c4c00a835a2abff30e57d90939f7ea9d732e72b`.

Objective: discover testable after-cost opportunity, not maximize an uncalibrated simulation win rate. This extends the connected research pipeline; it does not change live broker authority. The user authorized creative redesign and implementation.

## This implementation

1. Causal factual intelligence: premarket gap/range/participation where available, current trend efficiency, volatility and liquidity state, and named missing inputs. Premarket is prior context, never a current quote.
2. Fixed interpretable strategy family: WAIT, funded long hold, momentum breakout, trend pullback, VWAP reversion. Current state supplies eligibility. No shorting, derivatives, or borrowed capital assumptions.
3. Two different conditional-future hypotheses: existing GARCH/EWMA with chronological forecast selection; nearest historical states resampled as whole paths. No historical analogue whose outcome was not available by the training cutoff. Holdout supervised/analogue membership freezes before holdout.
4. Compare strategies on common futures, 1.5x volatility, 3x trading costs, front-loaded versus back-loaded drift with identical terminal returns, and a named adverse first-minute jump. Nominal/analogue/cost/volatility cases drive robust ranking; adverse jump is a separate loss diagnostic, with no invented scenario likelihood.
5. Simulate minute-grid triggers and exits at the observed grid price, including overshoots. Costs are declared assumptions. No intraminute price ordering, executable quote, or guaranteed stop fill is invented.
6. Feedback retains the actual future bar path and counterfactual strategy outcomes; these are research mark-to-bar calculations, not fills, broker P&L or verified executable returns. Monitor forecast coverage using only already matured outcomes.
7. A finite-family session bootstrap will report selection-aware empirical uncertainty only with enough independent-session assumptions/data; thousands of simulated paths are not thousands of independent market observations.
8. Independent artifact verifier reconstructs state, path membership, stresses, strategies, decisions and outcomes. Negative tests include fully rehashed false results.

## Frozen evaluation intent

Run existing synthetic persistent/null/reversal worlds through the real CLI with added explicitly synthetic premarket context. Reuse the already-examined Massive SPY 2026-08-24 through 2026-09-11 capture only as a labeled integration/diagnostic run. It is not a new untouched holdout and cannot establish discovered edge. Do not tune thresholds to make this dataset or these seeds pass economic performance criteria.

Use 1,000 paths per stochastic world, 15-minute targets, the existing 30-minute decision grid with 5-second delivery allowance, and the existing frozen phase boundaries. Preserve every failed artifact under its own run directory. The existing full-suite, demo and verifier gates remain required.

## Research rationale

Repeated strategy searches create false discoveries even when individual backtests look convincing. The family and all trials must remain visible; a nominal held-out split does not undo prior dataset reuse. [Bailey et al., The Probability of Backtest Overfitting](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf).

Time-series uncertainty must account for changing conditions and temporal dependence. This release exposes coverage/shift diagnostics rather than calling empirical scenario frequencies calibrated probabilities. It does not implement or claim adaptive conformal coverage guarantees. [Zaffran et al., Adaptive Conformal Predictions for Time Series](https://proceedings.mlr.press/v162/zaffran22a.html).

## Additional frozen evaluation before results

A newly retrieved SPY range, 2026-06-01 through 2026-07-31, was selected by fixed dates without inspecting prices/returns or fitting models. Development begins June22; holdout begins July13. This is a retrospectively retrieved first-run comparison, not a prospective trade trial or a guarantee that public historical prices were globally unseen. Raw source count: 38,973; normalization rejects:0. The acquisition intent was persisted before the single Massive request.

An additional positive execution control uses fixed 0.0003 per-minute log drift plus independent Gaussian standard deviation0.00015, with no AR term. This deliberately artificial, easily detectable edge tests that the complete selector can produce an opportunity; the existing persistent/null/reversal controls are retained unchanged. No market result or threshold informed this generator.
