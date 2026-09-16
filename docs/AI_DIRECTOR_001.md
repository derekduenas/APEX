# APEX v0.6 — research director and peer dislocation hypothesis

The objective remains capacity-aware, after-cost geometric capital growth subject
to survival. This release implements another research vertical toward that
objective. It establishes no profitable edge or capital authority.

## The hypothesis I chose

**A stock that lags a positive market/sector move and begins to recover may offer
a short-lived catch-up opportunity.** The research question is whether a
causally available relative-price gap adds information to the stock's own
features over the next 15 minutes, after declared costs.

This mechanism is informed by research on cross-security information diffusion.
Lo and MacKinlay distinguish cross-stock lead–lag effects from a stock reversing
its own overreaction ([original authors](https://web.mit.edu/Alo/www/Papers/lo-mackinlay-90b.html)).
It does not establish this particular horizon, universe, or strategy. Transaction
price reversal can also reflect bid/ask bounce; Heston, Korajczyk and Sadka's
intraday work motivates quote-based falsification before executable-edge claims
([original paper](https://arxiv.org/abs/1005.3535)). These are research precedents,
not evidence that this new implementation makes money.

The first instrument mapping is AAPL / SPY / XLK. It is a declared fixed
stock/market/sector proxy mapping, not a historically reconstructed constituent
universe. Actual aligned observations form this small cross-asset twin. Broad
market population, institutional inventories and hidden order flow are not
measured by this release.

## The implemented connected system

| Stage | Implemented reader and output |
|---|---|
| Research director | Reads the input inventory and fixed operator policy; consumes a constrained AI/external proposal; runs one allowed research study or defers. |
| Existing laboratory | Original as-of ingestion, market state, regime, GARCH/EWMA forecasts, historical analogues, five strategies and nine worlds remain connected. |
| Market–sector–stock twin | Aligns completed current-session bars; fits the market/sector relationship on 120 prior returns excluding the latest five-minute shock; records beta, residual, recovery and every consumed bar ID. |
| Information experiment | Compares zero drift, shrunk mean, own-stock six-feature ridge and the same six features plus the relative-price gap. Fits use identical peer-valid matured rows, training-only scalers and a frozen supervised holdout. |
| Shared simulated futures | All four models reuse the original conditional GARCH/EWMA innovations; a declared linear drift overlay modifies entire paths. |
| Counterfactual policy | Fixed funded HOLD_LONG versus WAIT, entry strictly after the decision, unchanged horizon, fixed share quantity and declared costs. |
| Experience | All matured opportunities, including WAIT and unavailable-model cases, remain in the outcome record; forecast comparisons use matched complete samples. |
| Verification | Separate readers reconstruct the original lab, peer state, training, paths, decisions, outcomes, summaries and director stages. |
| Operator report | Names planner provenance, evidence counts, refusals and next research need; never reports experimental opportunities as orders or account returns. |

The peer experiment is a challenger. Its base-cost positive-mean rule does not
replace the original laboratory's seven-world growth selector. Three-times-cost
results are sensitivity diagnostics. All four experimental policies share the
same peer-derived setup eligibility: their economics isolate adding the gap
feature conditional on that setup, not the total value of a fully peer-aware
policy versus a fully peer-free policy.

Factor betas update from contemporaneously available prior bars during holdout.
Supervised return coefficients and their scalers stay frozen before holdout.
This distinction matters: causal state estimation continues without fitting to
held-out future-return labels.

## What the AI controls

The director gives a language model authority to select one reviewed study or
defer. The model receives a bounded input inventory and a strict response schema.
Its rationale is retained as text. The executor owns source paths, parameters,
costs, compute limits, verification and the absence of broker capabilities.

There are two planner routes:

- **External proposal:** a retained human or external AI JSON file. This build
  includes the actual proposal authored by Codex in the conversation. The runtime
  labels its authorship unauthenticated and makes no API-model call.
- **OpenAI Responses planner:** an explicit model argument and independently
  configured `OPENAI_API_KEY`. The fixed-endpoint, bounded subprocess client
  validates strict structured output. No credential is stored in artifacts.
  Missing configuration refuses explicitly. This workspace had neither key nor
  model configured when checked; live runtime inference was not exercised.

The runtime model chooses a reviewed experiment rather than writing arbitrary
code. One cycle is one experiment; this is not a continuously deployed autonomous
trader. A global cross-run research-search budget, automated hypothesis coding,
model promotion, portfolio allocation and broker execution remain future work.
Human authorization continues to own any real-money action.

The Responses request follows the official
[structured-output contract](https://developers.openai.com/api/docs/guides/structured-outputs).
API conformity tests use an injected transport; they do not establish a live
provider connection.

## Run and review

```sh
apex director-demo --world catchup --out runs/director-catchup-unique
apex verify-director --run runs/director-catchup-unique
apex director-demo --world null --out runs/director-null-unique
apex director-demo --world continuation --out runs/director-continuation-unique

apex director --input data/peer-dislocation-001/market-input.json \
  --plan data/peer-dislocation-001/frozen-plan.json \
  --out runs/director-market-unique --symbol AAPL \
  --market-symbol SPY --sector-symbol XLK \
  --proposal docs/evidence/peer-dislocation-001/codex-proposal.json
apex verify-director --run runs/director-market-unique
```

The demos use an explicitly scripted synthetic proposal and planted data worlds.
They do not invoke an LLM. For runtime AI planning, replace `--proposal ...` with
`--model YOUR_CONFIGURED_MODEL`; configure the key through the host's secret
mechanism. No command in this vertical places an order.

## Evidence interpretation and next gate

The recovered June–July SPY v0.5 study is verified and chose WAIT at all 319
tournaments. The new August 3–21 acquisition contains 33,538 returned minute
bars across three instruments and five held-out sessions. Historical availability
is assumed at bar completion; original receipt times and revisions are unknown.
There are no contemporaneous quotes. The first experiment is a descriptive
integration study, not an independently sufficient proof of edge.

The completed market run produced 60 forecasts and 20 matched held-out forecast
comparisons across five sessions. Only one peer setup was eligible, and every
experimental model chose WAIT. Adding the peer gap slightly worsened CRPS by
0.000000421 versus the matched own-stock model and produced no economic
improvement. The director returned `HYPOTHESIS_NOT_SUPPORTED`. The planted
catch-up control also failed the incremental peer-value comparison, and the
continuation control exposed negative held-out experimental economics. These
outcomes do not justify promoting the hypothesis.

All 392 tests passed. The real lifecycle CLI verified, and the recorded-data
director's separate verifier returned VALID across six stages with matching
source hashes. See the [acceptance and evidence scope](evidence/peer-dislocation-001/verification-scope.md).

Keep the hypothesis only if it adds held-out forecast information relative to
the matched own-stock model and useful after-cost behavior. Failure is an outcome
to retain. Synthetic success alone is insufficient. Any adjustment after viewing
this study makes this range development data for that adjustment.

Before advancing to paper execution, require a new frozen evaluation range,
quote/midpoint tests against bid/ask bounce, executable ask-entry/bid-exit
revaluation, delay and cost sensitivity, repeated-session evidence and the
separate restart-safe paper adapter. More paths reduce Monte Carlo noise under
the assumed model; they do not create more historical evidence or prove the
model is correct.
