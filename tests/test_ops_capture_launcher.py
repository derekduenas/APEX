"""The macOS launcher bridges existing Keychain secrets without exposing them."""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]
LAUNCHER = ROOT / "ops" / "apex_shadow_capture.sh"


def _program(path: Path, source: str):
    path.write_text("#!/bin/sh\n" + source)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_keychain_launcher_forwards_capture_arguments_without_echoing_secrets(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _program(fake_bin / "security", 'printf "%s" "test-secret-$5"\n')
    _program(fake_bin / "python", 'printf "args=%s key=%s secret=%s\\n" "$*" "${APCA_API_KEY_ID:+set}" "${APCA_API_SECRET_KEY:+set}"\n')
    env = {"PATH": str(fake_bin), "HOME": str(tmp_path)}
    result = subprocess.run([str(LAUNCHER), "--out", "runs/unique", "--history-start", "2026-09-15T13:30:00Z",
                             "--feed", "sip", "--round-lot-shares", "100"], text=True, capture_output=True, env=env)
    assert result.returncode == 0
    assert "capture-alpaca --out runs/unique" in result.stdout
    assert "key=set secret=set" in result.stdout
    assert "test-secret" not in result.stdout + result.stderr


def test_keychain_launcher_refuses_when_security_is_not_available(tmp_path):
    result = subprocess.run([str(LAUNCHER)], text=True, capture_output=True,
                            env={"PATH": str(tmp_path), "HOME": str(tmp_path)})
    assert result.returncode == 2
    assert "Keychain" in result.stderr
