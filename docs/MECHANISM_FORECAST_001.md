# Competing explanations for the next price path

Status: proposed experiment, not implemented, calibrated or admitted to the paper policy. The v0.7 path outlook is an implemented diagnostic; this document specifies what could make a later forecast more informative. No mechanism or return claim follows from naming a model after physics.

The useful part of the “time machine” ambition is to infer conditional future distributions, compare their trading implications after costs, and measure what the model gets wrong. It cannot require reliable predictions in every regime. Candles, indicators and recent prices often restate the same observations; counting them as independent evidence creates false confidence. A latent state is a statistical explanation, not an observed digital copy of investors.

## The user's combination-lock analogy: search over decisions

The intended search asks: given the current information and account, which conditional sequence of entry, waiting, exit and sizing decisions has the strongest defensible after-cost result across plausible futures? This is broader than forecasting the next price. A policy must be selected before its future is known; subsequent actions can depend only on observations revealed by then. Selecting a different perfect strategy after seeing each complete simulated path would create a hindsight oracle, not an executable policy.

For example, three branches could buy on the next eligible quote, wait for an observed breakout, or wait for an observed pullback. Each branch has predefined expiry and exit rules. Evaluate every policy on identical future scenarios and declared execution assumptions. Choose the current action, observe new information, and plan again. Do not count unrealized or missing exits as successful outcomes. Search cannot manufacture information omitted or misrepresented by the simulator.

The corrected historical run's 14,355,000 evaluations equal **319 decision tournaments × 5 registered strategies × 9 scenario worlds × 1,000 paths**. The five strategies were WAIT, funded long hold, momentum breakout, trend pullback and VWAP reversion. These are repeated evaluations of a small fixed family, not 14 million independent edge discoveries or a search over every possible combination. Of the 319 choices, 304 lacked positive growth across required worlds and 15 lacked analogue support. No eligible non-WAIT hypothesis had positive mean modeled growth even in the unstressed selected-model world. The retained run therefore establishes a negative result for that family and those assumptions; it does not establish that every possible strategy lacks edge. See [the reconstructed study](evidence/strategy-lab-001/market-new-r1-review.json).

A later branch-search experiment should register a finite policy grammar and a search budget before development. Search on development sessions, then freeze the entire search procedure for unseen sequential evaluation. Compare with the existing fixed family and WAIT on identical observations. Keep every attempted combination in the research record; increasing the search space increases the chance of discovering a lucky fit. Start with fixed position sizing so leverage cannot masquerade as forecasting improvement. Broader sizing or instrument selection requires its own execution and account model.

Tree search with a learned world model is an established AI planning approach; the [MuZero paper](https://arxiv.org/abs/1911.08265) demonstrates it in games. That result provides an architectural analogy, not evidence of a trading edge. APEX v0.7 does **not** implement MuZero, an adaptive action tree, unrestricted policy generation or a calibrated probability distribution over all market futures. Its paper policy remains the fixed numerical experiment documented in the runtime contract.

## Next experiment: transient versus persistent price response

Question: can recent trading pressure distinguish a temporary price displacement from a move that persists over the next five minutes?

Fit a small two-state model. One state permits price impact to decay; the other permits it to persist. Estimate state probabilities from sequences of trades, midpoint changes, spreads and displayed NBBO sizes. These states do not prove that an investor is informed, trapped, accumulating or being liquidated.

| Contract | Required comparison or evidence |
|---|---|
| Forecast target | Every five minutes, predict the next five-minute midpoint-return distribution. |
| Strong baseline | Regularized autoregression with identical price, inferred signed-flow, spread, NBBO-size, volatility and time-of-day inputs. Include zero drift. |
| Negative control | Refit a challenger with flow histories shuffled inside prespecified session/time-of-day blocks; preserve train/holdout separation. |
| Availability | Use completed windows available at each decision. Preserve exchange, SIP and actual application receipt separately. Historical SIP receipt is not APEX receipt. |
| Training | Freeze dates, preprocessing, parameters, hypothesis budget and latency assumptions before holdout. State filtering may consume new observations; training cannot consume unmatured labels. |
| Required input | Complete trade and NBBO updates, conditions, corrections, timestamps and documented ordering. Infer trade direction as a proxy and retain ambiguous-case exclusions and denominators. |
| Primary evaluation | Heldout five-minute CRPS against the strong baseline, with uncertainty evaluated in session blocks. Keep all attempted sessions and forecasts. |
| Economic evaluation | Secondary quote-based after-cost outcomes, measured or declared latency and adverse-execution sensitivity. Forecast improvement alone is insufficient. |
| Failure | Reject added complexity when improvement is absent, concentrated in selected sessions, reproduced by shuffled flow, or explained by spread and ordinary reversal. |

Depth is not required for this narrower experiment. NBBO size changes cannot establish actual depth replenishment, so the separate replenishment hypothesis remains untested. Minute bars cannot reliably distinguish bid/ask bounce, midpoint movement and signed trading pressure. The current trade/quote entitlement failures therefore block this experiment; bar-only performance is not a substitute.

The eventual model comparison should test how frequently intervals cover outcomes and when errors increase. A future regime detector or model ensemble must earn its contribution against simpler baselines. No automatic strategy promotion, calibrated state probability, cross-asset generalization or continuous AI controller is implemented by this proposal.

See [the implemented paper runtime](PAPER_RUNTIME_001.md), [its conditional path outlook](evidence/paper-runtime-001/synthetic-path-outlook.json), and [observed data access](evidence/paper-runtime-001/data-access.json).
