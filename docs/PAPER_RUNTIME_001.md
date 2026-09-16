# v0.7: persistent paper runtime and path outlook

APEX now has a persistent, file-fed simulated paper account with orders, positions,
realized and unrealized net P&L, recovery, and independent accounting reconstruction.
The complete AI-directed system requested by the operator is **not complete and
is not continuously running**. No current market feed or remote paper service was
commissioned in this session, and no runtime language-model inference completed.

This release implements a numerical paper experiment and a separate bounded
research planner. It establishes neither profitable market edge nor broker fills.

## What runs

`paper_runtime.py` connects the existing causal market-state reader, shrunk
empirical mean forecast, conditional GARCH paths, regime description and candidate
evaluator to `paper_book.py`. The policy remains
`SHRUNK_EMPIRICAL_MEAN_EXPERIMENT_V1`: funded long stock versus WAIT, under the
frozen account and cost settings. Neither the strategy preview nor an LLM chooses
the paper policy. The failed peer catch-up hypothesis has not been promoted.

Each tick follows this order:

1. Bind the account, policy, supplied session calendar and input evidence; acquire
   the exclusive controller lock and refuse incompatible restarts or clock reuse.
2. Expire pending orders, recover due exit obligations from the primary account
   database, and process an eligible quote before running a new forecast.
3. At the configured scan cadence, create and retain the forecast, paths,
   intelligence, diagnostic outlook and candidate. Submit an eligible simulated
   entry as a pending order for a later quote.
4. Persist the resulting account state and tick status. Reports reconstruct the
   account instead of trusting a previously saved P&L number.

SQLite transactions with WAL and FULL synchronous commits bind account state and
the event chain. Order and decision identities are idempotent: duplicate terms
reuse their existing record; conflicting terms refuse. Reopening the account
verifies its retained history and contract. This protects local consistency;
filesystem durability, host backups and external authenticity remain separate.

Exit obligations are stored in the entry evidence inside `paper.sqlite`.
`obligations/` contains redundant copies. Missing or altered copies produce a
recovery diagnostic while the controller services the due exit from the primary
database. An unfilled exit retains the position and can be renewed on a later
tick. Market closure or missing quotes never manufacture a liquidation.

The exit step precedes numerical inference within each tick. The systemd wrapper
runs ticks serially and bounds each invocation to 45 seconds; a slow invocation
can still delay the next one. This is not a hard real-time exit guarantee or a
separately concurrent broker execution engine.

## Simulated execution and P&L

An entry cannot fill from its decision quote. Quote event time and availability
must both follow the decision, the event must meet the one-second minimum
simulated latency, and the quote must be fresh, unconflicted and large enough for
the entire position. There are no partial fills or modeled queue positions.
Purchases use the later ask plus configured adverse slippage; sales use the later
bid minus slippage. Fees are declared assumptions, not authenticated broker fees.

The controller caps an entry's price at 0.1% above its decision ask. Pending
entries expire at the earlier of the 120-second entry window or the scheduled
exit time minus the minimum latency. Expiration occurs before quote processing,
so a quote arriving after the entry's eligible window cannot create a new position.
Cash and total open purchase cost, including entry fees, are checked at fill time.
The account permits funded longs and full exits without margin or averaging down.

Realized net P&L is sale proceeds after exit fees minus the full entry cost basis.
Unrealized net P&L uses an eligible bid mark and declared exit costs. A stale or
conflicting mark makes affected unrealized P&L and account equity unavailable;
retained cash, realized results and position obligations remain visible. Missing
data is not zero P&L on an open position.

`verify-paper` independently reconstructs Decimal fill arithmetic, event
completeness, positions and cash from the primary account evidence. It shares
timing/provenance validators with the writer. It does not independently rerun the
forecast model, authenticate a data vendor, prove broker execution, or certify an
economic edge. Runtime decision files retain hashes and input references;
consistency hashes are not signatures.

## Path outlook: possible futures under stated assumptions

`paper_outlook.path_outlook` implements the requested forward-looking diagnostic
on the exact paths bound to each forecast. It reports three sensitivity worlds:

