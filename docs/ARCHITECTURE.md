# Destination and current architecture

APEX's target is a system that can generate hypotheses, model alternative futures,
choose executable expressions, observe outcomes and improve through controlled
comparison. v0.7 connects research and durable simulated paper accounting, but
the full AI-directed goal is not complete and the new runtime is not continuously
running. Novelty and profitable edge remain questions to test.

## Implemented boundaries

| Component | Actual reader and output | Authority and remaining limit |
|---|---|---|
| Market input | Massive historical import and bounded read-only Alpaca capture normalize `APEX_DATA_V1` observations | Historical availability assumptions remain distinct from measured receipts; recent SIP denied and historical ticks not entitled in the latest checks |
| Market state | Completed as-of bars, returns, volume, close-weighted VWAP proxy, factual setup and regime readers | No consumed news/filing feed, population model or chart-vision reader |
| Forecast | Shrunk empirical mean and zero drift; GARCH conditional variance and named EWMA fallback | Uncalibrated numerical hypotheses; same-model path counts do not establish market probabilities |
| Research | Chronological ridge tournament, historical analogues, strategy/stress laboratory and peer experiment | Frozen development/holdout evaluation; no automatic paper promotion |
| Research director | Strict proposal → laboratory → verification → peer experiment → verification → report | One reviewed study or defer; no broker orders, policy rewriting or capital authorization |
| Subscription planner | Bounded Codex CLI structured proposal with saved ChatGPT authentication | Auth available; required isolation unverified; no runtime inference completed; no API-billing fallback |
| Path outlook | Forecast-bound fitted, zero-direction and adverse/high-volatility sensitivity worlds | Price/excursion quantiles and illustrative economics only; no inferred world probabilities, admission changes or general predictor |
| Paper controller | File-fed ticks connect the fixed shrunk-mean candidate to pending orders and due exit service | Local simulated funded-long account; no LLM paper-policy selection or broker connection |
| Paper book | Transactional SQLite orders, quote evidence, fills, positions, fees, cash and P&L | Independent account arithmetic reconstruction; assumed latency/slippage, no queue or market-impact model |
| Host packaging | Separate shadow and paper systemd services, process bounds, persistent state and status readers | Paper feed publisher and remote deployment uncommissioned; latest SSH attempt was `Network unreachable` |
| Operator reporting | JSON/text account reports, stale heartbeat, retained decisions and input failures | No complete interactive dashboard or external host-health monitor |

## Research evidence and paper policy are separate

The laboratory compares directional and strategy hypotheses using causal samples,
shared paths, matured labels and frozen holdouts. The director runs an allowed
study and derives a descriptive next research action from retained evidence. Its
verifier reconstructs child research artifacts and the final report. An accepted
proposal is not a deployment instruction.

The completed AAPL/SPY/XLK peer study had 20 matched holdout forecasts across five
sessions and did not improve economics over its matched own-stock model. That
result does not select the paper policy. The controller deliberately records
`NO_LLM_POLICY_SELECTION_CONSUMED_BY_THIS_FIXED_NUMERICAL_EXPERIMENT` and uses the
existing shrunk empirical mean candidate path.

Three proposed mechanisms—pressure absorption/decay, source-linked earnings
reconciliation and closing-auction liquidity response—remain
[design-only](EDGE_RESEARCH_002.md). Current minute OHLCV cannot supply their
missing tick, release-receipt or auction evidence. The path outlook exposes
declared sensitivities; it does not implement or validate these proposed edges.
The [mechanism forecast design](MECHANISM_FORECAST_001.md) develops the operator's
combination-lock idea into a proposed causal branch search and transient/persistent
impact comparison. This is also a design-only next direction.

## Durable simulated execution

Each paper tick acquires a single controller lock, binds immutable policy and
session contracts, persists input evidence, and opens the verified account.
Expiration and due exit service precede new model work. The entry evidence in
the primary SQLite database carries the scheduled exit obligation; external
obligation JSON files are redundant copies. A missing copy cannot erase the
database obligation. Failed or unfilled exits retain exposure for later service.

