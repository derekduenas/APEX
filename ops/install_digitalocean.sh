#!/bin/bash
# Install a pinned, separate APEX shadow release. Does not activate its timer.
set -euo pipefail
umask 022
test "$(id -u)" = 0 || { echo 'Run as root on the target Linux host' >&2; exit 2; }
pin=${1:?Usage: bash ops/install_digitalocean.sh FULL_COMMIT_SHA}
[[ "$pin" =~ ^[0-9a-f]{40}$ ]] || { echo 'An exact commit SHA is required' >&2; exit 2; }
test "$(git rev-parse HEAD)" = "$pin" || { echo 'Checkout does not match requested release' >&2; exit 2; }
test -z "$(git status --porcelain)" || { echo 'Checkout must be clean' >&2; exit 2; }
python3 -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ required"'
command -v systemd-analyze >/dev/null
test -f /etc/apex-shadow/settings.json
test "$(stat -c '%a:%U' /etc/apex-shadow/credentials)" = '700:root' || { echo 'Credentials directory must be root-owned mode 700' >&2; exit 2; }
for credential in alpaca_key alpaca_secret; do
  path="/etc/apex-shadow/credentials/$credential"
  test -s "$path" || { echo "Missing credential file: $credential" >&2; exit 2; }
  test "$(stat -c '%a:%U' "$path")" = '600:root' || { echo "Credential must be root-owned mode 600: $credential" >&2; exit 2; }
done
if systemctl is-active --quiet apex-shadow.service || systemctl is-active --quiet apex-shadow.timer; then
  echo 'Stop this APEX shadow timer and service before changing its release' >&2
  exit 2
fi
release="/opt/apex-shadow/releases/$pin"
test ! -e "$release" || { echo 'Release directory already exists; preserved for inspection' >&2; exit 2; }
mkdir -p "$release"
git archive "$pin" | tar -x -C "$release"
python3 -m venv "$release/venv"
"$release/venv/bin/python" -m pip install -r "$release/requirements.lock" "$release"
"$release/venv/bin/python" -c 'from apex.runtime import load_settings; from pathlib import Path; load_settings(Path("/etc/apex-shadow/settings.json"))'
( cd "$release"; "$release/venv/bin/python" -m pytest -q )
"$release/venv/bin/apex" demo --out "$release/acceptance-lifecycle"
"$release/venv/bin/apex" verify --run "$release/acceptance-lifecycle"
"$release/venv/bin/apex" demo-capture --out "$release/acceptance-capture"
"$release/venv/bin/apex" verify-capture --capture "$release/acceptance-capture"
id apex-shadow >/dev/null 2>&1 || useradd --system --home-dir /var/lib/apex-shadow --shell /usr/sbin/nologin apex-shadow
chown root:apex-shadow /etc/apex-shadow
chmod 750 /etc/apex-shadow
install -d -m 700 -o apex-shadow -g apex-shadow /var/lib/apex-shadow
chown root:apex-shadow /etc/apex-shadow/settings.json
chmod 640 /etc/apex-shadow/settings.json
sed "s|@@RELEASE@@|$release|g" "$release/deploy/apex-shadow.service.in" > "$release/deploy/apex-shadow.service"
systemd-analyze verify "$release/deploy/apex-shadow.service" "$release/deploy/apex-shadow.timer"
install -m 644 "$release/deploy/apex-shadow.service" /etc/systemd/system/apex-shadow.service
install -m 644 "$release/deploy/apex-shadow.timer" /etc/systemd/system/apex-shadow.timer
systemctl daemon-reload
printf 'Installed release %s. Timer remains inactive.\n' "$pin"
printf 'Commission: systemctl start apex-shadow.service\n'
printf 'Inspect: journalctl -u apex-shadow.service --no-pager -n 80\n'
printf 'Report: %s/venv/bin/apex shadow-report --root /var/lib/apex-shadow\n' "$release"
printf 'After inspecting actual capture: systemctl enable --now apex-shadow.timer\n'
