# APEX v0.1 commissioning record

Date: 2026-09-15. These are builder-executed checks, not an independent external review. The delivered scope is an offline research prototype.

## Executed paths

| Evidence | Synthetic control | Massive historical bars |
|---|---:|---:|
| Scans | 4 | 4 |
| Forecasts and saved path sets | 4 | 4 |
| Paths per forecast | 1,000 | 1,000 |
| GARCH11_T actually fitted and used | 4 | 4 |
| Experimental entries / exits | 4 / 4 | 0 / 0 |
| Forecasts joined to matured outcomes | 4 | 4 |
| Artifact and accounting verification | VALID | VALID |
| Unresolved positions | 0 | 0 |

The synthetic market deliberately has positive drift. Its net of **$15.11** and ending cash of **$10,015.11** establish an accounting control only. This is not a fitted real strategy or profitability evidence.

The historical run produced WAIT on every scan because no executable quotes were provided. No prices or spreads were invented. Its mean Brier score was **0.2556385**, versus **0.25285525** for the shared-shock zero-drift baseline (lower is better). The hypothesis was slightly worse on this tiny segment. Four observations cannot establish comparative edge or calibration.

## Historical input provenance and limits

One bounded read-only request was made through the connected Massive tool:

`/v2/aggs/ticker/SPY/range/1/minute/2026-09-10/2026-09-14`, with `adjusted=false`, `sort=asc`, `limit=50000`.

The connector returned transformed CSV, not a retained original HTTP response. It contained **1,711 rows**: 860 on September 10 and 851 on September 11, with 390 regular-session rows on each date. **No September 14 rows were returned.** The requested range is not claimed complete; why that date is absent was not investigated in this build. The diagnostic uses the returned September 11 session, **09:35–10:35 Eastern**, with September 10 history.

- Saved transformed CSV SHA-256: `a58ff8180429b17047e3c88e4ba7dcbf4628429e118eb42748ffe0b165aeb82d`.
- Imported JSON SHA-256: `2a6aa949b48feb5ebcd00cb73f9f2588c5a7aba80a1a62dd4aef4fc85c13aefb`.
- All 1,711 normalized rows accepted; zero malformed rows observed in this capture.
- Capture timestamp `2026-09-15T03:15:49Z` was recorded **after the tool call**, not measured at transport receipt.
- Historical availability is `BAR_COMPLETION_ASSUMPTION_V1`, not measured historical receipt. Later revisions and publication latency remain unknown.
- Raw market data remains outside Git. The committed historical summary is reported evidence; independently rerunning it requires the matching capture. It is not advertised as publicly reproducible from the summary alone.

## Tests and scope

**26 tests passed** on the final executable source, followed by the real CLI GARCH demo and artifact verification, and the historical import/replay/verification commands. See the preserved [JUnit report](evidence/tests.xml), [synthetic summary](evidence/synthetic-summary.json), and [historical summary](evidence/historical-summary.json).

The tests cover independent feature arithmetic, no overnight return, malformed rows, future/conflicting observations, provenance-equivalent duplicates, forecast invariance to future prices, actual behavior under changed history, real GARCH execution, fallback versus firewall, standardized-t variance, path variance, after-cost WAIT, missing/stale exit quotes, retained exposure, event ordering, run collisions, preserved failures, input/manifest/path/ledger tampering and independent cash reconstruction. They do not test live account authorization or live broker fills.

The run's source hashes bind the executable Python files. Runtime package versions are recorded. This is not a signed deployment attestation, full dependency closure proof or adversarial authenticity guarantee.

## Readiness

- Connected offline data → model → paths → candidate → lifecycle → accounting: **exercised**.
- Real market bars → GARCH → paths → matured scores: **exercised under a historical availability assumption**.
- Profitability, calibration, operational resilience, live quote ingestion, live paper deployment: **not established**.
- Premarket AI, TradingView consumption, Kelly, options, portfolio allocation, autonomous learning and dashboard: **not delivered in v0.1**.

The next implementation is the measured live capture adapter and capture/replay agreement, followed by the premarket/setup seam and chronological research commissioning described in [architecture](ARCHITECTURE.md). No existing APEXAI release, timer, account or position was changed.
