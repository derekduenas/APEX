"""Execute installer refusals before any dependency or host modification."""
import os
from pathlib import Path
import subprocess

import pytest


PIN = "b" * 40
INSTALLER = Path(__file__).parents[1] / "ops/install_digitalocean.sh"


@pytest.mark.parametrize("mode,expected", [
    ("head_error", "Cannot verify checkout identity"),
    ("head_mismatch", "Checkout does not match"),
    ("status_error", "Cannot verify checkout cleanliness"),
    ("dirty", "Checkout must be clean"),
])
def test_git_refusals_stop_before_setup(tmp_path, mode, expected):
    # Only commands before the refusal are substituted. The real installer
    # shell controls exit handling. No credential path or service is accessed.
    programs = {
        "id": "echo 0\n",
        "git": '''
if [ "$1" = rev-parse ]; then
  [ "$TEST_MODE" = head_error ] && exit 128
  if [ "$TEST_MODE" = head_mismatch ]; then echo aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa; else echo "$TEST_PIN"; fi
  exit 0
fi
if [ "$1" = status ]; then
  [ "$TEST_MODE" = status_error ] && exit 128
  [ "$TEST_MODE" = dirty ] && echo ' M tracked.py'
  exit 0
fi
exit 99
''',
        "python3": "echo UNEXPECTED_SETUP_REACHED\nexit 92\n",
    }
    for name, body in programs.items():
        p = tmp_path / name
        p.write_text("#!/bin/sh\n" + body)
        p.chmod(0o755)
    result = subprocess.run(["/bin/bash", str(INSTALLER), PIN], text=True, capture_output=True,
                            env={**os.environ, "PATH": str(tmp_path), "TEST_MODE": mode, "TEST_PIN": PIN})
    assert result.returncode == 2
    assert expected in result.stderr
    assert "UNEXPECTED_SETUP_REACHED" not in result.stdout
