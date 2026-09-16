# APEX: research direction and brain activation

Assessment date: 2026-09-16. This is a source-informed design review and operational handoff, not a new backtest, model promotion or change to the paper policy. The operator explicitly includes stocks, options and BTC derivatives in the research universe. Real-money execution remains outside this work.

## Judgment

Keep the accounting, causal replay and evidence retention. Change the priority from expanding the simulator to establishing one economically useful prediction on new market data. APEX has useful engineering and negative research results; it has no demonstrated profitable edge. Test counts, simulated path counts and an LLM's confidence cannot substitute for that evidence. Extraordinary account growth is not an outcome this research can promise.

A reusable strategy library is useful when each entry includes its mechanism, causal inputs, execution rules, applicability conditions, costs, invalidation conditions and actual evaluation history. Calling five public strategies “proven winners” would be misleading. Both the strategies and the mechanism that selects between them must survive unseen evaluation. The selection procedure can overfit even if every individual strategy has appeared in a paper. The [probability-of-backtest-overfitting paper](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf) explains why searching configurations creates false discoveries; an untouched evaluation and a record of every attempted search remain necessary.

## Recover existing data assets

The older APEXAI checkout at commit `0852f06d0a26100036c77a4f31472a09fc164915` contains `apex/data/sharadar_api.py`, `apex/data/nasdaq_api.py`, the Sharadar fetch scripts, `scripts/nightly_pull.py`, and `ops/nightly_pull.sh`. The launcher retrieves an existing macOS Keychain item with account `apex` and service `NASDAQ_DATA_LINK_API_KEY`, exporting it only to its child. The provider reads `NASDAQ_DATA_LINK_API_KEY` from the environment. No credential value was read or copied during this review. The variable is absent in the current new-runtime process, and no Sharadar connector is exposed here.

The retained `SHARADAR-DAILYVOL-001` artifact describes 4,531 daily rows from 2004-01-05 through 2021-12-31 and reports volatility-score improvements over a rolling-variance comparator in its historical development sample. It explicitly is not evidence for the 15-minute options pilot. This review inspected that artifact; it did not reconstruct the entire old study or establish current provider entitlement. Preserve it as prior research, not a fresh holdout.

