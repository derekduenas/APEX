import copy
from dataclasses import replace
import json
import math
from pathlib import Path
import shutil

import numpy as np
import pytest

from apex.core import Config, Refused, canonical, digest
from apex.data import session
from apex.fixtures import research_demo_document
from apex.ledger import read_verified
from apex.research import MODELS, run_research, summarize
from apex.research_verification import verify_research


@pytest.fixture(scope="module")
def research_fixture(tmp_path_factory):
    document, plan = research_demo_document()
    days = sorted({session(row["event_epoch"]) for row in document["observations"]})[:4]
    document["observations"] = [row for row in document["observations"] if session(row["event_epoch"]) in days]
    starts = [min(row["event_epoch"] for row in document["observations"] if session(row["event_epoch"]) == day)
              for day in days]
    plan = replace(plan, start=starts[0], development_start=starts[1], holdout_start=starts[2],
                   end=starts[3] + 390 * 60, scan_minutes=60, minimum_training_labels=2,
                   training_window=20, minimum_comparison_sessions=1)
    config = Config(variance="ewma", paths=100)
    root = tmp_path_factory.mktemp("research-verification") / "run"
    result = run_research(canonical(document).encode(), root, plan=plan, config=config)
    return root, result, document, plan, config


def _copy_run(research_fixture, tmp_path):
    source = research_fixture[0]
    return Path(shutil.copytree(source, tmp_path / "run"))


def _rehash(root, rows, *, refresh_summary=False, plan=None):
    previous = "GENESIS"
    for index, row in enumerate(rows, 1):
        row.pop("hash", None)
        row["prev_hash"], row["seq"] = previous, index
        row["hash"] = digest(row)
        previous = row["hash"]
    (root / "ledger.jsonl").write_text("\n".join(canonical(row) for row in rows) + "\n")
    (root / "COMPLETE").write_text(previous)
    if refresh_summary:
        (root / "summary.json").write_text(canonical(summarize(rows, plan)))


def test_research_reader_reconstructs_real_artifacts_and_frozen_models(research_fixture):
    root, result, _, plan, _ = research_fixture
    verified = verify_research(root)
    assert verified["status"] == "VALID"
    assert verified["verified_forecasts"] == result["forecast_count"] > 0
    assert verified["verified_scores"] == result["scored_model_forecasts"]
    assert verified["verified_labels"] > verified["verified_forecasts"]
    rows = read_verified(root / "ledger.jsonl")
    heldout = [row["payload"] for row in rows if row["kind"] == "RESEARCH_FORECAST"
               and row["payload"]["phase"] == "HOLDOUT"]
    assert len(heldout) > 1
    assert all(row["ridge"] == heldout[0]["ridge"] for row in heldout)
    assert heldout[0]["ridge"]["max_target_epoch"] < plan.holdout_start
    assert heldout[0]["ridge"]["max_label_available_epoch"] < plan.holdout_start
    zeros = [row["payload"]["score"] for row in rows if row["kind"] == "RESEARCH_SCORE"
             and row["payload"]["model"] == "ZERO_DRIFT"]
    assert all(score["p_up"] == .5 and score["brier"] == .25 for score in zeros)
    assert "not authenticated" in verified["scope"]


@pytest.mark.parametrize("field", ["label_available_epoch", "bar_id", "features", "realized_log_return"])
def test_rehashed_labels_remain_bound_to_actual_mature_input(research_fixture, tmp_path, field):
    root = _copy_run(research_fixture, tmp_path)
    rows = read_verified(root / "ledger.jsonl")
    label = next(row for row in rows if row["kind"] == "RESEARCH_LABEL")
    if field == "features":
        label["payload"][field]["ret_1"] += .01
    elif field == "bar_id":
        label["payload"][field] = "f" * 64
    elif field == "label_available_epoch":
        label["payload"][field] = label["epoch"] + 1
    else:
        label["payload"][field] += .01
    _rehash(root, rows)
    with pytest.raises(Refused, match="RESEARCH_LABEL_NOT_MATURE_CAPTURE_BOUND"):
        verify_research(root)


