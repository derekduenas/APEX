# Research decisions and primary references

The current prototype deliberately uses simple direction hypotheses and reuses tested variance components. Increasing complexity is justified only when an unseen comparison supports it.

1. **Chronological validation.** Scikit-learn documents time-ordered splits and a `gap` between train and test. APEX's future evaluation harness must also account for label horizons, universe selection and repeated testing; using a split utility alone does not prevent leakage. [TimeSeriesSplit documentation](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html).
2. **Adaptive uncertainty.** Zaffran et al. study adaptive conformal prediction for time series, including dependence and changing coverage behavior. This motivates an evaluated uncertainty challenger, not a claim that nominal intervals guarantee conditional trading accuracy. No conformal algorithm is installed in v0.1. [Adaptive Conformal Predictions for Time Series, ICML 2022](https://proceedings.mlr.press/v162/zaffran22a.html).

Implemented now: forecasts precede their realized labels; inputs and paths are retained; the zero-drift comparison uses shared shocks; later labels generate descriptive scores. Not implemented: a complete multi-period calibration study, multiple-testing correction, an independent holdout registry, or a verified promotion policy.

The intended advantage is the combination of information quality, controlled experiments, execution realism and learning discipline. No research citation establishes that this particular system has an edge.
