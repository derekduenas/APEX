# APEX Linux shadow runtime — v0.3

Target: a DigitalOcean Ubuntu 24.04 droplet with Python 3.11+ and systemd 255. Deployment is separate from existing APEXAI services, under `/opt/apex-shadow`, `/etc/apex-shadow` and `/var/lib/apex-shadow`. The Mac is optional. No DigitalOcean API token, SSH agent/configuration or cloud connector was available during this build, so **no droplet installation or live service operation is claimed**.

## What is implemented

The systemd timer invokes one `apex shadow-tick` sixty seconds after the preceding invocation finishes. The service has a ninety-second start timeout (including synchronous model fitting/replay), a five-second stop deadline, and control-group termination. It is `Type=oneshot`; its total work is governed by `TimeoutStartSec`, not `RuntimeMaxSec`. The timer starts again after a reboot. There is no claim of replaying missed market sessions: late starts capture what is actually available when they run.

Each tick takes a kernel file lock, preserves a unique run directory, invokes the actual v0.2 capture → Twin → GARCH/EWMA → simulated paths → stock/WAIT evaluator, and verifies capture/replay agreement. A previous process that died without its terminal record is marked INTERRUPTED after the next process acquires the lock. Existing captures stay intact. A low-disk reserve stops new capture rather than deleting old evidence. Recurring ticks fetch bounded REST snapshots/history; **they do not subscribe to the existing SIP WebSocket collector**. The old collector should remain running. A future bridge must verify its deployed schema and receipt semantics before it replaces this input.

`shadow-report` rebuilds a dated report from tick records, including model attempts, training-return counts, path counts, candidate reasons and source health. A latest tick more than 300 seconds old reports STALE_RUNTIME. Historical reports evaluated later will also be stale by that operational measure. Candidates are observations, not orders or a portfolio; no P&L is fabricated. The detailed capture remains the source for quote/bar ages and input IDs. Per-capture replay verification is enforced; the operational report is not an independent authentication of host files.

Normal trading hours are currently a New York weekday clock filter. Holidays/early closes are **not** implemented: stale underlying/quote checks still refuse invalid inputs, but the runtime may request data on a holiday. This must be resolved before live execution. Each tick rebuilds history; quotas, actual memory and CPU costs must be measured on the host. Defaults request at most five REST responses per cycle and do not claim full 164-symbol coverage. SPY is the first commissioned symbol. Repeated candidates never reserve funds or alter positions.

## Install on the actual droplet

1. Obtain the pinned APEX checkout using the host's authorized GitHub access. Do not copy Mac Keychain files. Use the droplet's existing secret provisioning mechanism to create `/etc/apex-shadow/credentials/alpaca_key` and `alpaca_secret`, each root-owned mode 600, in a mode-700 credentials directory. Values are not command arguments, Git files or journal output. Systemd `LoadCredential` supplies read-only copies to the data worker. If that configured source fails, the worker refuses rather than switching to a different environment account.
2. Create `/etc/apex-shadow/settings.json` from `deploy/settings.example.json`. Set the quote round-lot share count for SPY after checking the source contract. The shipped placeholder intentionally refuses; a test fixture's value is not live unit verification. Keep `feed=sip`, `variance=garch`, and a seven-day history window to supply prior sessions. Settings contain no secrets. Ensure `/etc/apex-shadow` is traversable by the `apex-shadow` service user; the credential subdirectory remains root-only.
3. From the clean pinned checkout, run as root:

```bash
bash ops/install_digitalocean.sh "$(git rev-parse HEAD)"
```

The installer creates a release-specific virtual environment, checks settings, runs the full tests and both synthetic CLI controls, verifies unit syntax, and installs the inactive service/timer. Existing APEXAI units and secrets are untouched. It refuses to replace an active APEX shadow service/timer. For an upgrade, stop those two units first; roll back by restoring the previous unit's absolute release path, verifying it and reloading systemd. Old releases/evidence are not automatically pruned.

4. During a regular market session, commission one real invocation:

```bash
systemctl start apex-shadow.service
journalctl -u apex-shadow.service --no-pager -n 80
/opt/apex-shadow/releases/EXACT_SHA/venv/bin/apex shadow-report --root /var/lib/apex-shadow
```

The service returns nonzero on DEGRADED/blocked results while retaining evidence. Inspect the actual capture class, quote event age, bar completion age, rejected rows/gaps, history completeness, models used and replay comparison. Off-hours IDLE is scheduler progress only. Missing credentials may be rejected by systemd before Python starts; inspect the journal, not just report files. No successful run is assumed from an enabled timer.

5. When that real capture passes, enable recurrence:

```bash
systemctl enable --now apex-shadow.timer
systemctl list-timers apex-shadow.timer
```

Verify several real invocations, then a restart with retained evidence. Host backup/encryption and an external monitor for stale reports/disk pressure remain operations work; a report on a dead machine cannot alert you itself. Do not enable paper orders from this result: account semantics, calibrated evidence and restart-safe trade servicing are separate work.

## Evidence and limits

The runtime tests use the production capture and model readers with substituted provider transport/clock, labeled SYNTHETIC_ACCEPTANCE. A real child process is killed during a hung request, the competing lock refuses, the next process recovers the interrupted tick, and original bytes survive. Credential tests exercise the real HTTP worker's headers using systemd-shaped credential files and a substituted network opener; they do not prove host provisioning. Unit parsing can be checked here; PID-1 scheduling, cgroup termination, permissions and boot behavior need the actual droplet.

Executed: **49 tests passed**; the existing synthetic CLI lifecycle and independent accounting verification passed. The runtime control fitted GARCH11_T, generated 1,000 paths from 393 returns, produced one experimental long-stock candidate and verified capture/replay agreement. Its subsequent real-clock report correctly marks that historical fixture STALE_RUNTIME. Evidence: [tests](evidence/linux-runtime/tests.xml), [daily report](evidence/linux-runtime/synthetic-report.txt), [structured report](evidence/linux-runtime/synthetic-report.json), [accounting](evidence/linux-runtime/synthetic-accounting.json), and [unit parser scope/result](evidence/linux-runtime/unit-parser.txt). The unit check substituted only ExecStart with `/usr/bin/true` because the absolute release executable is not installed here; it proves syntax, not execution or scheduling.

Earlier messages inferred a DigitalOcean collector and then a Mac collector from incomplete evidence. The supplied operator inventory reports a healthy SIP fabric; inspected source proves a Mac launcher exists. **Neither independently establishes today's live host or deployment pin.** This package deliberately makes the DigitalOcean target explicit rather than treating either inference as established.
