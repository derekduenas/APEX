"""Independent reader failures, using completed real engine runs as evidence."""
import json
from decimal import Decimal

import pytest

from apex.core import Config, Refused, canonical, digest, money
from apex.data import normalize
from apex.engine import run
from apex.fixtures import demo_document
from apex.ledger import read_verified, reconstruct
from apex.reused.contracts import digest as model_digest
from apex.verification import verify_run


def flight(tmp_path):
    document, start, end = demo_document()
    root = tmp_path / "run"
    run(canonical(document).encode(), root, start=start, end=end, config=Config(variance="ewma"))
    return root


def rewrite(root, rows):
    """Preserve an intact chain and all row references after a semantic mutation.

    Hash integrity alone must not make inconsistent economics or causality valid.
    """
    remap = {}

    def replace(value):
        if isinstance(value, str):
            return remap.get(value, value)
        if isinstance(value, dict):
            return {k: replace(v) for k, v in value.items()}
        if isinstance(value, list):
            return [replace(v) for v in value]
        return value

    previous = "GENESIS"
    for seq, row in enumerate(rows, 1):
        old = row.pop("hash")
        row.update(seq=seq, prev_hash=previous, payload=replace(row["payload"]))
        row["hash"] = digest(row)
        remap[old] = previous = row["hash"]
    (root / "ledger.jsonl").write_text("".join(canonical(r) + "\n" for r in rows))
    (root / "COMPLETE").write_text(previous)
    saved = json.loads((root / "summary.json").read_text())
    saved["accounting"] = reconstruct(root / "ledger.jsonl")
    (root / "summary.json").write_text(canonical(saved))


@pytest.mark.parametrize("field,value", [("mean_brier", -123), ("count", 999), ("baseline_mean_brier", 0)])
def test_feedback_report_is_rebuilt_from_scores(tmp_path, field, value):
    root = flight(tmp_path)
    saved = json.loads((root / "summary.json").read_text())
    saved["forecast_feedback"][field] = value
    (root / "summary.json").write_text(canonical(saved))
    with pytest.raises(Refused, match="SAVED_FORECAST_FEEDBACK_DISAGREES"):
        verify_run(root)


def test_twin_fields_are_reconstructed_from_captured_bars(tmp_path):
    root = flight(tmp_path)
    rows = read_verified(root / "ledger.jsonl")
    snapshot = next(r["payload"] for r in rows if r["kind"] == "TWIN")
    snapshot["fields"]["spot"] = 999999
    snapshot["snapshot_id"] = digest({k: v for k, v in snapshot.items() if k != "snapshot_id"})
    rewrite(root, rows)
    with pytest.raises(Refused, match="TWIN_INPUT_BINDING_INVALID"):
        verify_run(root)


@pytest.mark.parametrize("change", ["missing_quote", "ineligible", "wrong_quote", "late_fill"])
def test_accounting_refuses_execution_detached_from_candidate(tmp_path, change):
    root = flight(tmp_path)
    rows = read_verified(root / "ledger.jsonl")
    candidate = next(r for r in rows if r["kind"] == "CANDIDATE")
    entry = next(r for r in rows if r["kind"] == "ENTRY")
    if change == "missing_quote":
        candidate["payload"]["quote_ref"] = "MISSING_QUOTE"
    elif change == "ineligible":
        candidate["payload"].update(expected_net=-10000, model_probability_net_positive=0, reason="AFTER_COST_MODEL_NOT_ELIGIBLE")
    elif change == "wrong_quote":
        # The quote is real and fresh; it is simply a different evidence row.
        duplicate = dict(next(r for r in rows if r["kind"] == "QUOTE"))
        duplicate["hash"] = "extra-quote"
        rows.insert(rows.index(entry), duplicate)
        entry["payload"]["quote_ref"] = "extra-quote"
    else:
        entry["epoch"] += 1
    rewrite(root, rows)
    result = reconstruct(root / "ledger.jsonl")
    assert result["status"] == "MISMATCH" and result["cash"] is None
    with pytest.raises(Refused, match="ACCOUNTING_RECONSTRUCTION_FAILED"):
        verify_run(root)


def test_candidate_cost_estimate_is_recomputed_from_paths_and_cash(tmp_path):
    root = flight(tmp_path)
    rows = read_verified(root / "ledger.jsonl")
    candidate = next(r["payload"] for r in rows if r["kind"] == "CANDIDATE")
    candidate["expected_net"] += 123
    rewrite(root, rows)
    assert reconstruct(root / "ledger.jsonl")["status"] == "VALID"
    with pytest.raises(Refused, match="CANDIDATE_BEHAVIOR_DISAGREES"):
        verify_run(root)


