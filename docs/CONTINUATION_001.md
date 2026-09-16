# Opening-to-closing continuation diagnostic 001

The frozen signal failed its first recent historical pilot. Do not promote it, reverse it after observing the result, or count it as a profitable library member.

The question was deliberately small: does the direction of SPY's 09:30–10:00 ET open-to-close return help predict its 15:30–16:00 return? The research plan was committed at `1144b1f` before the first data retrieval. January through May 2026 were chosen outside the known June–September project studies. This is a fixed sign-rule diagnostic inspired by intraday-momentum research, not an exact replication of a published regression, universe, sample or execution strategy. There is no fitted model and no parameter selection.

The data source was the connected Massive provider: five monthly requests for unadjusted 30-minute aggregates, 3,264 returned bars, and 102 paired sessions. Five weekdays lacked the required windows. The report labels these missing-window or closed-session exclusions; it does not independently authenticate an exchange calendar. No pagination hint was returned. The complete transformed CSV, connector receipts, manifest, paired decisions and outcomes are retained in ignored local research directories. Only aggregate evidence and hashes are checked in; a clone needs a licensed capture to reconstruct this run.

| Measure | Result |
| --- | ---: |
| Mean signed closing-window log return | −1.558 bp/session |
| Always-long closing-window comparator | +2.672 bp/session |
| Primary paired difference versus always-long | −4.230 bp/session |
| Frozen 95% block-bootstrap interval for that difference | [−8.887, +0.019] bp |
| Active directional hit rate | 47.06% |
| Signed return less illustrative 2 bp round-trip cost | −3.558 bp/session |
| Actual trades / account P&L | 0 / unavailable |

These are signed log-return diagnostics, not brokerage returns. Negative exposure assumes a directional comparison, not stock borrow availability. Bar opens and closes are outcome references, not simulated fills. No compounding, position sizing or six-figure account projection is justified. The primary interval includes zero; it is not evidence of a reliably negative effect that could simply be reversed. Bootstrap uncertainty uses five-session circular blocks and 10,000 resamples with seed 74001 as frozen in the plan. That approximation is not a calibrated probability for future market paths. Roughly 100 observations only give useful power for relatively large effects, before dependence and research-selection penalties.

At each decision, only the completed opening window determines the sign. Decision time is 10:00:05 ET and the target starts at 15:30. Historical availability is assumed to be bar start plus 30 minutes; neither point-in-time revisions nor measured receipt latency are known. Missing and invalid required windows are excluded with reasons, duplicate timestamps are rejected, and mutated research plans are refused. A separate reader reconstructs every result field, paired observation, input digest and implementation digest. It detects a truncated result, but does not independently authenticate provider data.

Run through the installed `apex` entry point (or `PYTHONPATH=src python -m apex.cli`):

```sh
apex continuation-experiment --input data/continuation-001/bars.csv --plan plans/continuation-001.json --out runs/continuation-real-001
apex verify-continuation --run runs/continuation-real-001
```

Use a new output directory for another run. `verify-continuation` exits 2 on a reconstructed mismatch. Tests exercise the actual CLI, tampered outputs, missing and invalid windows, duplicate observations, frozen-plan mutation and future-outcome changes that leave prior signals unchanged across the DST transition.

## Brain and operational status

The approved direction assessment is published in the existing draft PR. The subscription planner remains inactive. A further diagnostic used the same managed restricted permission profile and `/bin/true`, with no model inference; it timed out after approximately ten seconds without stdout or stderr. This narrows the observed blocker to launcher/sandbox startup, before model planning, but does not identify its root cause. The process snapshot was inconclusive. An unsupported `sandbox linux --help` attempt also timed out and is not a valid platform-capability test. Neither attempt relaxed permissions or accessed credentials.

The existing paper ledger, recovery, quote-driven fills and P&L reporting remain implemented and previously acceptance-tested with explicitly synthetic controls. There is still no commissioned continuous measured quote publisher or reachable deployment-host session in this workspace. No real-market paper trades were produced by this diagnostic. The Director's bounded research proposal is not yet a runtime policy selector. The documented Sharadar adapter is recovered, but its credential is not present here; this study did not use Sharadar or assert active access.

The concrete activation dependency remains an authenticated session to the intended always-on host. On that host, run the existing restricted planner acceptance and authenticate Codex with ChatGPT if necessary; separately commission the measured feed, persistent account and timer. Do not mark the brain or paper service active merely because a login or synthetic example succeeds.

## Research consequence

Keep the simulation system as a way to compare explicitly limited hypotheses and actions. This experiment supplies a negative library assessment, not a reason to optimize more combinations on the same outcomes. The original transient-versus-persistent pressure hypothesis still needs historical trades and quotes to distinguish aggressive flow, replenishment and subsequent price response. Its execution test must wait for that evidence rather than replace it with invented bar-derived liquidity. Options and perpetual futures also need their own data, costs and contract-aware accounting before they can be admitted.

The next genuinely new experiment needs a new frozen plan and independent evaluation data. This January–May range is now consumed for this research lineage. Results should constrain the Director's proposals, while economic validation and account limits retain authority over promotion.

Final source acceptance: **458 tests passed in 489.98 seconds**; actual CLI demo and verify returned VALID, continuation reconstruction returned VALID, and an independent stdlib calculation matched the primary result. The checked-in manifest identifies tested Python source and tests. An earlier regression attempt was interrupted after the mismatch-exit-code fix; its log is retained and is not counted as acceptance.
