# Publisher to paper-service repair

Repairs Claude's c79563d8d239109565f48a98f73fcd895eb3fa9a directly. This is a paper-only integration, not a premarket intelligence component or a demonstrated trading edge.

## Connected path

The paper unit now consumes `--generation /var/lib/apex-paper-feed`. The installer installs both publisher and account units. Publisher-owned files use a dedicated group; the account service receives that supplementary group, and the publisher receives the account group only to read settings. Credentials remain root-owned and supplied through systemd LoadCredential. No broker endpoints are added.

Publisher operations take a nonblocking kernel lock. Generation files cannot be rewritten through publication after either half exists. A reader resolves one generation under the allowed generations directory, reads bounded strict JSON, and checks the pair identity. Atomic replacement and directory fsync make publication explicit; failed attempts preserve prior generations. Retention is not automatic: the publisher enforces the configured free-disk reserve before capture.

Publisher failure, unknown outcome, mismatched status/generation, future timestamps, or generation age above 60 seconds deny market input to the paper service. The controller receives an explicitly blocked empty document while retaining the session, so order expiry and position obligations remain visible. No fills are manufactured. Existing marks age out under the book's independent quote-age rule. A damaged generation can use already-pinned calendar facts for the current date to service obligations without quotes; absent calendar facts remain a named service block, never an invented schedule.

Calendar known-at time is measured after response receipt, with integer request/receipt stamps retained. Stable facts and the first receipt are pinned once per date. Repeated publication reuses the same PaperSession record. Capture completion timestamp and per-observation measured receipts remain distinct. Failure reasons are sanitized both in status files and at the publisher exception boundary.

## Host installation and activation

Prepare `/etc/apex-paper/settings.json` and `/etc/apex-paper/feed-settings.json` using the reviewed examples and the host's known SIP configuration. Retain the existing root-owned credential files. From a clean checkout at the exact reviewed repair SHA:

```sh
sudo bash ops/install_paper.sh EXACT_REVIEWED_SHA
sudo bash /opt/apex-paper/releases/EXACT_REVIEWED_SHA/ops/activate_paper.sh EXACT_REVIEWED_SHA
```

The separate activation command avoids rebuilding an already installed release. It verifies installed ExecStart pins, starts the real publisher and account services once, checks current measured input, quote eligibility, publisher health, and an open-session account status, and verifies the paper account before enabling both timers. It intentionally refuses activation outside the session or on stale/blocked data. Inspect several subsequent real generations and service heartbeats; first-tick success is not proof of continuous operation or reboot recovery. Existing account state is never reset.

NumPy/SciPy installations require wheels, preventing the reported prolonged source build for those dependencies; an unsupported host fails installation explicitly. Other packages retain the existing pinned dependency workflow.

## Verification scope

Consumer tests use explicitly synthetic fixtures of the measured contract. They exercise the real tick_files, runtime, quote reader and paper book; they are not real-feed commissioning. Covered: actual pending order expiry, an open position across publisher failure, stale marks, swap between service reads, two fetched-calendar generations through service, generation reuse, overlapping writers, and private exception-text suppression.

Host SSH from this session returned `Network is unreachable`; no remote service activation or live paper trades are claimed. The unit parser check substitutes only ExecStart with `/usr/bin/true`; it proves syntax, not target-host identity, credentials, permissions, timer behavior or process execution. Original c79563d tests and docs remain upstream evidence; this document supersedes their deployment and failure-path claims.

The upstream fractional-time refusal exposed seconds-only synthetic fixtures in test_paper_book. Those fixtures now explicitly declare nanosecond stamps; their accounting, latency, stale-mark, recovery and expiry assertions remain intact. All 22 paper-book tests pass.

## Final validation record

Full run: **549 passed, 1 failed in 650.77s**. The sole failure was a seconds-only synthetic quarter-second receipt fixture under the new refusal contract. The fixture now declares nanoseconds, preserving every assertion; its targeted recheck passed in 27.68s. Application source remained unchanged. This is not reported as a clean 550-test full run. The host installer runs the full suite again before installation.

Focused publisher/service checks: 48 passed. Paper-book checks: 22 passed. Actual CLI replay and paper demos plus both independent verifiers passed. The no-auth publisher CLI recorded a sanitized failure and published no generation. Both unit/timer syntax checks and shell parsing passed within the scope above. See `evidence/publisher-repair-001/acceptance.json` and retained logs, including interrupted runs.
