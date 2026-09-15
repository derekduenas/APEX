#!/bin/sh
# Run APEX's bounded, read-only Alpaca shadow capture using the operator's
# existing macOS Keychain entries. This launcher never prints credentials.
set -eu

if ! command -v security >/dev/null 2>&1; then
  echo "REFUSED: macOS Keychain command 'security' is unavailable" >&2
  exit 2
fi

APCA_API_KEY_ID="$(security find-generic-password -a apex -s ALPACA_API_KEY_ID -w)"
APCA_API_SECRET_KEY="$(security find-generic-password -a apex -s ALPACA_API_SECRET_KEY -w)"
export APCA_API_KEY_ID APCA_API_SECRET_KEY

# The caller supplies the explicit bounded capture arguments, including a
# unique output directory. No credential value is written to an argument,
# artifact, log or terminal output by this launcher.
exec python -m apex.cli capture-alpaca "$@"
