# APEX Capture 002 — bounded input collection and shadow/replay agreement

Base: `ef84cc430cc63f116b3439df11d3baab718f44e7`. No APEXAI deployment, timer, broker permission, order or position was changed.

## Delivered

- `apex capture-alpaca`: one bounded run through the actual read-only HTTP worker, normalization and shadow reader.
- `apex demo-capture`: substitutes only clock and provider transport; real capture, GARCH, normalization and candidate code run. Every result is labeled `SYNTHETIC_ACCEPTANCE` and observations use `SYNTHETIC_CLOCK`.
- `apex verify-capture`: re-reads the captured response bytes once for hashing and decoding, rebuilds the observation set, then compares each earlier shadow result against an as-of replay of the complete capture.
- `decision.py`: replay and shadow share the quote selector and candidate arithmetic. The existing execution path remains offline. Shadow never constructs a Book, fills, orders or positions.
- Freshness and internal regular-session gaps are visible in each shadow result. The gap counter covers intervals between observed bars, not unknown opening/closing coverage or a holiday calendar.

The comparison proves deterministic agreement of two ways of delivering data to the **same reader**: incremental prefixes during capture, and the full captured set filtered as of each cutoff. It does not independently prove all reader/model mathematics. Existing feature, model and accounting tests provide separate bounded checks.

## Time and quote units

The HTTP worker handles only `GET` on `/v2/stocks/bars`, `/v2/stocks/bars/latest` and `/v2/stocks/quotes/latest`, all at `https://data.alpaca.markets`. Redirects are refused. Authentication headers stay in the worker; credentials and server error bodies are not logged.

The parent measures request and receipt in wall-clock nanoseconds. Receipt is when the parent receives the worker result, an **upper bound on response arrival**, not an exchange/network timestamp. History is available at this receipt, never backdated to its bar events. Model calculation finish is separately recorded; the cutoff is not claimed to be the completion instant.

Provider RFC3339 timestamps are retained in full and parsed to nanoseconds. Future events and incomplete bars refuse before conversion into the existing floating-second replay schema. Downstream epoch comparisons still use that schema; this release does not claim nanosecond event ordering throughout the engine.

The original release used a caller-declared multiplier and a synthetic fixture with 100 shares per provider unit. That fixture exercises conversion arithmetic; it is not evidence of provider units. Current raw fields use neutral size names and conversion is labeled unverified. The recovered SPY/SIP historical REST study supports using multiplier 1 for that study, but quote/trade magnitude comparisons do not independently prove units or generalize to streaming. See [the retained dated study](evidence/quote-integrity-001/upstream/quote-size-units.json) and [review reconciliation](QUOTE_REVIEW_002.md). Candidate sizing retains the conservative raw-size ceiling.

## Request and failure contracts

Defaults: three history pages, then one latest bar and one latest quote; 1,000 bars per requested page; 2 MB maximum successful response body; five-second wall deadline per HTTP worker. No automatic retry or feed substitution. Repeated page tokens refuse. Exhausting the page budget records partial history rather than claiming complete coverage.

The deadline uses a child process that is killed and reaped, including during DNS or stalled reads. The acceptance test first proves that a child entered and remained asleep, then checks the process is gone after timeout. It does not merely inject a timeout exception. **Model fitting is synchronous and is not covered by this HTTP deadline.** There is no claim of uninterrupted live exit servicing or whole-invocation wall-time enforcement.

A capture directory is claimed before credential-dependent requests. Individual requests, raw responses and shadow results are preserved. Completion binds all component digests; failures retain a marker. Missing credentials produce a reviewable `BLOCKED_NO_MARKET_DATA` result and CLI exit code 3. A comparison consisting only of refusals is `REFUSAL_PARITY_ONLY`, not successful model commissioning.

## Executed evidence