Sharadar describes fundamentals, EOD stock/fund prices, corporate actions, securities, institutional and insider data. Its [provider documentation](https://sharadar.com/) supports using it for universe construction, historical context and slower state variables. It does not replace an intraday trade/NBBO stream. Nasdaq as-reported fundamentals and a subsequently revised snapshot also cannot be treated as interchangeable. Preserve exact table/dimension, filing/availability semantics, retrieval metadata, delistings and corporate-action treatment before admitting any historical features. Current direct Sharadar documentation and the older Nasdaq adapter are different access routes; do not assume their credentials or entitlements are interchangeable.

## Five candidate families, not five certified strategies

| Family | Question to test | Evidence boundary and required inputs |
|---|---|---|
| Intraday continuation | Do early moves continue under specified volatility, participation and event conditions? | [Gao et al.](https://www.researchwithrutgers.org/en/publications/market-intraday-momentum/) document first-half-hour/last-half-hour predictability in historical SPY data. This is not proof of a generic breakout or today's profitability. Require fresh chronological bars and quotes for executable outcomes. |
| Temporary pressure reversal | Can we distinguish transient displacement from persistent repricing? | [Cont, Kukanov and Stoikov](https://arxiv.org/abs/1011.6402) find a relationship between order-flow imbalance and contemporaneous price changes. That alone is not an actionable forward signal. Require sequenced trade/NBBO histories and compare incremental prediction against ordinary reversal, spread and volatility controls. |
| Relative value | Does an economically related instrument temporarily diverge in a predictable way? | APEX's peer catch-up experiment was unsupported. Any new relationship needs new causal evidence, proper universe selection and a fresh search budget. Do not relabel that failed experiment as an admitted pairs strategy. Financing, borrow and both legs' costs matter where applicable. |
| Event interpretation | Does the price response underreact to information relative to what was already expected? | Require first-seen releases, timestamped prior expectations, revisions, and executable quotes. Sharadar supplies useful company context but not automatically the needed intraday news chronology or contemporaneous consensus. A longer-horizon earnings anomaly does not validate a five-minute strategy. |
| Volatility pricing | Does a forecast of realized movement differ usefully from the volatility and tails priced by options? | Research on [variance risk premia](https://www.federalreserve.gov/pubs/feds/2010/201014/201014pap.pdf) motivates a distinction between physical forecasts and market prices, not free arbitrage. This source studies materially longer horizons; require separate intraday validation, synchronized options quotes, surface dynamics, costs and lifecycle handling. |

Every library entry should carry a version, registered search budget, market/horizon scope, point-in-time inputs, exact policy, execution contract, benchmark, development history, frozen evaluation, failure conditions, and a status such as PROPOSED, TESTING, UNSUPPORTED or PAPER_ELIGIBLE. The five rows above are research candidates only. No new executable library or router is implemented by this document.

## Use different models for different jobs

| Job | Recommended first comparison | Advanced challenger and admission condition |
|---|---|---|
| Direction and conditional return distribution | Zero-drift and simple regularized state models, with shared innovations and empirical historical-path comparisons | Flexible conditional distribution models only after improving proper scores, calibration and economic outcomes on unseen data. |
| Volatility | Retain EWMA/GARCH baselines; test a realized-volatility model when intraday observations support its target | [HAR-RV](https://statmath.wu.ac.at/~hauser/LVs/FinEtricsQF/References/Corsi2009JFinEtrics_LMmodelRealizedVola.pdf) uses volatility across time scales. [Rough volatility](https://arxiv.org/abs/1410.3394) is a challenger with published realized-volatility forecasting results, not an automatic source of directional alpha. Neither result establishes our 15-minute performance. |
| Execution | Chronological observed quotes with explicit receipt, latency, size and spread; retain unfills | [ABIDES](https://arxiv.org/abs/1904.12066) can model interacting agents, exchange messages and latency. Calibrate against observed execution behavior before using it to support profitability claims. A population simulation does not identify real participants' hidden intentions. |
| Options expression and management | Observed prices and a contract-aware valuation/risk model, including possible IV changes before exit | [Deep Hedging](https://arxiv.org/abs/1802.03042) optimizes hedging with frictions and demonstrates a synthetic Heston example. It is not evidence that a directional options strategy earns alpha. |

GARCH models conditional volatility; it does not supply an independently established expected return. Option-implied risk-neutral distributions embed pricing and risk compensation, while trade-profit forecasts require a physical outcome model. A stock path plus an expiration payoff is insufficient to price an intraday options exit. The simulator and the policy optimizer should be evaluated separately so an optimizer cannot win by exploiting a simulator's mistakes.

## Broader expressions

Stocks and ETFs remain the simplest existing paper accounting path. Options should compete as expressions of a specific forecast, starting with contractually bounded structures whose complete lifecycle can be modeled. The [Options Industry Council](https://www.optionseducation.org/strategies/all-strategies/bull-call-spread-debit-call-spread) describes a debit call spread's premium/limited-payoff tradeoff and expiration risks. Correct direction alone does not ensure profitable options execution. Contract multipliers, quotes on both legs, fill simultaneity, IV, time decay, exercise/assignment and liquidation must be represented; a textbook terminal bound is not a complete broker lifecycle.

BTC perpetuals or perpetual-style futures are legitimate research candidates. Specify the actual venue and contract. Model spot/mark/index distinctions, funding, basis, tick/lot/contract units, trading interruptions, margin and liquidation. [Coinbase's US perpetual-style mechanics](https://help.coinbase.com/en/derivatives/perpetual-style-futures/settlement-and-other-mechanics) illustrate why funding changes account cash; these mechanics must not be assumed for another venue. No venue or account eligibility was verified here. Perps cannot inherit an options defined-max-loss certificate or be admitted merely because leverage is available. Research inclusion does not grant real-money routing authority.

Broader instruments expand ways to express information. They also expand model and execution requirements. Do not add them merely to convert a weak stock forecast into a larger leveraged outcome.

## AI responsibilities and missing integration

The intended Director reads actual state and a versioned research registry, proposes bounded experiments, requests evaluations, compares evidence, proposes the next permitted paper policy, and attributes errors after outcomes mature. Numerical readers validate the proposal, evaluate economics, enforce account constraints and reconstruct P&L. An LLM's prose probability must not become the sizing input.

The selector needs its own ablation: compare the adaptive selection rule with the strongest frozen policy, a simple numerical selector and WAIT on the same unseen opportunities. Persist unchosen candidates and missed opportunities as well as trades. Replanning may use newly available observations; it cannot select a different perfect policy after inspecting a complete future path.

The current Director runs one allowed peer study or defers. The paper runtime consumes a fixed shrunk-mean policy. A reviewed policy registry/consumer, broad opportunity scanner, adaptive action search and continuous AI scheduler remain implementation gaps. Authentication alone will not create those capabilities. Due exits must remain serviceable without waiting for an LLM response.

## Activate the research brain: operator handoff

A fresh `apex brain-status` check in this review returned `CODEX_CHATGPT_AUTH_AVAILABLE_ISOLATION_UNVERIFIED`, CLI `0.154.0`, authentication `CHATGPT`, isolation false, runtime LLM call false. This command checks authentication availability, not inference. The previously retained generated-canary sandbox probes timed out. No additional canary or inference attempt was made in this review.

The isolation gate is implemented in our `codex_planner.py`; the timeout is not evidence that the user's subscription is unsupported. Its cause has not been established. Do not describe it as an OpenAI account approval problem or ask the user to buy an API key to solve it.

What is needed from the operator:

1. A Codex execution session on the intended DigitalOcean host, or a connection from this workspace that can reach that host. The last SSH attempt from here returned network unreachable. Existing host/account information is already known; a new password pasted into chat is not needed.
2. If Codex on that host is not already authenticated, complete its supported interactive ChatGPT sign-in. [OpenAI documents](https://learn.chatgpt.com/docs/auth) `codex login` and a device-auth flow for headless environments. Keep credential contents on the trusted host. The session authenticated here does not establish that the remote host is authenticated.

Engineering tasks after host access (ours to complete): confirm the pinned CLI, diagnose and establish its supported restricted execution boundary, run one bounded structured research proposal, preserve request/response identity and actual inference status, validate the allowed action, and exercise timeout/invalid-output behavior. [Noninteractive Codex](https://learn.chatgpt.com/docs/non-interactive-mode) supports saved CLI authentication and structured outputs. Subscription usage limits remain applicable; there is no implicit API-billing fallback or guarantee of unattended availability.

Brain acceptance means an actual valid model response was consumed by the allowed research reader. Continuous paper acceptance additionally requires authorized measured quotes, session publication, the missing reviewed-policy consumer, scheduling, restart recovery and actual account reports. These are separate gates; market-data entitlement is not required merely to test a bounded research-brain response against a retained input.

## Next work order

1. Commission the research-brain boundary on a reachable host and recover the existing Sharadar pipeline under its actual credential configuration. Reuse the legacy study as context, not new evidence.
2. Build one source-to-score-to-paper experiment on real data. Replicating the published intraday-continuation setup is a useful pipeline benchmark; the first original challenger remains transient-versus-persistent price response from `MECHANISM_FORECAST_001.md`. Its required tick/quote entitlement is unresolved.
3. Freeze exact symbols, horizons, feature availability, policy candidates, costs, test budget, evaluation dates and uncertainty procedure before loading new evaluation outcomes. Select sample length through a declared power/uncertainty analysis; a convenient number of days is not proof of adequacy. Previously inspected SPY and peer-study holdouts cannot be reused as fresh validation.
4. Test incremental information first, then the policy search, then options or other expressions. Report forecast quality and executable after-cost outcomes separately. Expand only when a component adds measured value. Continue recording paper refusals and losses faithfully while research proceeds.

No execution code was changed, no new market backtest ran, and no new strategy was promoted in this assessment. The v0.7 Python/test manifests remain the evidence for the previously tested source, not for an implemented version of this proposed roadmap.