def test_rehashed_ridge_cannot_train_on_a_future_holdout_label(research_fixture, tmp_path):
    root = _copy_run(research_fixture, tmp_path)
    rows = read_verified(root / "ledger.jsonl")
    forecast = next(row["payload"] for row in rows if row["kind"] == "RESEARCH_FORECAST" and row["payload"]["ridge"])
    late = next(row["payload"] for row in reversed(rows) if row["kind"] == "RESEARCH_LABEL")
    ridge = forecast["ridge"]
    ridge["training_records"][-1] = {key: value for key, value in late.items() if key != "bar_id"}
    ridge["training_digest"] = digest(ridge["training_records"])
    ridge["training_sample_ids"][-1] = late["sample_id"]
    ridge["model_id"] = digest({key: value for key, value in ridge.items() if key != "model_id"})
    _rehash(root, rows)
    with pytest.raises(Refused, match="RESEARCH_RIDGE_TRAINING_NOT_EARLIER_LABELS"):
        verify_research(root)


def test_rehashed_ridge_coefficients_are_recomputed(research_fixture, tmp_path):
    root = _copy_run(research_fixture, tmp_path)
    rows = read_verified(root / "ledger.jsonl")
    ridge = next(row["payload"]["ridge"] for row in rows if row["kind"] == "RESEARCH_FORECAST" and row["payload"]["ridge"])
    ridge["coefficients"][0] += .1
    ridge["model_id"] = digest({key: value for key, value in ridge.items() if key != "model_id"})
    _rehash(root, rows)
    with pytest.raises(Refused, match="RESEARCH_RIDGE_FIT_DISAGREES"):
        verify_research(root)


def test_holdout_cannot_reselect_even_with_updated_summary_and_hashes(research_fixture, tmp_path):
    root = _copy_run(research_fixture, tmp_path)
    rows = read_verified(root / "ledger.jsonl")
    frozen = next(row["payload"] for row in rows if row["kind"] == "SELECTION_FROZEN")
    frozen["selected_model"] = next(model for model in MODELS if model != frozen["selected_model"])
    _rehash(root, rows, refresh_summary=True, plan=research_fixture[3])
    with pytest.raises(Refused, match="RESEARCH_SELECTION_NOT_DEVELOPMENT_FROZEN"):
        verify_research(root)


@pytest.mark.parametrize("artifact", ["base_path", "terminal", "unsafe_filename"])
def test_full_paths_and_named_terminal_artifacts_are_bound(research_fixture, tmp_path, artifact):
    root = _copy_run(research_fixture, tmp_path)
    rows = read_verified(root / "ledger.jsonl")
    forecast = next(row["payload"] for row in rows if row["kind"] == "RESEARCH_FORECAST")
    if artifact == "unsafe_filename":
        forecast["base_path_file"] = "../" + forecast["base_path_file"]
        _rehash(root, rows)
        reason = "RESEARCH_INVALID_PATH_ARTIFACT_NAME"
    else:
        filename = forecast["base_path_file"] if artifact == "base_path" else forecast["variants"]["RIDGE_STATE"]["terminal_file"]
        values = np.load(root / filename, allow_pickle=False)
        if artifact == "base_path":
            values[0, 1] += .1  # Terminal values are unchanged: the full path is checked.
        else:
            values[0] += .1
        np.save(root / filename, values, allow_pickle=False)
        reason = "RESEARCH_SIMULATION_ARTIFACT_CHANGED"
    with pytest.raises(Refused, match=reason):
        verify_research(root)


def test_rehashed_interior_path_must_follow_the_persisted_variance_model(research_fixture, tmp_path):
    root = _copy_run(research_fixture, tmp_path)
    rows = read_verified(root / "ledger.jsonl")
    forecast = next(row["payload"] for row in rows if row["kind"] == "RESEARCH_FORECAST")
    base = forecast["base_forecast"]
    original_path = root / forecast["base_path_file"]
    values = np.load(original_path, allow_pickle=False)
    values[0, 1] += .1
    base["paths_digest"] = digest(values.tolist())
    base["forecast_id"] = digest({key: value for key, value in base.items() if key != "forecast_id"})
    forecast["base_path_file"] = base["forecast_id"] + ".npy"
    np.save(root / forecast["base_path_file"], values, allow_pickle=False)
    original_path.unlink()
    _rehash(root, rows)
    with pytest.raises(Refused, match="PERSISTED_MODEL_PATHS_DISAGREE"):
        verify_research(root)