@pytest.mark.parametrize("field", ["interval_covered", "label_available_epoch", "target_epoch"])
def test_score_coverage_and_time_metadata_are_checked(tmp_path, field):
    root = flight(tmp_path)
    rows = read_verified(root / "ledger.jsonl")
    score = next(r["payload"] for r in rows if r["kind"] == "FORECAST_SCORE")
    score[field] = not score[field] if field == "interval_covered" else score[field] + 60
    rewrite(root, rows)
    with pytest.raises(Refused, match="SCORE_DISAGREES|LABEL_NOT_MATURE"):
        verify_run(root)


def test_torn_ledger_is_a_named_refusal(tmp_path):
    path = tmp_path / "ledger.jsonl"
    path.write_text('{"seq":')
    with pytest.raises(Refused, match="LEDGER_ROW_INVALID"):
        read_verified(path)


def test_persisted_variance_parameters_must_reproduce_paths(tmp_path):
    root = flight(tmp_path)
    rows = read_verified(root / "ledger.jsonl")
    prediction = next(r["payload"] for r in rows if r["kind"] == "FORECAST")
    old_id, old_file = prediction["forecast_id"], prediction["path_file"]
    artifact = prediction["variance"]
    artifact["params"]["h"] *= 2
    artifact["artifact_digest"] = model_digest({"model_id": artifact["model_id"], "params": artifact["params"]})
    new_id = digest({k: v for k, v in prediction.items() if k not in ("forecast_id", "path_file")})
    # Retain the original path bytes and update every ordinary content reference.
    (root / old_file).rename(root / (new_id + ".npy"))
    rows = json.loads(canonical(rows).replace(old_id, new_id))
    rewrite(root, rows)
    with pytest.raises(Refused, match="PERSISTED_MODEL_PATHS_DISAGREE"):
        verify_run(root)


def test_completion_must_reconcile_pending_forecasts(tmp_path):
    root = flight(tmp_path)
    rows = read_verified(root / "ledger.jsonl")
    rows[-1]["payload"]["pending_forecast_ids"] = ["nonexistent"]
    rewrite(root, rows)
    with pytest.raises(Refused, match="RUN_CLOSE_ACCOUNTING_DISAGREES"):
        verify_run(root)


def test_matured_score_cannot_be_omitted_as_pending(tmp_path):
    root = flight(tmp_path)
    rows = read_verified(root / "ledger.jsonl")
    removed = next(r for r in rows if r["kind"] == "FORECAST_SCORE")
    rows.remove(removed)
    rows[-1]["payload"]["pending_forecast_ids"] = [removed["payload"]["forecast_id"]]
    rows[-1]["payload"]["counts"]["scored_forecasts"] -= 1
    rewrite(root, rows)
    with pytest.raises(Refused, match="MATURED_SCORE_EVIDENCE_DISAGREES"):
        verify_run(root)


def test_executable_exit_cannot_be_omitted_as_open_exposure(tmp_path):
    root = flight(tmp_path)
    rows = read_verified(root / "ledger.jsonl")
    rows.remove([r for r in rows if r["kind"] == "EXIT"][-1])
    rows[-1]["payload"]["status"] = "CLOSED_WITH_OUTSTANDING_OBLIGATIONS"
    rows[-1]["payload"]["counts"]["exits"] -= 1
    rewrite(root, rows)
    assert reconstruct(root / "ledger.jsonl")["status"] == "VALID"
    with pytest.raises(Refused, match="EXIT_OBLIGATION_NOT_SERVICED"):
        verify_run(root)


def test_exit_cannot_choose_favorable_superseded_quote(tmp_path):
    document, start, end = demo_document()
    latest = next(r for r in document["observations"] if r["kind"] == "quote" and r["event_epoch"] == end)
    old = {**latest, "event_epoch": end - 1, "available_epoch": end - 1,
           "bid": latest["bid"] + 1, "ask": latest["ask"] + 1}
    document["observations"].append(old)
    root = tmp_path / "run"
    run(canonical(document).encode(), root, start=start, end=end, config=Config(variance="ewma"))
    normalized, _ = normalize(document)
    old = next(r for r in normalized if r["kind"] == "quote" and r["event_epoch"] == end - 1)
    rows = read_verified(root / "ledger.jsonl")
    entry = [r for r in rows if r["kind"] == "ENTRY"][-1]["payload"]
    exit_row = [r for r in rows if r["kind"] == "EXIT"][-1]
    next(r for r in rows if r["hash"] == exit_row["payload"]["quote_ref"])["payload"] = old
    p = exit_row["payload"]
    credit = money(Decimal(str(old["bid"])) * p["quantity"])
    gross = credit - Decimal(entry["debit"])
    p.update(credit=str(credit), gross_pnl=str(gross),
             net_pnl=str(gross - Decimal(entry["fee"]) - Decimal(p["fee"])),
             cash_after=str(Decimal(entry["cash_after"]) + credit - Decimal(p["fee"])))
    rewrite(root, rows)
    assert reconstruct(root / "ledger.jsonl")["status"] == "VALID"
    with pytest.raises(Refused, match="EXIT_QUOTE_SELECTION_DISAGREES"):
        verify_run(root)
