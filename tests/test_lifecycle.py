import copy
import json
import os
import subprocess
import sys
from decimal import Decimal

import numpy as np
import pytest

from apex.core import Config, Refused, canonical, digest
from apex.engine import run
from apex.fixtures import demo_document
from apex.ledger import read_verified, reconstruct
from apex.verification import verify_run


def flight(tmp_path, doc=None):
    original, start, end = demo_document()
    result = run(canonical(doc or original).encode(), tmp_path / "run", start=start, end=end, config=Config(variance="ewma"))
    return tmp_path / "run", result


def test_whole_flight_and_independent_money(tmp_path):
    root, result = flight(tmp_path)
    verified = verify_run(root)
    assert verified["status"] == "VALID" and result["counts"] == {"scans": 4, "forecasts": 4, "entries": 4, "exits": 4, "scored_forecasts": 4}
    rows = read_verified(root / "ledger.jsonl")
    seen, cash = {}, Decimal("10000.00")
    for row in rows:
        p = row["payload"]
        if row["kind"] in ("ENTRY", "EXIT"):
            q = seen[p["quote_ref"]]["payload"]
            amount = (Decimal(str(q["ask"] if row["kind"] == "ENTRY" else q["bid"])) * p["quantity"]).quantize(Decimal(".01"))
            commission = max(Decimal(".01"), Decimal(".005") * p["quantity"]).quantize(Decimal(".01"))
            cash += -amount - commission if row["kind"] == "ENTRY" else amount - commission
        seen[row["hash"]] = row
    assert verified["cash"] == str(cash)
    assert Decimal(verified["total_net_pnl"]) == cash - 10000
    # Exit is processed before the next candidate at a shared instant.
    for row in rows:
        if row["kind"] == "EXIT":
            assert not any(r["kind"] == "ENTRY" and r["epoch"] == row["epoch"] for r in rows[:row["seq"] - 1])


def test_real_quote_absence_keeps_models_running_but_refuses_trades(tmp_path):
    doc, _, _ = demo_document(quotes=False)
    root, result = flight(tmp_path, doc)
    assert result["counts"]["forecasts"] == 4 and result["counts"]["entries"] == 0
    assert result["decisions"] == {"QUOTE_UNAVAILABLE_OR_CONFLICTING": 4}
    assert verify_run(root)["total_net_pnl"] == "0.00"


def test_missing_exit_retains_exposure_and_blocks_new_entries(tmp_path):
    doc, start, _ = demo_document()
    doc["observations"] = [r for r in doc["observations"] if r["kind"] != "quote" or r["event_epoch"] <= start]
    root, result = flight(tmp_path, doc)
    assert result["counts"]["entries"] == 1 and result["counts"]["exits"] == 0
    assert result["accounting"]["total_net_pnl"] is None
    assert len(verify_run(root)["open_exposure"]) == 1


def test_negative_drift_wait_is_legitimate(tmp_path):
    doc, _, _ = demo_document(drift=-.0003)
    _, result = flight(tmp_path, doc)
    assert result["counts"]["forecasts"] == 4 and result["counts"]["entries"] == 0
    assert "AFTER_COST_MODEL_NOT_ELIGIBLE" in result["decisions"]


def test_fresh_receipt_with_old_quote_cannot_close_position(tmp_path):
    doc, start, end = demo_document()
    doc["observations"] = [r for r in doc["observations"] if r["kind"] != "quote" or r["event_epoch"] <= start]
    due = start + 900
    doc["observations"].append({"kind": "quote", "symbol": "SPY", "event_epoch": due - 40, "available_epoch": due,
                                "availability_basis": "MEASURED_RECEIPT", "bid": 120, "ask": 121, "bid_size": 100, "ask_size": 100})
    root, result = flight(tmp_path, doc)
    assert result["counts"]["entries"] == 1 and result["counts"]["exits"] == 0
    assert verify_run(root)["total_net_pnl"] is None


