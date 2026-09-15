"""Alpaca data credentials from systemd files or an explicit environment pair."""
import os
from pathlib import Path


def alpaca_credentials():
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if directory:
        # An explicitly configured systemd source never falls back to another
        # account in the environment when its files are missing or unreadable.
        try:
            values = tuple((Path(directory) / name).read_text().strip()
                           for name in ("alpaca_key", "alpaca_secret"))
        except (OSError, UnicodeError):
            return None, None
    else:
        values = (os.environ.get("APCA_API_KEY_ID"), os.environ.get("APCA_API_SECRET_KEY"))
    if any(not value or len(value) > 4096 or any(c.isspace() for c in value) for value in values):
        return None, None
    return values
