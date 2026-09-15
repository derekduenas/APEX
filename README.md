# APEX

An integrated market-research system that asks: **what could happen next, what expression survives costs, and did the forecast actually help?**

Version 0.1 is a runnable offline research vertical, built by reusing APEXAI's variance models and replacing the orchestration. It is not a live trading service or a demonstrated source of edge. A successful simulation never becomes a calibrated probability or capital permission by changing a label.

## Run it

Python 3.11 or newer:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock -e .
python -m pytest -q
apex demo --out runs/first-morning
apex verify --run runs/first-morning
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

These are **offline experimental fills**, not orders sent to a paper broker. The model is uncalibrated and the execution assumptions omit latency, queue priority and market impact. No broker client, trading credential, live service or deployment command is shipped.

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

Bars alone exercise the forecast path; they do **not** supply executable quotes. Such a run produces WAIT with `QUOTE_UNAVAILABLE_OR_CONFLICTING`. There is no substitution of bar closes or invented spreads for observed quotes.

All commands are offline except the separate data retrieval performed outside this package. See [data contract](docs/DATA.md), [commissioning evidence](docs/COMMISSIONING.md), [reuse review](docs/REUSE.md), and [architecture and next milestones](docs/ARCHITECTURE.md).

## What is next

1. Capture completed bars **and measured quote receipts** through one read-only Alpaca adapter, then replay the same capture and prove equivalent decisions.
2. Add the timestamped premarket/catalyst packet and setup readers, proving actual downstream consumption. LLM research output remains separate from capital authority.
3. Freeze hypotheses and run chronological multi-session evaluation, calibration diagnostics and realistic execution sensitivity before commissioning live paper execution.
4. Add a restart-safe paper service and operator dashboard; then broader expression comparison and controlled challenger learning.

TradingView vision, Kelly sizing, options/spreads, learned fusion, multi-asset portfolio allocation and autonomous research agents are not implemented in this release. They are additions to earn through comparative evidence, not boxes to mark green.