| World | Construction |
|---|---|
| `FITTED_DIRECTION` | The saved fitted forecast paths |
| `ZERO_DIRECTION` | The same paths with their fitted drift removed |
| `ADVERSE_DIRECTION_HIGH_VOL` | Residual paths scaled by 1.5, with adverse drift |

For each world it reports terminal and intermediate price quantiles, path maximum
and minimum return quantiles, and, when a candidate quantity exists, illustrative
net-return quantities after current spread and commissions. The outlook also
shows whether the mean net sign changes across worlds. Without a quote it reports
`NO_QUOTE_ANCHOR` and no price worlds.

These are **uncalibrated model scenarios**, not universal predictions or
probabilities that a particular world will occur. Parameter uncertainty and
regime-transition probabilities are not estimated. Intraminute paths are
unobserved. Scenario economics assume a fixed current spread and differ from
paper fills, which depend on later observed quotes and configured slippage. The
outlook does not alter admission thresholds, size, strategy selection or the
account's P&L.

## Run and inspect the controls

After the standard installation, use a new directory for every replay:

```sh
python -m apex.cli paper-demo --world positive --out runs/paper-positive-unique
python -m apex.cli verify-paper --root runs/paper-positive-unique
python -m apex.cli paper-report --root runs/paper-positive-unique --format text
python -m apex.cli paper-demo --world adverse --out runs/paper-adverse-unique
python -m apex.cli verify-paper --root runs/paper-adverse-unique
python -m apex.cli paper-demo --world no-quotes --out runs/paper-no-quotes-unique
python -m apex.cli verify-paper --root runs/paper-no-quotes-unique
```

An installed `apex` entry point is equivalent to `python -m apex.cli`.

| Retained control | Realized simulated net P&L | Meaning |
|---|---:|---|
| Positive synthetic quotes | +$3.61 | Exercises a profitable simulated lifecycle |
| Adverse synthetic future quotes | −$2.74 | Same pre-decision information, worse later execution |
| No synthetic quotes | $0.00 | No orders or fills; no invented executable prices |

**Every row is synthetic. None is market performance or evidence of profitable
edge.** The final release acceptance record and exact gate count belong in
[`evidence/paper-runtime-001/acceptance.json`](evidence/paper-runtime-001/acceptance.json).
That record contains the completed release gates and exact test count.

For retained market inputs, `paper-replay` accepts an `APEX_DATA_V1` file plus a
separate session JSON containing `open_epoch`, `close_epoch`, `calendar_id`,
`calendar_source` and `known_at_epoch`:

```sh
python -m apex.cli paper-replay --input data/input.json --session data/session.json \
  --start "$APEX_REPLAY_START" --end "$APEX_REPLAY_END" \
  --symbol SPY --out runs/paper-recorded-unique
python -m apex.cli verify-paper --root runs/paper-recorded-unique
```

The supplied calendar must cover that session and be known at the tick. Historical
bars with assumed completion availability can support recorded analysis, but
non-synthetic fills require the measured-receipt quote contract. Minute OHLCV
alone supplies no fill evidence. Recorded replay does not establish live service
operation.

## Subscription-powered research planner

