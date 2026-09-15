# Reuse from APEXAI

Source repository: `derekduenas/APEXAI`.
Inspected local source commit: `a0af2b83e3132cf69357801f3137a06eafcf3a6f`.

| Original file | Destination | SHA-256, identical source bytes |
|---|---|---|
| `apex/worldmodel_wb/contracts.py` | `src/apex/reused/contracts.py` | `f04c8d6f2e5f23c89d65431659cda7ffcd17ea0633a96ccf12edf78a3be80713` |
| `apex/worldmodel_wb/vol_models.py` | `src/apex/reused/vol_models.py` | `38f68869411124545a2147e2163c974a86776a76fba33900f1b8e5739b5457f5` |

The adopted implementation supplies standardized Student-t innovations, GARCH/GJR variance recursion, convergence checks, and EWMA. The wrapper currently selects GARCH(1,1), not GJR; importing other classes does not establish their execution.

Important limits identified during inspection:

1. Legacy `Model.fit` defaults missing availability to event time. The new wrapper explicitly requires finite `available` on every row and refuses future rows before invoking it.
2. A logical fit counter is not the number of optimizer calls. GARCH may use primary L-BFGS-B, a Nelder-Mead polish, and a second L-BFGS-B start, plus up to eight feasibility draws. The run records model-level fit attempts and the implementation's convergence record; there is no wall-clock timeout guarantee.
3. Numerical overflow or convergence refusal produces a named fallback, except a firewall refusal, which stops the forecast. A fallback is not silently reported as GARCH.
4. The legacy multi-step Student-t density is an aggregation approximation. This wrapper uses recursively simulated paths for terminal quantiles and probabilities instead of presenting that approximation as an exact density.
5. Parameter uncertainty, structural breaks, jumps and market-impact uncertainty are not modeled. Their absence limits interpretation of the paths.

Adoption tests exercise actual fitting, the standardized-t variance convention, one-step simulated variance against the GARCH recursion, fallback labeling and a non-bypassable wrapper timing check. They are bounded adoption evidence, not proof of all inherited classes or market calibration.

The new execution ledger is intentionally independent of the old options-only ledger. The unreviewed `5f427af` options reconstruction is not imported. New APEX does not presume old Hunter branches are merged, deployed or commissioned.
