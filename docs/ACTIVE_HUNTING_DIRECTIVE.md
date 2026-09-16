# APEX Predator — active intraday profit hunting

The operator's goal is an AI-directed intraday system that actively searches for
after-cost edge and compounds a small account substantially. Exceptional returns
are the objective; they are not an established system capability. The system
must learn where to look, when a setup exists, which expression best captures
it, how much risk is justified, and when its advantage has disappeared.

## Operating design

| Organ | Required economic job | Present state |
|---|---|---|
| Opportunity scanner | Continuously prioritize a declared liquid universe by unusual activity, relative movement, catalyst reaction and executable liquidity. | Fixed-symbol research inputs; broad live scanner pending. |
| Market and stock twins | Track causal market, sector and stock state; expose missing and stale information. | Bar-state/regime and new three-instrument peer twin implemented; broad universe and order flow pending. |
| World model | Estimate conditional future paths and test incremental information against strong baselines. | GARCH/EWMA innovations, ridge challengers, matched peer ablation implemented; calibrated edge unestablished. |
| Multiverse | Challenge candidate expressions under model disagreement, different path timing, liquidity and cost stresses. | Original strategy lab has nine worlds; peer challenger uses shared paths and a separately labeled cost sensitivity. |
| AI supervisor | Choose information requests, research priorities and experiments; interpret failures and allocate research effort. | One reviewed study/defer cycle and deterministic outcome review implemented; runtime LLM not configured here. |
| Expression and capital allocation | Compare candidate trades by after-cost expected growth, dependence, capacity and account constraints. | Funded stock/WAIT research; options, shorting and portfolio sizing pending. |
| Execution and protection | Service exits, reconcile actual paper positions, enforce risk and survive process restarts. | Offline experimental fills and read-only shadow package; commissioned paper broker path pending. |
| Experience | Preserve forecasts before outcomes, no-trade opportunities, attribution and every attempted hypothesis. | Per-run ledgers and comparisons implemented; global search accounting and controlled promotion pending. |

The market population should be represented by measurable cross-asset behavior
and properly identified uncertainty about participants. Hypothetical agents may
challenge a scenario; their narrative does not supply measured order flow or
calibrated probabilities.

The fast loop should use validated numerical readers and preauthorized paper
risk rules. The slower AI supervisor should decide where to investigate and
which registered hypotheses deserve an experiment. Slow or unavailable model
inference must not prevent existing position obligations from being serviced.

Active hunting means spending research effort on promising states and acting
when evidence supports it. A broad scanner must count rejected and missing-data
opportunities as well as attractive selections. Otherwise universe selection
creates an invisible backtest search.

## TradingView's possible role

No TradingView connection exists in v0.6. A September 16 directory search found
no exact TradingView plugin. The available TradingCursor listing described
multi-signal analysis but did not establish TradingView chart access; no such
capability or connection was inferred.

TradingView supports alert messages delivered to an external endpoint by
[HTTP POST webhook](https://www.tradingview.com/support/solutions/43000529348-how-to-configure-webhook-alerts/).
That can supply an additional timestamped event after a real receiver and source
contract are commissioned. It does not by itself provide a chart image or an
autonomous chart-reading connection.

TradingView's [Advanced Charts Datafeed API](https://www.tradingview.com/charting-library-docs/latest/connecting_data/Datafeed-API/)
connects a developer-supplied data source to chart components. Embedding such a
chart is not evidence that APEX has obtained TradingView's market feed.

Chart vision should be admitted only through a blinded incremental-information
test: retained pre-decision images versus the same structured market inputs,
with fixed prediction tasks and matched timing. Labels, drawings, incomplete
candles and future-visible screenshots can leak answers. The canonical price,
quote, receipt and order record must remain machine-readable and auditable.

## Next vertical after v0.6

Build a **timestamped opportunity queue across a declared universe**. It should
record why each symbol became interesting, what information was missing, how
quickly the evidence arrived and what the eventual after-cost outcome was.
Feed that queue into the existing forecast/strategy readers and make the AI
supervisor allocate a bounded number of registered experiments. Start with
observed live data and paper/shadow authority, then commission position recovery
and paper execution separately.

The first peer-gap hypothesis was not supported in the fixed August study.
It must not receive trading authority or threshold adjustments based on that
holdout. Any revision needs a new registered comparison. The actual next
research priority is stronger incremental information and broader opportunity
coverage, followed by executable quote evidence.

Three additional, economically distinct proposals are specified in
[EDGE_RESEARCH_002.md](EDGE_RESEARCH_002.md): changing response to selling,
forward-news reconciliation and closing demand versus liquidity replenishment.
They include required observations, AI roles, strong baselines and failure tests.
They are untested designs and are not executable actions in the current director.

Real-money authority remains human-controlled. No source claim, chart score,
simulation frequency, LLM recommendation or research result can override it.