def test_permutation_does_not_change_model_or_execution(tmp_path):
    doc, start, end = demo_document()
    first, _ = flight(tmp_path, doc)
    doc["observations"].reverse()
    second = tmp_path / "permuted"
    run(canonical(doc).encode(), second, start=start, end=end, config=Config(variance="ewma"))
    # Capture digests and therefore ledger hashes differ; economic inputs and
    # forecasts must not. Compare values, not references tied to the manifest.
    left, right = read_verified(first / "ledger.jsonl"), read_verified(second / "ledger.jsonl")
    assert [r["payload"] for r in left if r["kind"] == "FORECAST"] == [r["payload"] for r in right if r["kind"] == "FORECAST"]
    assert [r["payload"]["cash_after"] for r in left if r["kind"] == "EXIT"] == [r["payload"]["cash_after"] for r in right if r["kind"] == "EXIT"]


def test_collision_preserves_prior_run_and_bad_input_preserves_failure(tmp_path):
    root, _ = flight(tmp_path)
    head = (root / "COMPLETE").read_text()
    doc, start, end = demo_document()
    with pytest.raises(FileExistsError):
        run(canonical(doc).encode(), root, start=start, end=end, config=Config())
    assert (root / "COMPLETE").read_text() == head
    with pytest.raises(json.JSONDecodeError):
        run(b"bad json", tmp_path / "bad", start=start, end=end, config=Config())
    assert (tmp_path / "bad" / "FAILED.json").exists()


@pytest.mark.parametrize("artifact", ["input.json", "manifest.json", "summary.json", "ledger.jsonl"])
def test_artifact_tampering_detected(tmp_path, artifact):
    root, _ = flight(tmp_path)
    path = root / artifact
    if artifact == "ledger.jsonl":
        path.write_text(path.read_text().replace('"cash_after":"', '"cash_after":"9', 1))
    else:
        body = json.loads(path.read_text())
        if artifact == "summary.json":
            body["accounting"]["cash"] = "999999.00"
        else:
            body["tampered"] = True
        path.write_text(canonical(body))
    with pytest.raises(Refused):
        verify_run(root)


def test_path_tampering_detected(tmp_path):
    root, _ = flight(tmp_path)
    path = next(root.glob("*.npy"))
    values = np.load(path)
    values[0, 1] += 1
    np.save(path, values)
    with pytest.raises(Refused, match="SIMULATION_ARTIFACT_CHANGED"):
        verify_run(root)


def test_rehashed_bad_cash_detected_by_accounting(tmp_path):
    root, _ = flight(tmp_path)
    rows = read_verified(root / "ledger.jsonl")
    # Change only the last exit, so downstream references need no rewriting.
    target = [r for r in rows if r["kind"] == "EXIT"][-1]
    target["payload"]["cash_after"] = "999999.00"
    prev = "GENESIS"
    for row in rows:
        row.pop("hash")
        row["prev_hash"] = prev
        row["hash"] = digest(row)
        prev = row["hash"]
    (root / "ledger.jsonl").write_text("\n".join(canonical(r) for r in rows) + "\n")
    result = reconstruct(root / "ledger.jsonl")
    assert result["status"] == "MISMATCH" and result["cash"] is None
    assert any("ACCOUNTING_DISAGREEMENT" in p for p in result["problems"])


def test_cli_runs_and_independently_verifies(tmp_path):
    env = {**os.environ, "PYTHONPATH": "src"}
    output = tmp_path / "cli"
    subprocess.run([sys.executable, "-m", "apex.cli", "demo", "--variance", "ewma", "--out", str(output)], env=env, check=True, capture_output=True)
    result = subprocess.run([sys.executable, "-m", "apex.cli", "verify", "--run", str(output)], env=env, check=True, capture_output=True, text=True)
    assert json.loads(result.stdout)["verified_forecasts"] == 4