OpenAI documents saved ChatGPT sign-in as a supported Codex authentication method
and `codex exec` as the noninteractive interface. This adapter uses that supported
CLI route with structured output; it does not send ChatGPT credentials through a
custom HTTP client. See [OpenAI authentication](https://learn.chatgpt.com/docs/auth)
and [noninteractive mode](https://learn.chatgpt.com/docs/non-interactive-mode).

`codex_planner.py` pins the reviewed CLI version, checks for ChatGPT authentication,
bounds process time and output, restricts the model's callable surface, and
requires a successful isolation canary before inference. The proposal must pass
the existing research director's fixed schema and action limits. The model cannot
rewrite account policy or send broker orders. These controls depend on supported
host permissions; see [OpenAI permissions](https://learn.chatgpt.com/docs/permissions).

```sh
export APEX_CODEX_EXECUTABLE=/absolute/path/to/codex
python -m apex.cli brain-status
# After isolation commissioning on the trusted host:
python -m apex.cli director --input data/input.json --plan data/frozen-plan.json \
  --symbol AAPL --market-symbol SPY --sector-symbol XLK \
  --codex-model gpt-6-astra --out runs/subscription-research-unique
```

The retained check identified ChatGPT authentication, CLI 0.154.0 and
`gpt-6-astra` in the authenticated model catalog. Required restricted sandbox
probes timed out; isolation remains **UNVERIFIED** and no model inference was
attempted. There is no API-billing fallback. `brain-status` checks only version
and saved authentication; it neither runs an isolation probe nor marks the brain
active. See [sanitized brain access evidence](evidence/paper-runtime-001/brain-access.json).

Even after planner commissioning, this release's paper controller does not consume
LLM policy selection. Connecting a bounded AI research decision to a reviewed
paper policy remains additional implementation and evaluation work.

## Separate Linux paper service

`ops/install_paper.sh` installs a clean, exact-commit release under
`/opt/apex-paper/releases/`, runs acceptance checks, and creates a dedicated
`apex-paper` account and systemd service. Persistent state lives in
`/var/lib/apex-paper`; settings live in `/etc/apex-paper/settings.json`. It operates
beside the existing APEXAI and shadow services.

Start with the reviewed [`deploy/paper-settings.example.json`](../deploy/paper-settings.example.json).
The paper service opens no market-data or broker network connection. A separately
commissioned publisher must atomically supply readable, measured `APEX_DATA_V1`
observations at `/var/lib/apex-paper-feed/input.json` and a current session contract
at `/var/lib/apex-paper-feed/session.json`. **That publisher has not been
commissioned.** Declaring `LIVE_PAPER` does not create a feed or a broker account.

On the target Linux host, after preparing settings and a clean checkout:

```sh
sudo bash ops/install_paper.sh "$APEX_EXACT_COMMIT_SHA"
```

This installs with the timer inactive. The installer accepts `--activate` only
when a commissioning tick demonstrates current measured input, an eligible current
quote and the supplied open session. Use activation when installing a fresh exact
release after feed commissioning; the installer deliberately refuses an existing
release directory. Do not use synthetic or old quote files to satisfy that gate.

The timer starts 15 seconds after boot and schedules another invocation five
seconds after the previous invocation ends. `service-health.json` exposes input
failures without replacing missing quotes. `paper-report` labels a live runtime
stale after 60 seconds without a completed tick; this heartbeat is not independent
attestation that a provider or remote service is healthy.

```sh
sudo -u apex-paper -- /opt/apex-paper/releases/EXACT_SHA/venv/bin/apex \
  paper-report --root /var/lib/apex-paper --format text
sudo -u apex-paper -- /opt/apex-paper/releases/EXACT_SHA/venv/bin/apex \
  verify-paper --root /var/lib/apex-paper
```

The build-session SSH attempt returned `Network unreachable`. No remote
installation, active timer, reboot recovery or continuous paper result is claimed.
The Alpaca clock and calendar were callable, but the recent SIP quote request was
denied by subscription. Massive historical stock trade and quote requests returned
`NOT_ENTITLED`. No alternate feed was silently substituted. See the
[data-access evidence](evidence/paper-runtime-001/data-access.json).

## Remaining work toward the operator's goal

Commission authorized quotes and their measured receipts, the file publisher and
the remote service; exercise restart recovery and stale-feed behavior on that
host. Commission the supported Codex isolation boundary before claiming an active
subscription brain. Then connect AI proposals to explicitly reviewed, evidence-led
paper experiments without letting the model approve its own performance.

The three proposed edges—pressure absorption and decay, source-linked earnings
reconciliation, and closing-auction liquidity response—remain
[design-only research](EDGE_RESEARCH_002.md). They need their specified quote,
trade, news or auction observations and causal readers. Neither the new outlook
nor the positive synthetic paper control validates those hypotheses.

The operator's combination-lock idea is developed further in the
[mechanism forecast design](MECHANISM_FORECAST_001.md): a proposed search over
causal branches and a transient-versus-persistent impact experiment. This is a
design-only next direction, not a claim that the current fixed family of paths
searches every market mechanism or has solved prediction.
