# Destination and delivery sequence

APEX's ambitious target is a research organism that can generate hypotheses, model alternative futures, choose executable expressions, observe outcomes, and improve under controlled comparison. Novelty and profitability remain questions to test.

The first implementation prioritizes one comprehensible flow. It proves wiring and accounting while leaving the unproven intelligence visible.

| Capability | Current implementation | Next evidence needed |
|---|---|---|
| Historical market ingestion | Massive CSV import exercised | Full requested-range coverage and revision/latency accounting |
| Live market capture | v0.2 bounded REST capture built; synthetic acceptance passed; real access blocked | Successful provider capture during market hours, quote-unit verification, then streaming |
| DigitalOcean shadow runtime | v0.3 Linux tick, process lock/recovery, systemd credentials, daily report and deployment package | Actual droplet installation, real data capture and scheduling/reboot evidence |
| Premarket context | Causal gap/range/volume/source-age reader integrated into strategy setup eligibility | News/filings integration and an actual LLM brief remain absent |
| TradingView chart vision | Not connected or consumed | Verified tool/runtime route and a blinded contribution experiment |
| Market-state features | Minimal subset implemented | Independent checks for each added feature and intended reader |
| Direction forecast | Shrunk mean and zero drift; six-feature ridge in offline chronological tournament | Real-market holdout evidence, costs, calibration and robust uncertainty |
| GARCH and conditional paths | Exercised on synthetic and returned historical bars | Holdout diagnostics and model comparison, including bad regimes |
| Regime and strategy laboratory | Descriptive regime changes analogue matching and setup eligibility; shared-path strategy and stress tournament implemented | Fresh independent market validation, richer causal inputs, and calibrated admission |
| Cross-instrument expression | Funded long stock versus WAIT only | Stocks/ETFs universe, options quotes, permissions and lifecycle-aware risk |
| Sizing | Fixed purchase-cost ceiling | Account constraints and uncertainty-aware allocation; Kelly remains absent |
| Trade lifecycle | Offline quote-based experimental fills | Measured latency, liquidity assumptions, restart recovery and paper adapter |
| Feedback | Matured-label training, frozen holdout, per-strategy counterfactual outcomes, coverage alarm and fixed-family session diagnostics | Independent repeated experiments and controlled promotion, not self-approval |
| Operator dashboard | Deferred | Read the actual ledger and health artifacts, not parallel summaries |

## Capture and replay agreement: implementation landed in v0.2

The read-only Alpaca adapter now bounds request count, response bytes and request wall time, preserves provider responses and quote-unit assumptions, and feeds the actual shadow readers. SIP and IEX are separately labeled; IEX is not presented as national NBBO. Standalone credentials are required for the HTTP route, independently of interactive connector access. See `CAPTURE_002.md` for the successful synthetic flight and actual external-access failures. The same frozen input contract feeds replay and the future paper service. A live connectivity indicator does not establish coverage or consumption.

Acceptance: capture one bounded session segment, show each accepted/rejected input, exercise forecasts and candidate creation, and compare the normalized replay's decisions. If quotes are unavailable, the actual refusal must remain visible. This release does not require an edge claim or an order merely to demonstrate integration.

## Research commissioning

Freeze a data universe, decision horizon, costs, hypothesis count and evaluation dates before comparing candidates. Separate training, calibration and unseen evaluation. Purge training labels that overlap later partitions and record every attempted hypothesis. Keep zero-drift and WAIT baselines. Report uncertainty, costs, drawdowns, turnover and unresolved positions; a single favorable historical segment establishes no edge.

The AI desk can propose setups and research changes, but proposals enter this protocol as challengers. It cannot rewrite outcomes or promote its own probability labels. Conformal intervals are a research candidate, not an automatic certificate of conditional market accuracy.

## Live paper commissioning

Add a persistent single-writer service with restart recovery, a real exchange calendar, broker paper-account verification, idempotent orders, reconciled positions, cost terms, quote provenance, stale-data behavior and an operator stop. Due exits must remain serviced during slow inference or a hung external provider; the current synchronous offline loop proves event ordering only.

DigitalOcean, Robinhood and TradingView service access were not verified in this build session. No prior deployment was modified. The [Linux package](DIGITALOCEAN.md) deploys only the shadow observer, not a paper broker daemon.

The eventual dashboard should expose freshness, stage execution, model choice/fallback, distributions, candidate rejection reasons, position obligations and independently reconstructed P&L directly from these artifacts.
