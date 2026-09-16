# APEX

An integrated market-research system that asks: **what could happen next, what expression survives costs, and did the forecast actually help?**

Version 0.6 adds a bounded AI research director and a market–sector–stock catch-up hypothesis. The director runs the existing regime-aware strategy laboratory, then tests whether peer information improves a matched stock-only forecast using the same causal samples and conditional paths. The retained v0.5 SPY study now verifies completely: all 319 tournaments chose WAIT, with no profitable-edge finding. DigitalOcean remains the deployment target. This release has not been commissioned on the host and does not establish profitable edge or broker authority.

## Run it

Python 3.11 or newer:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock -e .
python -m pytest -q
apex demo --out runs/first-morning
apex verify --run runs/first-morning
apex demo-capture --out runs/capture-control
apex verify-capture --capture runs/capture-control
```

The demo is an explicitly artificial positive-drift market designed to exercise entries and exits. It uses the real model, simulator, candidate evaluator and ledger. Its profit is a fixture result, not a backtest finding. Every run needs a new output directory; collisions refuse.

## The working path

| Stage | What actually runs |
|---|---|
| Inputs | Captured bars and quotes; explicit availability; malformed rows named; conflicts adjudicated as of the decision |
| Market state | Completed regular-session bars, log returns, volume and a close-weighted VWAP proxy |
| Forecast | Shrunk empirical mean hypothesis and zero-drift comparison; GARCH(1,1) Student-t variance, named EWMA fallback |
| Simulated futures | 1,000 saved paths over 15 minutes, conditional variance recursion, recorded random seed |
| Candidate | Fully funded long stock versus WAIT, observed spread, displayed size, assumed commissions, fixed purchase-cost ceiling |
| Experimental execution | Ask entry, bid exit, exit serviced before new entries, missing exits retain exposure |
| Feedback | Forecasts joined to completed outcomes; Brier scores and interval coverage recorded; no automatic promotion |
| Accounting | Separate reconstruction from quote evidence, quantity and declared fees; hash chain and artifact verification |

These are **offline experimental fills**, not orders sent to a paper broker. The model is uncalibrated and the execution assumptions omit latency, queue priority and market impact. The Linux deployment package operates only the read-only shadow capture; it has no broker client or order route.

## Historical data

The Massive connector's CSV export can be imported without embedding a credential:

```bash
apex import-massive --csv /path/to/spy.csv --out data/spy.json \
  --symbol SPY --retrieved-utc 2026-09-15T03:15:49Z
apex replay --input data/spy.json --out runs/spy-history \
  --start 2026-09-11T13:35:00Z --end 2026-09-11T14:35:00Z
apex verify --run runs/spy-history
```

Create `data/` first. Historical bar availability is **explicitly assumed at bar start + 60 seconds**. This does not recover original delivery latency, publication lag or later revisions. The supplied retrieval timestamp is capture metadata, not a measured historical receipt. Request-range completeness is not inferred from returned rows.

In `apex replay`, bars alone exercise the forecast path; they do **not** supply executable quotes. Such a run produces WAIT with `QUOTE_UNAVAILABLE_OR_CONFLICTING`. There is no substitution of bar closes or invented spreads for observed quotes.

The `capture-alpaca` command and its scheduled `shadow-tick` wrapper make provider requests through a subprocess restricted to three read-only market-data endpoints. The demo capture uses an injected clock and transport and labels every result `SYNTHETIC_ACCEPTANCE`.

## Bounded shadow capture

On a host with standalone `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY` already supplied through its secret configuration:

```bash
apex capture-alpaca --out runs/capture-unique-id \
  --history-start "$APEX_HISTORY_START" --symbol SPY --feed sip \
  --round-lot-shares "$APEX_ROUND_LOT_SHARES"
apex verify-capture --capture runs/capture-unique-id
```

`APEX_HISTORY_START` must be a timezone-qualified timestamp within the last ten days. Verify the exact endpoint's quote-size encoding and effective symbol/date units before choosing the conversion. That conversion is recorded as an operator declaration, not automatically verified. There is no default conversion or silent feed downgrade.

This captures up to three history pages, one latest bar and one latest quote, observes the real Twin/model/candidate path after each response, then exits. It creates no orders or positions. `verify-capture` decodes the saved bytes and compares the online-prefix results with offline as-of results. This is not a streaming daemon, and its HTTP timeout does not bound model fitting time.

On the existing macOS host where the two Alpaca secrets already live in the `apex` Keychain account, use the supplied launcher instead of copying either value into a terminal or file:

```bash
ops/apex_shadow_capture.sh --out runs/sip-commissioning-unique-id \\
  --history-start "$APEX_HISTORY_START" --symbol SPY --feed sip \\
  --round-lot-shares "$APEX_ROUND_LOT_SHARES"
