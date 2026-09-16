#!/bin/bash
# Reusable commissioning for an installed release; no account reset or broker calls.
set -euo pipefail
test "$(id -u)" = 0
pin=${1:?Exact installed commit SHA required}
[[ "$pin" =~ ^[0-9a-f]{40}$ ]]
release="/opt/apex-paper/releases/$pin"
for unit in apex-paper apex-paper-feed; do
  systemctl show "$unit.service" --property=ExecStart --value | grep -F "$release/venv/bin/apex" >/dev/null
done
# The unit runs under its real identity, credentials, timeout, and filesystem permissions.
systemctl start apex-paper-feed.service
systemctl start apex-paper.service
"$release/venv/bin/python" - <<'PY'
import json, time
from pathlib import Path
r = json.loads(Path('/var/lib/apex-paper/service-health.json').read_text())
assert r.get('mode') == 'LIVE_PAPER'
assert r.get('source_problem') is None and r.get('quote_problem') is None
assert r.get('feed', {}).get('problem') is None
assert r.get('feed', {}).get('publisher_status') == 'PUBLISHED'
assert 0 <= r['feed']['generation_age_s'] <= 60
assert r.get('status') in {'WAIT', 'PAPER_ORDER_PENDING_LATER_QUOTE', 'MONITORING_BETWEEN_FORECASTS', 'ENTRY_WINDOW_CLOSED_EXIT_SERVICE_ACTIVE'}
assert r.get('account', {}).get('mode') == 'LIVE_PAPER'
PY
runuser -u apex-paper -- "$release/venv/bin/apex" verify-paper --root /var/lib/apex-paper
systemctl enable --now apex-paper-feed.timer apex-paper.timer
systemctl list-timers apex-paper-feed.timer apex-paper.timer --no-pager
echo 'Commissioning tick passed; timers enabled. Verify later heartbeat, fresh generations and paper-report.'
