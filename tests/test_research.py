import copy
from dataclasses import replace
import json

import numpy as np
import pytest

from apex.core import Config, Refused, canonical
from apex.fixtures import research_demo_document
from apex.ledger import read_verified
from apex.research import ResearchPlan, distribution_score, run_research


@pytest.fixture(scope="module")
def research_flight(tmp_path_factory):
    root = tmp_path_factory.mktemp("research") / "flight"
    doc, plan = research_demo_document()
    summary = run_research(canonical(doc).encode(), root, plan=plan, config=Config(variance="ewma", paths=100))
    return root, doc, plan, summary


def test_crps_matches_direct_pairwise_definition_and_exact_null_brier():
    values = np.array([-.02, -.01, .0, .04, .08])
    y = .017
    expected = np.mean(abs(values-y)) - .5 * np.mean(abs(values[:, None]-values[None, :]))
    score = distribution_score(values, y)
    assert score["crps"] == pytest.approx(expected, abs=1e-14)
    assert score["brier"] == (.4-1)**2
    assert distribution_score(values, y, model="ZERO_DRIFT")["brier"] == .25
    median = float(np.median(values))
    assert score["pinball"]["0.5"] == .5*abs(y-median)


def test_real_tournament_calls_all_three_models_and_scores_holdout(research_flight):
    root, _, _, summary = research_flight
    records = read_verified(root / "ledger.jsonl")
    assert summary["forecast_count"] == 88
    assert summary["scored_model_forecasts"] == 264
    assert summary["holdout"]["paired_samples"] == 44
    assert len(summary["holdout"]["sessions"]) == 4
    assert summary["readiness"] == "RESEARCH_ONLY_NO_CALIBRATION_OR_TRADING_AUTHORITY"
    assert summary["input_class"] == "SYNTHETIC_RESEARCH_CONTROL"
    for row in records:
        if row["kind"] == "RESEARCH_FORECAST":
            assert set(row["payload"]["variants"]) == {"ZERO_DRIFT", "SHRUNK_MEAN", "RIDGE_STATE"}
            assert row["payload"]["ridge"]["training_rows"] >= 40


def test_holdout_fit_frozen_and_every_consumed_training_label_precedes_cutoff(research_flight):
    root, _, plan, _ = research_flight
    records = read_verified(root / "ledger.jsonl")
    holdout_models = set()
    seen_labels = {}
    for row in records:
        payload = row["payload"]
        if row["kind"] == "RESEARCH_LABEL":
            seen_labels[payload["sample_id"]] = payload
        if row["kind"] != "RESEARCH_FORECAST":
            continue
        model = payload["ridge"]
        for training in model["training_records"]:
            prior = seen_labels[training["sample_id"]]
            assert prior["realized_log_return"] == training["realized_log_return"]
            assert training["label_available_epoch"] <= payload["training_cutoff_epoch"] < row["epoch"]
            assert training["target_epoch"] <= payload["training_cutoff_epoch"]
        if payload["phase"] == "HOLDOUT":
            holdout_models.add(model["model_id"])
            assert model["cutoff_epoch"] == plan.holdout_start - 60
    assert len(holdout_models) == 1


def test_terminal_paths_share_shocks_and_learned_output_reaches_candidate(research_flight):
    root, _, _, _ = research_flight
    effected = False
    for row in read_verified(root / "ledger.jsonl"):
        if row["kind"] != "RESEARCH_FORECAST":
            continue
        variants = row["payload"]["variants"]
        base = variants["ZERO_DRIFT"]
        noise = np.load(root/base["terminal_file"], allow_pickle=False)
        for name, variant in variants.items():
            actual = np.load(root/variant["terminal_file"], allow_pickle=False)
            np.testing.assert_allclose(actual, noise+variant["total_log_drift"], rtol=0, atol=1e-17)
        if variants["RIDGE_STATE"]["candidate"]["expected_net"] != variants["ZERO_DRIFT"]["candidate"]["expected_net"]:
            effected = True
    assert effected


def test_future_holdout_perturbation_cannot_change_earlier_predictions_or_selection(research_flight, tmp_path):
    root, original, plan, summary = research_flight
    changed = copy.deepcopy(original)
    for row in changed["observations"]:
        if row["event_epoch"] >= plan.holdout_start:
            for key in ("open", "high", "low", "close", "bid", "ask"):
                if key in row:
                    row[key] *= 1.2
    new = tmp_path/"changed"
    result = run_research(canonical(changed).encode(), new, plan=plan, config=Config(variance="ewma", paths=100))
    kinds = {"RESEARCH_FORECAST", "SELECTION_FROZEN"}
    before = [r["payload"] for r in read_verified(root/"ledger.jsonl") if r["kind"] in kinds and r["epoch"] <= plan.holdout_start]
    after = [r["payload"] for r in read_verified(new/"ledger.jsonl") if r["kind"] in kinds and r["epoch"] <= plan.holdout_start]
    assert before == after
    assert result["frozen_selection"] == summary["frozen_selection"]


