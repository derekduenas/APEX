# Research decisions and primary references

The current prototype deliberately uses simple direction hypotheses and reuses tested variance components. Increasing complexity is justified only when an unseen comparison supports it.

1. **Chronological validation.** Scikit-learn documents time-ordered splits and a `gap` between train and test. APEX's future evaluation harness must also account for label horizons, universe selection and repeated testing; using a split utility alone does not prevent leakage. [TimeSeriesSplit documentation](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html).
2. **Adaptive uncertainty.** Zaffran et al. study adaptive conformal prediction for time series, including dependence and changing coverage behavior. This motivates an evaluated uncertainty challenger, not a claim that nominal intervals guarantee conditional trading accuracy. No conformal algorithm is installed in v0.1. [Adaptive Conformal Predictions for Time Series, ICML 2022](https://proceedings.mlr.press/v162/zaffran22a.html).

Implemented in v0.4: forecasts precede their realized labels; inputs and paths are retained; zero-drift, shrunk-mean and six-feature ridge hypotheses use shared variance shocks. A chronological multi-session tournament separates warmup, development and holdout; fits/scalers use only matured earlier training labels; supervised training and selection freeze at the holdout boundary. It reports CRPS, Brier, pinball losses, interval coverage, reliability bins and descriptive paired session differences. The five-second decision allowance is frozen in the plan to support actual bar delivery after completion. See [EDGE_RESEARCH_001](EDGE_RESEARCH_001.md).

Not implemented: a validated market calibration study, multiple-testing correction, an independent holdout registry, or a verified promotion policy. Re-running a holdout after inspecting it is exposed research, not fresh confirmation. Three registered hypotheses in one manifest do not count all previous human/agent experiments.

The zero-drift directional probability is exactly 0.5 under the symmetric return law; finite simulated baseline frequencies are retained as Monte Carlo diagnostics. Simulation uses continuously truncated, variance-normalized shocks so its finite-horizon arithmetic price expectation exists. This tail bound is a modelling assumption, not a financial loss bound; its effect needs chronological sensitivity testing. Model fitting still uses the original Student-t likelihood.

The intended advantage is the combination of information quality, controlled experiments, execution realism and learning discipline. No research citation establishes that this particular system has an edge.
