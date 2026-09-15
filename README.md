# APEX

An integrated market-research system that asks: **what could happen next, what expression survives costs, and did the forecast actually help?**

Version 0.2 adds bounded read-only Alpaca capture and shadow/replay agreement to the offline research vertical. It reuses APEXAI's variance models with new orchestration. It is not a live trading service or a demonstrated source of edge. A successful simulation never becomes a calibrated probability or capital permission by changing a label.

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

The `capture-alpaca` command is the only command that makes provider requests. It uses a subprocess restricted to three read-only market-data endpoints. The demo capture uses an injected clock and transport and labels every result `SYNTHETIC_ACCEPTANCE`.

## Bounded shadow capture

On a host with standalone `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY` already supplied through its secret configuration:

```bash
apex capture-alpaca --out runs/capture-unique-id \
  --history-start "$APEX_HISTORY_START" --symbol SPY --feed sip \
  --round-lot-shares "$APEX_ROUND_LOT_SHARES"
apex verify-capture --capture runs/capture-unique-id
```

`APEX_HISTORY_START` must be a timezone-qualified timestamp within the last ten days. Quote sizes arrive in round lots; supply the correct share count per lot for the requested instrument. That conversion is recorded as an operator declaration, not automatically verified. There is no default conversion or silent feed downgrade.

This captures up to three history pages, one latest bar and one latest quote, observes the real Twin/model/candidate path after each response, then exits. It creates no orders or positions. `verify-capture` decodes the saved bytes and compares the online-prefix results with offline as-of results. This is not a streaming daemon, and its HTTP timeout does not bound model fitting time.

**Current external status:** in the September 15 build session the Alpaca clock worked, but three stock-data connector calls returned internal errors. Standalone keys were absent. Consequently real successful market-data capture and an open-market run remain unproven; the capture control and failure handling are tested. See [capture commissioning](docs/CAPTURE_002.md).

See also [data contract](docs/DATA.md), [initial commissioning evidence](docs/COMMISSIONING.md), [reuse review](docs/REUSE.md), and [architecture and next milestones](docs/ARCHITECTURE.md).

## What is next

1. Commission the new capture adapter against healthy stock-data access during market hours; confirm completed bars and measured quote receipts, then replay them and prove equivalent decisions.
2. Add the timestamped premarket/catalyst packet and setup readers, proving actual downstream consumption. LLM research output remains separate from capital authority.
3. Freeze hypotheses and run chronological multi-session evaluation, calibration diagnostics and realistic execution sensitivity before commissioning live paper execution.
4. Add a restart-safe paper service and operator dashboard; then broader expression comparison and controlled challenger learning.

TradingView vision, Kelly sizing, options/spreads, learned fusion, multi-asset portfolio allocation and autonomous research agents are not implemented in this release. They are additions to earn through comparative evidence, not boxes to mark green.