New orders require a strictly later eligible quote. Event and receipt timestamps
must follow the decision; minimum simulated latency, freshness, displayed size,
cash, notional limits, fees, slippage and the entry price limit apply. Entry expiry
is bounded by both the entry window and exit due time and is serviced before quote
processing. The account makes full funded purchases and full exits; it supplies
no margin, shorting, partial fill or queue simulation.

The primary book commits event and account state together with SQLite WAL and
FULL synchronous transactions. Reopening reconstructs history and checks the
unchanged account contract. The independent reader reconstructs Decimal accounting
and event completeness using retained quote evidence. Shared validators establish
timing/provenance consistency; they do not authenticate market data. Runtime
decision-file hashes do not substitute for independent forecast reconstruction.

The live service uses system time and requires measured receipt provenance.
Recorded replay advances an explicit chronological clock; synthetic replay stays
explicitly synthetic. Quotes are never inferred from bars. Missing or stale marks
make affected unrealized results and equity unavailable, while cash, realized P&L
and position obligations persist.

The retained positive, adverse and no-quote controls produced +$3.61, −$2.74 and
$0.00 realized simulated net P&L. **All are synthetic controls, not market results.**
See [v0.7 runtime details](PAPER_RUNTIME_001.md) and the
[release acceptance record](evidence/paper-runtime-001/acceptance.json) for the
completed gates and exact test count.

## Service and planner commissioning

`ops/install_paper.sh` installs a clean exact-commit release separately from the
shadow observer. A dedicated `apex-paper` service consumes atomically published
files and stores its account under `/var/lib/apex-paper`. It has no data-provider
or broker network path. A separate publisher must supply authorized measured
quotes, completed bars and an explicitly sourced session calendar. That publisher
has not been commissioned.

The timer schedules five seconds after each invocation ends, with a 45-second
whole-service bound. Exit ordering within a tick does not guarantee uninterrupted
execution while a prior tick is blocked. The installer leaves scheduling inactive
unless `--activate` completes a current measured-quote commissioning tick. Host
restart tests, retained-state recovery, live quote-unit verification, backup and
an independent stale-service monitor remain deployment evidence to obtain.

The subscription planner uses the documented saved-login
[Codex authentication](https://learn.chatgpt.com/docs/auth) and
[noninteractive CLI](https://learn.chatgpt.com/docs/non-interactive-mode) route.
It checks ChatGPT auth, restricts tools and permissions, requires a successful
host isolation probe, and bounds request/output/process work before accepting a
proposal. The current CLI status confirms ChatGPT authentication and model catalog
access only. Isolation probes timed out, so inference never started. The
[sanitized evidence](evidence/paper-runtime-001/brain-access.json) makes that gap
explicit. `brain-status` does not run probes or inference.

Connecting future AI research choices to paper experiments still needs an
implemented consumer of reviewed policy decisions, causal evidence and controlled
comparison. An authenticated subscription alone does not create that integration.

## Next evidence needed

1. Restore authorized market-data access and commission a measured feed publisher
   on the intended host; prove normalized online decisions agree with retained
   as-of replay and preserve failures and stale-data behavior.
2. Install and commission the persistent paper service on that host, including
   current quotes, restart recovery, retained obligations and independently
   reconstructed account reports. A service label alone proves none of these.
3. Commission supported Codex isolation and a bounded actual inference, then
   connect reviewed research choices to paper experiments without self-promotion.
4. Acquire each new hypothesis's required observations, freeze the search budget
   and evaluation partitions before viewing outcomes, and compare against strong
   price-only and WAIT baselines with costs and uncertainty.

TradingView vision, Kelly sizing, options lifecycle handling, learned fusion,
multi-asset allocation and unrestricted autonomous strategy development remain
unimplemented. They require their own contracts and comparative evidence.