The production-shaped synthetic control captured 397 normalized observations from three responses, ran three model observations, and reproduced all three results on replay. GARCH executed in the CLI control. The final hypothetical candidate was `EXPERIMENTAL_LONG`, with `NONE_SHADOW_ONLY` authority. A test passes the resulting input document through the existing replay engine and obtains the same quantity, expected net and model probability; the missing later quote leaves the experimental position unresolved rather than inventing an exit.

The full suite now contains **40 passing tests**. The new checks cover transport-to-reader-to-replay agreement, synthetic labels, real candidate consumption by the replay engine, history receipt causality, quote freshness, a one-nanosecond future event, incomplete bars, malformed bodies, authorization failure, page exhaustion/cycles, clock rewind, file tampering, collision refusal, forbidden order endpoints, an actual killed hung child and the real CLI missing-credential path. The existing full GARCH demo also completed: four experimental entries, four exits, and independent accounting agreement at $10,015.11 cash. Those deliberately favorable synthetic prices are accounting evidence only.

Retained evidence: [test results](evidence/capture-002/tests.xml), [synthetic capture summary](evidence/capture-002/synthetic-capture-summary.json), [standalone refusal](evidence/capture-002/standalone-blocked-summary.json), and [connector probes](evidence/capture-002/connector-probes.json). The CLI reproduces the synthetic capture and replay comparison; licensed market payloads are not committed.

## Actual external access: not commissioned

One advisory Alpaca clock call succeeded before the recorded probes. Four subsequent connector calls were captured with tool-call brackets:

| Recorded call | Result |
|---|---|
| Latest SPY bar, SIP | MCP internal error `-32603` |
| Latest SPY quote, SIP | MCP internal error `-32603` |
| SPY snapshot, IEX | MCP internal error `-32603`; explicitly different feed, no successful substitution |
| Market clock | Success; regular market closed, next open September 15 at 09:30 Eastern |

These errors do not establish an entitlement problem, bad credentials or a market-closure cause. Their cause was not diagnosed from the generic messages. The records are MCP tool responses, not original HTTP packets; their brackets are not exchange receipt timestamps. No market observations were fabricated from them.

The actual standalone CLI also ran. `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY` were absent; one request returned `BLOCKED_EXTERNAL_CREDENTIAL`, zero observations were accepted, zero orders existed, and verification reported refusal parity only. The host's lack of standalone credentials does not explain the separately authenticated connector errors.

**Verdict:** synthetic capture/shadow/replay integration is exercised. A successful real provider capture, continuous streaming, an exchange calendar, live execution, restart recovery and predictive edge remain unproven. The next commissioning run needs healthy stock-data access on the actual runtime during a market session. No further model label or synthetic run can substitute for that evidence.

## Host correction and commissioning bridge

The operator later reported a healthy live Alpaca SIP fabric and Keychain credentials. Source inspection established a macOS launcher exists, but did not verify the collector's current host or liveness. The earlier standalone failure was a property of the build workspace, not a verdict on the operator host. `ops/apex_shadow_capture.sh` retrieves the existing `ALPACA_API_KEY_ID` and `ALPACA_API_SECRET_KEY` entries under the `apex` account and launches only this module's bounded read-only REST capture. It does not log the values and it does not reuse Robinhood credentials or broker routes.

This bridge was prepared for commissioning the new APEX capture → Twin → forecast → candidate → replay path; it was not run on the credentialed host. DigitalOcean is now the explicit operating target; see [Linux deployment](DIGITALOCEAN.md). The REST capture is not a continuous feed.

## Subsequent quote integration correction

The earlier text records that release's provider-documentation assumptions. The historical quote integration does not establish provider units from quote/trade magnitude comparisons. Raw size fields now use neutral names; conversion remains unverified and candidate quantities are conservatively capped by the raw size. The actual v0.7 paper runtime already has a measured-receipt live gate. See [QUOTE_INTEGRITY_001.md](QUOTE_INTEGRITY_001.md) for the integrated source and its acceptance boundaries.
