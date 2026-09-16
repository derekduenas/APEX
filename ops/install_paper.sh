#!/bin/bash
# Install an immutable paper release beside existing APEXAI/shadow services.
# --activate requires a successful current measured-feed commissioning tick.
set -euo pipefail
umask 022
test "$(id -u)" = 0 || { echo 'Run as root on the target Linux host' >&2; exit 2; }
pin=${1:?Usage: bash ops/install_paper.sh FULL_COMMIT_SHA [--activate]}
activation=${2:-}
[[ "$pin" =~ ^[0-9a-f]{40}$ ]] || { echo 'An exact commit SHA is required' >&2; exit 2; }
[[ -z "$activation" || "$activation" = --activate ]] || { echo 'Unknown option' >&2; exit 2; }
checked_head=$(git rev-parse --verify HEAD)
checked_status=$(git status --porcelain)
test "$checked_head" = "$pin" && test -z "$checked_status" || { echo 'Expected a clean checkout at the requested pin' >&2; exit 2; }
python3 -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ required"'
command -v systemd-analyze >/dev/null
test -f /etc/apex-paper/settings.json || { echo 'Configure /etc/apex-paper/settings.json from the reviewed example' >&2; exit 2; }
if systemctl is-active --quiet apex-paper.service || systemctl is-active --quiet apex-paper.timer; then
  echo 'Stop only the existing apex-paper timer/service before changing its release' >&2
  exit 2
fi
release="/opt/apex-paper/releases/$pin"
test ! -e "$release" || { echo 'Release already exists; retained for inspection' >&2; exit 2; }
mkdir -p "$release"
git archive "$pin" | tar -x -C "$release"
python3 -m venv "$release/venv"
"$release/venv/bin/python" -m pip install -r "$release/requirements.lock" "$release"
"$release/venv/bin/python" -c 'from apex.paper_service import load_service_settings; load_service_settings("/etc/apex-paper/settings.json")'
( cd "$release"; "$release/venv/bin/python" -m pytest -q )
"$release/venv/bin/apex" demo --out "$release/acceptance-lifecycle"
"$release/venv/bin/apex" verify --run "$release/acceptance-lifecycle"
"$release/venv/bin/apex" paper-demo --out "$release/acceptance-paper"
"$release/venv/bin/apex" verify-paper --root "$release/acceptance-paper"
id apex-paper >/dev/null 2>&1 || useradd --system --home-dir /var/lib/apex-paper --shell /usr/sbin/nologin apex-paper
install -d -m 700 -o apex-paper -g apex-paper /var/lib/apex-paper
chown root:apex-paper /etc/apex-paper
chmod 750 /etc/apex-paper
chown root:apex-paper /etc/apex-paper/settings.json
chmod 640 /etc/apex-paper/settings.json
sed "s|@@RELEASE@@|$release|g" "$release/deploy/apex-paper.service.in" > "$release/deploy/apex-paper.service"
systemd-analyze verify "$release/deploy/apex-paper.service" "$release/deploy/apex-paper.timer"
install -m 644 "$release/deploy/apex-paper.service" /etc/systemd/system/apex-paper.service
install -m 644 "$release/deploy/apex-paper.timer" /etc/systemd/system/apex-paper.timer
systemctl daemon-reload
if test "$activation" = --activate; then
  test -r /var/lib/apex-paper-feed/input.json && test -r /var/lib/apex-paper-feed/session.json
  systemctl start apex-paper.service
  "$release/venv/bin/python" - <<'PY'
import json
from pathlib import Path
r = json.loads(Path('/var/lib/apex-paper/service-health.json').read_text())
assert r.get('mode') == 'LIVE_PAPER', 'LIVE paper mode required'
assert r.get('source_problem') is None and r.get('quote_problem') is None, 'Current measured data/quote required'
assert r.get('status') in {'WAIT','PAPER_ORDER_PENDING_LATER_QUOTE','MONITORING_BETWEEN_FORECASTS','ENTRY_WINDOW_CLOSED_EXIT_SERVICE_ACTIVE'}, 'Commission during the supplied market session'
assert r.get('account', {}).get('mode') == 'LIVE_PAPER', 'Verified paper account required'
PY
  systemctl enable --now apex-paper.timer
  echo 'Paper timer activated after a current measured-feed tick. Inspect journal and paper-report.'
else
  echo 'Paper release installed; timer inactive. Feed publication and a current commissioning tick are required for activation.'
fi
