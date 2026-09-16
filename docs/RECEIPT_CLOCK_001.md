# Exact receipt clock and paper preparation

Repairs PR #6 at 2ce757a08030b8a7a1c8370471c94acb120d954c. The original receiver retained integer nanoseconds but passed only `received_ns / 1e9` to the shadow reader. Converting that float back to nanoseconds could move the decision before the very receipt being evaluated. A received quote could therefore be incorrectly reported unavailable.

## Repair and scope

Capture now supplies `now_ns=received_ns`. The original integer passes through observe, visible, twin, quote_at and the regime reader. The integer determines visibility and the quote selector's age check; seconds remain a display/model clock. A supplied integer must agree with its displayed seconds and have the correct type. A seconds-only caller retains its declared clock behavior; the code does not invent a missing original receipt.

The outer observation records its exact integer cutoff. Capture's opening declares `EXACT_RECEIPT_NS_V1`, and capture verification replays those records using the same retained integer. Older capture records without that declaration keep their legacy reader interpretation; they are not silently upgraded into evidence of exact-clock behavior.

Snapshot IDs retain the existing model-input structure. Adding redundant clock metadata to the snapshot hash would unnecessarily change seeded simulation draws even when inputs and decisions are identical. The receipt is instead retained in the captured observation envelope, which binds the complete result.

Regression tests exercise the actual capture path with receipts that round down and up. On the same rows, the legacy rounded cutoff reproduces the missing quote; the integer path sees it and replay agrees. Another test exercises 2,048 adjacent integer cutoffs, admitting receipt-time observations and excluding observations available one nanosecond later. These are synthetic counterexamples, not a claim that this session retrieved the operator's real morning capture.

## The earlier fixture failure, precisely

The sole failure in the prior 549-pass run was `tests/test_research.py::test_simulated_receipt_delay_flows_through_actual_tournament`. It added 0.25 seconds to synthetic observation availability but supplied no integer stamps. Under the new fractional-seconds refusal, every row was rejected before the tournament ran.

The correction declares `event_ns` from the fixture's integer event clock and `available_ns` as the original integer availability plus exactly 250,000,000 ns. Displayed availability is derived from that integer. The source remains synthetic. Assertions were preserved: 88 forecasts, 44 paired holdout samples, synthetic classification, nonempty samples, and a 5-second decision-to-price-origin gap. This does not excuse the separate capture receipt-cutoff defect; that production defect is repaired here.

The paper-book fixtures were also updated in the preceding repair to declare their synthetic fractional times in nanoseconds, preserving their original accounting and latency assertions.

## Host settings and installation

The installer now creates missing files under `/etc/apex-paper` from the pinned release's reviewed examples. It never copies `/etc/apex-shadow/settings.json`, never overwrites existing paper settings, and never changes shadow/APEXAI services. The feed example is SPY/SIP, declared multiplier 1, seven history days, eight pages, and a 2 GiB free-disk reserve. Eight pages cover the operator-reported 4,326-bar acquisition; successful pagination completion still has to be observed, not presumed.

The account example retains the existing $10,000 simulated starting balance and $1,000 maximum notional. These are paper experiment settings, not real capital. Created files use mode 640; installer ownership and service supplementary groups provide access. The settings validator now receives a Path, fixing a separate installer type error. If paper settings already exist, they are preserved and must be inspected for capacity rather than silently replaced.

From a clean checkout at the final reviewed SHA, `sudo bash ops/install_paper.sh EXACT_SHA --activate` prepares missing settings, runs the full suite and CLI verification, installs both units, and invokes the existing real-feed commissioning gate. Neither timers nor account state are reset to manufacture success.

Host access from this session still returns `Network is unreachable`. No live activation, heartbeat, order, fill or P&L is claimed. A healthy host result requires the exact deployed SHA, successful publisher generations, current quote/bar ages, recurring account ticks and independent account verification after installation.

A saved synthetic counterexample reproduces the reported shape exactly: receipt `1789133700035000096`, legacy cutoff `1789133700035000000`, error -96 ns, zero legacy visible quotes versus one with the fixed reader, and capture/replay AGREEMENT. See `evidence/receipt-clock-001/counterexample-96ns.json`.

## Final acceptance

Full suite: 556 tests, 0 failures, 0 errors, 0 skipped. Source, tests, ops and deployment hashes matched the frozen manifest. Full pytest log and JUnit XML are retained. Actual capture/replay CLI agreement and independent demo verification passed. Host commissioning remains unexecuted.
