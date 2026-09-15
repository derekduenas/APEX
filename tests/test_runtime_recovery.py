import json
import os
import subprocess
import sys

import pytest

from apex.core import Refused
from apex.runtime import report, tick
from test_capture import CaptureFixture
from test_runtime import SETTINGS


def _idle_tick(root):
    fixture = CaptureFixture()
    now_ns = int((fixture.start - 3600) * 1e9)

    def forbidden(*args):
        pytest.fail("Recovery must not make an off-hours provider request")

    result = tick(root, SETTINGS, transport=forbidden, clock_ns=lambda: now_ns)
    return result, now_ns


def test_torn_terminal_recovery_preserves_bytes_and_report_is_unavailable(tmp_path):
    first, now_ns = _idle_tick(tmp_path)
    prior = tmp_path / first["run"]
    (prior / "finished.json").write_bytes(b"{")
    (tmp_path / "active.json").write_text(json.dumps({"run": first["run"]}))
    second, _ = _idle_tick(tmp_path)

    assert second["run"] != first["run"]
    assert (prior / "finished.json").read_bytes() == b"{"
    recovery = json.loads((prior / "recovery.json").read_bytes())
    assert recovery["status"] == "UNAVAILABLE_RUNTIME_EVIDENCE"
    assert recovery["reason"] == "FINISHED_RECORD_UNAVAILABLE"
    assert "finished_ns" not in recovery
    view = report(tmp_path, day="2026-09-11", now=now_ns / 1e9)
    assert view["status"] == "UNAVAILABLE_RUNTIME_EVIDENCE"
    assert view["ticks"] == {"UNAVAILABLE_RUNTIME_EVIDENCE": 1, "IDLE_OUTSIDE_REGULAR_CLOCK_WINDOW": 1}
    assert view["unavailable_runs"][0]["run"] == first["run"]
    assert view["candidate_observations"] == {}
    assert view["orders"] == view["fills"] == 0 and view["pnl"] is None


@pytest.mark.parametrize("record, contents", [
    ("finished.json", b"{"),
    ("finished.json", b"[]"),
    ("finished.json", b"{}"),
    ("finished.json", b'{"status":"SHADOW_OBSERVED","finished_ns":1789133700000000000,"candidate":[]}'),
    ("finished.json", b'{"status":"SHADOW_OBSERVED","finished_ns":1789133700000000000,"forecast":{"models":[{}]}}'),
    ("finished.json", b'{"status":"SHADOW_OBSERVED","finished_ns":1789133700000000000,"extra":NaN}'),
    ("started.json", b"{"),
    ("started.json", b'{"started_ns":"unknown"}'),
    ("started.json", None),
])
def test_cli_report_names_bad_record_without_modifying_evidence(tmp_path, record, contents):
    first, _ = _idle_tick(tmp_path)
    path = tmp_path / first["run"] / record
    if contents is None:
        path.unlink()
    else:
        path.write_bytes(contents)
    before = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    command = [sys.executable, "-m", "apex.cli", "shadow-report", "--root", str(tmp_path), "--day", "2026-09-11"]
    for output_format in ("json", "text"):
        result = subprocess.run(command + ["--format", output_format], capture_output=True, text=True,
                                env={**os.environ, "PYTHONPATH": "src"})
        assert result.returncode == 0, result.stderr
        assert "UNAVAILABLE_RUNTIME_EVIDENCE" in result.stdout
        if output_format == "json":
            value = json.loads(result.stdout)
            assert value["latest"] is None
            assert value["unavailable_runs"][0]["issues"][0]["record"] == record
    assert before == {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


@pytest.mark.parametrize("contents", [b"{", b"[]", b"{}", b'{"run":null}'])
def test_corrupt_active_pointer_fails_closed_and_preserves_bytes(tmp_path, contents):
    active = tmp_path / "active.json"
    active.write_bytes(contents)
    with pytest.raises(Refused, match="ACTIVE_RUN_RECORD_UNAVAILABLE"):
        _idle_tick(tmp_path)
    assert active.read_bytes() == contents
    assert not (tmp_path / "ticks").exists()


@pytest.mark.parametrize("relative", [".", "../outside", "/tmp", "ticks/2026-09-11/missing"])
def test_invalid_active_run_path_is_named_and_retained(tmp_path, relative):
    active = tmp_path / "active.json"
    original = json.dumps({"run": relative}).encode()
    active.write_bytes(original)
    with pytest.raises(Refused, match="ACTIVE_RUN_PATH_INVALID"):
        _idle_tick(tmp_path)
    assert active.read_bytes() == original
    assert not (tmp_path / "finished.json").exists()


def test_terminal_publication_retains_torn_temporary_and_records_failure(tmp_path, monkeypatch):
    import apex.runtime as runtime

    original_write = runtime._write
    torn_paths = []

    def interrupted_write(path, value):
        if path.name.startswith("finished.json.") and not torn_paths:
            path.write_bytes(b"{")
            torn_paths.append(path)
            raise OSError("synthetic write interruption")
        return original_write(path, value)

    monkeypatch.setattr(runtime, "_write", interrupted_write)
    with pytest.raises(OSError, match="synthetic write interruption"):
        _idle_tick(tmp_path)
    assert torn_paths[0].read_bytes() == b"{"
    terminal = next(tmp_path.glob("ticks/*/*/finished.json"))
    assert json.loads(terminal.read_bytes())["status"] == "FAILED"
    assert not (tmp_path / "latest.json").exists()