def test_delayed_development_label_cannot_enter_frozen_selection(research_flight, tmp_path):
    _, original, plan, _ = research_flight
    changed = copy.deepcopy(original)
    # Last development-session 15:45 target, deliberately delivered after the
    # holdout boundary. It may be scored later, but never participate in selection.
    target_start = plan.holdout_start - 86400 + 375*60 - 60
    for row in changed["observations"]:
        if row["kind"] == "bar" and row["event_epoch"] == target_start:
            row["available_epoch"] = plan.holdout_start + 3600
    out = tmp_path/"late"
    run_research(canonical(changed).encode(), out, plan=plan, config=Config(variance="ewma", paths=100))
    records = read_verified(out/"ledger.jsonl")
    late = [r["payload"] for r in records if r["kind"] == "RESEARCH_LABEL" and r["payload"]["label_available_epoch"] == plan.holdout_start+3600]
    assert late, "Fixture must reach a real delayed label"
    frozen = next(r["payload"] for r in records if r["kind"] == "SELECTION_FROZEN")
    assert all(r["sample_id"] not in frozen["eligible_sample_ids"] for r in late)


def test_unavailable_quotes_do_not_become_invented_fills(research_flight, tmp_path):
    _, doc, plan, _ = research_flight
    bars_only = {**doc, "observations": [r for r in doc["observations"] if r["kind"] == "bar"]}
    result = run_research(canonical(bars_only).encode(), tmp_path/"bars", plan=plan, config=Config(variance="ewma", paths=100))
    assert result["forecast_count"] == 88
    observed = result["candidate_observations"]
    assert (observed["long"], observed["wait"]) == (0, 264)
    # Every WAIT here is the absence of a quote, not a judgement about the market. That is exactly the shape of
    # answer a bars-only input forces, and the histogram is what makes the difference legible.
    assert observed["wait_reasons"] == {"QUOTE_UNAVAILABLE_OR_CONFLICTING": 264}
    assert "NOT trades" in observed["meaning"]
    records = read_verified(tmp_path/"bars"/"ledger.jsonl")
    assert not any(r["kind"] in ("ENTRY", "EXIT") for r in records)


def test_invalid_phase_or_overlapping_plan_refuses_before_run(tmp_path):
    doc, plan = research_demo_document()
    with pytest.raises(Refused, match="OFF_SCAN_GRID"):
        replace(plan, holdout_start=plan.holdout_start+300)
    with pytest.raises(Refused, match="OVERLAPPING_TARGETS"):
        run_research(canonical(doc).encode(), tmp_path/"no", plan=replace(plan, scan_minutes=5), config=Config())
    assert not (tmp_path/"no").exists()


def test_realistic_receipt_delay_flows_through_actual_tournament(research_flight, tmp_path):
    _, original, plan, _ = research_flight
    doc = copy.deepcopy(original)
    for row in doc["observations"]:
        row["available_epoch"] += .25
        row["availability_basis"] = "MEASURED_RECEIPT"
    root = tmp_path/"measured"
    result = run_research(canonical(doc).encode(), root, plan=plan, config=Config(variance="ewma", paths=100))
    assert result["forecast_count"] == 88
    assert result["holdout"]["paired_samples"] == 44
    assert result["input_class"] == "SYNTHETIC_RESEARCH_CONTROL"
    # These timestamps are fabricated by this test, not provider evidence.
    # The receipt-labelled path is exercised without claiming a live capture.
    samples = [r for r in read_verified(root/"ledger.jsonl") if r["kind"] == "RESEARCH_SAMPLE"]
    assert samples
    assert all(r["payload"]["decision_epoch"] - r["payload"]["price_origin_epoch"] == 5. for r in samples)


def test_out_of_support_ridge_refuses_variant_and_baselines_continue(monkeypatch, research_flight, tmp_path):
    import apex.research as research
    _, doc, plan, _ = research_flight
    monkeypatch.setattr(research, "predict_ridge", lambda *_: 1000.)
    root = tmp_path/"unsupported"
    result = run_research(canonical(doc).encode(), root, plan=plan, config=Config(variance="ewma", paths=100))
    assert result["forecast_count"] == 88
    assert result["holdout"]["paired_samples"] == 0
    forecasts = [r["payload"] for r in read_verified(root/"ledger.jsonl") if r["kind"] == "RESEARCH_FORECAST"]
    assert all(f["ridge_problem"] == "RIDGE_SIMULATION_NUMERIC_FAILURE" and set(f["variants"]) == {"ZERO_DRIFT", "SHRUNK_MEAN"} for f in forecasts)


def test_run_collision_preserves_prior_evidence(research_flight):
    root, doc, plan, _ = research_flight
    before = (root/"COMPLETE").read_bytes()
    with pytest.raises(FileExistsError):
        run_research(canonical(doc).encode(), root, plan=plan, config=Config())
    assert (root/"COMPLETE").read_bytes() == before