def test_rehashed_terminal_cannot_break_common_noise(research_fixture, tmp_path):
    root = _copy_run(research_fixture, tmp_path)
    rows = read_verified(root / "ledger.jsonl")
    forecast = next(row["payload"] for row in rows if row["kind"] == "RESEARCH_FORECAST")
    variant = forecast["variants"]["RIDGE_STATE"]
    values = np.load(root / variant["terminal_file"], allow_pickle=False)
    values[0] += .1
    np.save(root / variant["terminal_file"], values, allow_pickle=False)
    variant["terminal_digest"] = digest(values.tolist())
    _rehash(root, rows)
    with pytest.raises(Refused, match="RESEARCH_COMMON_NOISE_OR_SHIFT_DISAGREES"):
        verify_research(root)


@pytest.mark.parametrize("mutation", ["score", "duplicate_score", "missing_sample", "candidate", "summary"])
def test_recomputed_scores_decisions_and_event_completeness(research_fixture, tmp_path, mutation):
    root = _copy_run(research_fixture, tmp_path)
    rows = read_verified(root / "ledger.jsonl")
    if mutation == "summary":
        summary = json.loads((root / "summary.json").read_text())
        summary["forecast_count"] += 1
        (root / "summary.json").write_text(canonical(summary))
        reason = "RESEARCH_SAVED_SUMMARY_DISAGREES"
    else:
        if mutation in ("score", "duplicate_score"):
            index = next(index for index, row in enumerate(rows) if row["kind"] == "RESEARCH_SCORE")
            if mutation == "score":
                rows[index]["payload"]["score"]["crps"] += 1
                reason = "RESEARCH_SCORE_DISAGREES"
            else:
                rows.insert(index, copy.deepcopy(rows[index]))
                reason = "RESEARCH_SCORE_DISAGREES"
        elif mutation == "candidate":
            row = next(row for row in rows if row["kind"] == "RESEARCH_FORECAST")
            row["payload"]["variants"]["ZERO_DRIFT"]["candidate"]["quantity"] += 1
            reason = "RESEARCH_CANDIDATE_BEHAVIOR_DISAGREES"
        else:
            index = next(index for index, row in enumerate(rows) if row["kind"] == "RESEARCH_SAMPLE")
            rows.pop(index)
            reason = "RESEARCH_EVENT_ORDER_OR_COMPLETENESS_INVALID"
        _rehash(root, rows)
    with pytest.raises(Refused, match=reason):
        verify_research(root)


def test_changing_future_holdout_values_preserves_earlier_forecasts_and_selection(research_fixture, tmp_path):
    original_root, _, original, plan, config = research_fixture
    changed = copy.deepcopy(original)
    for row in changed["observations"]:
        if row["event_epoch"] >= plan.holdout_start:
            factor = math.exp((row["event_epoch"] - plan.holdout_start) / 60 * .0002)
            for field in ("open", "high", "low", "close", "bid", "ask"):
                if field in row:
                    row[field] *= factor
    new_root = tmp_path / "changed-holdout"
    run_research(canonical(changed).encode(), new_root, plan=plan, config=config)
    assert verify_research(new_root)["status"] == "VALID"
    left, right = read_verified(original_root / "ledger.jsonl"), read_verified(new_root / "ledger.jsonl")
    for kind in ("SELECTION_FROZEN", "RESEARCH_FORECAST"):
        before = [row["payload"] for row in left if row["kind"] == kind
                  and (kind == "SELECTION_FROZEN" or row["payload"]["phase"] == "DEVELOPMENT")]
        after = [row["payload"] for row in right if row["kind"] == kind
                 and (kind == "SELECTION_FROZEN" or row["payload"]["phase"] == "DEVELOPMENT")]
        assert before == after
    left_models = [row["payload"]["ridge"] for row in left if row["kind"] == "RESEARCH_FORECAST" and row["payload"]["phase"] == "HOLDOUT"]
    right_models = [row["payload"]["ridge"] for row in right if row["kind"] == "RESEARCH_FORECAST" and row["payload"]["phase"] == "HOLDOUT"]
    assert left_models == right_models
    assert [row["payload"] for row in left if row["kind"] == "RESEARCH_SCORE"] != [row["payload"] for row in right if row["kind"] == "RESEARCH_SCORE"]
