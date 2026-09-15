import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from apex.core import Refused
from apex.credentials import alpaca_credentials
from apex.runtime import load_settings, report, report_text, runtime_lock, tick
from test_capture import CaptureFixture


SETTINGS = {"symbol": "SPY", "feed": "sip", "round_lot_shares": 100, "history_days": 7,
            "max_pages": 3, "request_timeout": 5, "variance": "ewma", "min_free_bytes": 64 * 1024**2}


def test_runtime_real_readers_and_daily_report(tmp_path):
    f = CaptureFixture()
    result = tick(tmp_path, SETTINGS, transport=f.transport, clock_ns=f.clock)
    assert result["status"] == "SHADOW_OBSERVED"
    assert result["comparison"] == "AGREEMENT"
    assert result["forecast"]["paths"] == 1000
    assert result["candidate"]["decision"] == "EXPERIMENTAL_LONG"
    assert result["capture_class"] == "SYNTHETIC_ACCEPTANCE"
    view = report(tmp_path, day="2026-09-11", now=f.ns / 1e9)
    assert view["candidate_observations"] == {"EXPERIMENTAL_LONG": 1}
    assert view["orders"] == view["fills"] == 0 and view["pnl"] is None
    assert "SYNTHETIC_ACCEPTANCE" in report_text(view)
    assert report(tmp_path, day="2026-09-11", now=f.ns / 1e9 + 301)["status"] == "STALE_RUNTIME"
    assert not list(tmp_path.rglob("ledger.jsonl"))


def test_failed_provider_is_degraded_and_no_simulated_trade_in_report(tmp_path):
    f = CaptureFixture()
    result = tick(tmp_path, SETTINGS, clock_ns=f.clock,
                  transport=lambda *a: {"status": 0, "error": "BLOCKED_EXTERNAL_CREDENTIAL"})
    assert result["status"] == "DEGRADED" and result["comparison"] == "REFUSAL_PARITY_ONLY"
    assert result["forecast"] is None and result["candidate"] is None
    assert report(tmp_path, day="2026-09-11", now=f.start)["candidate_observations"] == {}


def test_off_hours_and_low_disk_make_no_requests(tmp_path, monkeypatch):
    from collections import namedtuple
    f = CaptureFixture()
    def forbidden(*args):
        pytest.fail("An idle/blocked runtime must not request data")
    result = tick(tmp_path / "night", SETTINGS, transport=forbidden, clock_ns=lambda: int((f.start - 3600) * 1e9))
    assert result["status"] == "IDLE_OUTSIDE_REGULAR_CLOCK_WINDOW"
    disk = namedtuple("Disk", "total used free")
    monkeypatch.setattr("apex.runtime.shutil.disk_usage", lambda p: disk(100, 99, 1))
    assert tick(tmp_path / "disk", SETTINGS, transport=forbidden, clock_ns=f.clock)["status"] == "BLOCKED_DISK_RESERVE"


def test_real_process_lock_death_and_recovery(tmp_path):
    config = tmp_path / "settings.json"
    config.write_text(json.dumps(SETTINGS))
    root = tmp_path / "state"
    entered = tmp_path / "entered"
    program = '''
import sys,time
from pathlib import Path
from apex.runtime import load_settings,tick
def hung(*args):
    Path(sys.argv[3]).write_text('entered')
    time.sleep(60)
tick(Path(sys.argv[1]),load_settings(Path(sys.argv[2])),transport=hung,clock_ns=lambda:1789133700000000000)
'''
    child = subprocess.Popen([sys.executable, "-c", program, str(root), str(config), str(entered)],
                             env={**os.environ, "PYTHONPATH": "src"}, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 5
        while not entered.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        assert entered.exists(), child.poll()
        with pytest.raises(Refused, match="RUNTIME_ALREADY_ACTIVE"):
            with runtime_lock(root):
                pass
        first = next((root / "ticks" / "2026-09-11").iterdir())
        original = (first / "started.json").read_bytes()
        child.kill()
        child.wait(timeout=5)
        assert not (first / "finished.json").exists()
        f = CaptureFixture()
        result = tick(root, SETTINGS, transport=f.transport, clock_ns=f.clock)
        assert result["status"] == "SHADOW_OBSERVED"
        assert json.loads((first / "finished.json").read_text())["status"] == "INTERRUPTED"
        assert (first / "started.json").read_bytes() == original
        assert len(list((root / "ticks" / "2026-09-11").iterdir())) == 2
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_config_rejects_example_and_bad_budget(tmp_path):
    with pytest.raises(Refused, match="LOT_SIZE"):
        load_settings(Path("deploy/settings.example.json"))
    p = tmp_path / "config.json"
    for key, value in (("max_pages", 99), ("feed", "iex"), ("min_free_bytes", 0), ("history_days", 20)):
        p.write_text(json.dumps({**SETTINGS, key: value}))
        with pytest.raises(Refused):
            load_settings(p)


def test_systemd_credentials_take_precedence_and_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("APCA_API_KEY_ID", "oldkey")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "oldsecret")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    assert alpaca_credentials() == (None, None)
    (tmp_path / "alpaca_key").write_text("filekey\n")
    (tmp_path / "alpaca_secret").write_text("filesecret\n")
    assert alpaca_credentials() == ("filekey", "filesecret")
    # Exercise the actual HTTP worker with file credentials and a fake socket
    # opener that checks headers, without returning the credentials as output.
    program = '''
import io,json,sys
from apex import http_worker
class Response:
    status=200
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def read(self,n): return b'{"bars":{}}'
class Opener:
    def open(self,request,timeout):
        assert request.get_header('Apca-api-key-id')=='filekey'
        assert request.get_header('Apca-api-secret-key')=='filesecret'
        return Response()
http_worker.build_opener=lambda *args:Opener()
sys.stdin=io.StringIO(json.dumps({'path':'/v2/stocks/bars/latest','params':{'symbols':'SPY','feed':'sip'},'timeout':5}))
http_worker.main()
'''
    r = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": "src"})
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["status"] == 200
    assert "filekey" not in r.stdout + r.stderr and "filesecret" not in r.stdout + r.stderr


def test_real_cli_idle_and_read_only_report(tmp_path):
    r = subprocess.run([sys.executable, "-m", "apex.cli", "shadow-report", "--root", str(tmp_path), "--day", "2026-09-11", "--format", "json"],
                       capture_output=True, text=True, env={**os.environ, "PYTHONPATH": "src"})
    assert r.returncode == 0 and json.loads(r.stdout)["status"] == "NO_RUNS"
    assert list(tmp_path.iterdir()) == []