apex verify-capture --capture runs/sip-commissioning-unique-id
```

The launcher only exports the existing values to its child capture process. It has no broker endpoint, order command or streaming loop. The output directory must be new.

**Current external status:** in the September 15 build session the Alpaca clock worked, but three stock-data connector calls returned internal errors. Standalone keys were absent in this workspace. The operator reports a healthy existing SIP collector; its current host and connection to this new APEX runtime have not been independently verified. See [capture commissioning](docs/CAPTURE_002.md).

## DigitalOcean runtime

The cloud package uses systemd credential files, a separate `apex-shadow` user, persistent captures, an exclusive process lock and a whole-service timeout. It preserves interrupted runs and exposes stale/degraded status. The Mac Keychain launcher above is optional, not the cloud deployment path.

Follow [DigitalOcean deployment and commissioning](docs/DIGITALOCEAN.md). The daily report is:

```bash
sudo -u apex-shadow -- /opt/apex-shadow/releases/EXACT_SHA/venv/bin/apex shadow-report --root /var/lib/apex-shadow
```

It shows model activity, candidate counts/reasons and the latest result. Orders/fills remain zero and P&L unavailable because the service observes candidates without executing them. This package is tested locally; a running droplet service is not claimed.

See also [data contract](docs/DATA.md), [initial commissioning evidence](docs/COMMISSIONING.md), [reuse review](docs/REUSE.md), and [architecture and next milestones](docs/ARCHITECTURE.md).

## What is next

**v0.4 introduced the chronological forecast tournament, retained in v0.5.** Six causal features train a ridge challenger from matured labels; zero drift and the existing shrunk-mean model share the same variance paths. Selection uses development data and is frozen before a separate holdout. CRPS, Brier, quantile loss, interval coverage, reliability bins and paired session differences are reconstructed from retained artifacts. These are research diagnostics, not trading authorization.

```bash
apex research-demo --world persistent --variance garch --out runs/research-control-unique
apex verify-research --run runs/research-control-unique
```

For real retained data, provide an `APEX_DATA_V1` input and a frozen JSON research plan:

```bash
apex research --input data/input.json --plan data/research-plan.json --out runs/research-unique
apex verify-research --run runs/research-unique
```

The command makes no network requests or orders. See [the research contract and review](docs/EDGE_RESEARCH_001.md) for the plan fields, causal rules, repairs, evidence and remaining gaps. The ridge challenger runs in this offline research path; it is not silently promoted into the deployed shadow service.

1. Commission the new capture adapter against healthy stock-data access during market hours; confirm completed bars and measured quote receipts, then replay them and prove equivalent decisions.
2. Extend the implemented factual premarket/setup reader with timestamped catalysts and cross-sectional context, and measure incremental value. LLM research output remains separate from capital authority.
3. Run the frozen tournament on multi-session market data, then establish calibration and realistic execution sensitivity before commissioning live paper execution.
4. Add a restart-safe paper service and operator dashboard; then broader expression comparison and controlled challenger learning.

TradingView vision, Kelly sizing, options/spreads, learned fusion, multi-asset portfolio allocation and autonomous research agents are not implemented in this release. They are additions to earn through comparative evidence, not boxes to mark green.

## Regime-aware strategy laboratory

APEX now connects factual premarket/regime context, causal forecast selection, whole-path historical analogues, five strategy hypotheses and nine stress worlds. It reconstructs counterfactual outcomes, monitors matured forecast coverage and reports selection-aware empirical diagnostics. The shadow observer exposes current intelligence and a single-model strategy preview. These are research capabilities, not calibrated profit probabilities or broker execution. See [the complete flow and assumptions](docs/STRATEGY_LAB_001.md).

```sh
apex strategy-demo --world persistent --out runs/strategy-demo
apex verify-strategy-lab --run runs/strategy-demo
```

## AI research director and peer dislocation study

The new director connects proposal, original laboratory, peer experiment,
verification and an operator report in one preserved run. The first hypothesis
tests whether a stock lagging a positive market/sector move catches up after its
residual begins to recover. Own-stock and peer-augmented ridge models use the
same matured rows, regularization and shocks; a separate reader reconstructs
all decisions and outcomes. See [the design and authority contract](docs/AI_DIRECTOR_001.md).

```sh
apex director-demo --world catchup --out runs/director-catchup
apex verify-director --run runs/director-catchup
```

The demo has a fixed scripted proposal and synthetic data. For captured inputs,
`apex director` accepts a retained external AI/human `--proposal` or an explicit
`--model` using OpenAI Responses with separately configured `OPENAI_API_KEY`.
Missing runtime AI configuration refuses; it does not silently simulate an AI
call. The director runs one reviewed study or defers. It cannot change cost/risk
policy, deploy models, create broker orders or grant capital authority.

The runtime API-model path was not exercised in the build workspace because no
key/model was configured. The actual Codex-authored proposal is retained under
`docs/evidence/peer-dislocation-001`; its origin remains explicitly unauthenticated
at the program boundary. The multi-symbol historical capture is a research input,
with assumed bar-completion availability and no contemporaneous quote evidence.

The completed AAPL/SPY/XLK study did not support the peer hypothesis: 20 matched
held-out forecasts across five sessions showed slightly worse CRPS and no
economic improvement versus the matched own-stock model. All 392 tests passed;
the recorded-data director independently reconstructed as VALID. See
[the retained evidence](docs/evidence/peer-dislocation-001/verification-scope.md)
and [the active-search development directive](docs/ACTIVE_HUNTING_DIRECTIVE.md).
Three [additional edge research proposals](docs/EDGE_RESEARCH_002.md) describe the
next experiments; they are design-only and require new observations and readers.
