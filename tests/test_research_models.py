import copy
import json
import math

import numpy as np
import pytest

from apex.core import Refused, canonical, digest
from apex.data import normalize, twin
from apex.fixtures import demo_document
from apex.research_models import FEATURE_NAMES, features, fit_ridge, predict_ridge


def _training_records(count=240, *, offset=0, seed=7):
    rng = np.random.default_rng(seed)
    x = rng.normal(size=(count, len(FEATURE_NAMES)))
    x = x * [.002, .004, .008, .005, .003, .2] + [0, 0, 0, .01, 0, .5]
    beta = np.array([.7, -.4, .3, .1, -.2, .003])
    y = .001 + x @ beta
    rows = []
    for index in range(count):
        decision = 1_700_000_000 + (offset + index) * 1200
        rows.append({"sample_id": f"sample-{offset + index}", "decision_epoch": decision,
                     "target_epoch": decision + 900, "label_available_epoch": decision + 903,
                     "features": dict(zip(FEATURE_NAMES, map(float, x[index]))),
                     "realized_log_return": float(y[index])})
    return rows


def _fit(rows, **kwargs):
    return fit_ridge(rows, cutoff=rows[-1]["label_available_epoch"], horizon_minutes=15, **kwargs)


def _market_snapshot(completed=40):
    doc, start, _ = demo_document(quotes=False)
    now = start + (completed - 5) * 60
    rows, rejected = normalize(doc)
    assert not rejected
    snapshot, returns = twin(rows, now, "SPY")
    return doc, rows, snapshot, returns


def test_features_use_real_reader_and_exact_completed_session_window():
    _, _, snapshot, returns = _market_snapshot(completed=31)
    values = features(snapshot, returns)
    assert tuple(values) == FEATURE_NAMES
    assert values["rv_30"] == pytest.approx(math.sqrt(sum(r["ret_1"] ** 2 for r in returns[-30:])))
    assert values["log_spot_to_vwap"] == pytest.approx(
        math.log(snapshot["fields"]["spot"] / snapshot["fields"]["close_weighted_vwap_proxy"]))
    assert values["session_fraction"] == 31 / 390
    for name in ("ret_1", "ret_5", "ret_15"):
        assert values[name] == snapshot["fields"][name]


def test_rv_refuses_insufficient_same_session_history_despite_yesterday_rows():
    _, _, snapshot, returns = _market_snapshot(completed=30)
    assert len(returns) > 30
    with pytest.raises(Refused, match="NONCONTIGUOUS_CURRENT_SESSION_RETURNS"):
        features(snapshot, returns)


def test_rv_refuses_a_gap_inside_consumed_window():
    _, rows, snapshot, _ = _market_snapshot(completed=50)
    missing = snapshot["fields"]["last_bar_event"] - 20 * 60
    gapped = [row for row in rows if row["event_epoch"] != missing]
    snapshot, returns = twin(gapped, snapshot["now"], "SPY")
    assert snapshot["fields"]["ret_15"] is not None
    with pytest.raises(Refused, match="NONCONTIGUOUS_CURRENT_SESSION_RETURNS"):
        features(snapshot, returns)


def test_session_fraction_does_not_compress_earlier_missing_bars():
    _, rows, snapshot, _ = _market_snapshot(completed=50)
    missing = snapshot["fields"]["last_bar_event"] - 40 * 60
    snapshot, returns = twin([r for r in rows if r["event_epoch"] != missing], snapshot["now"], "SPY")
    assert snapshot["fields"]["completed_bars_today"] == 49
    assert features(snapshot, returns)["session_fraction"] == 50 / 390


@pytest.mark.parametrize("mutation", ["delayed", "future", "unknown_id", "nonfinite"])
def test_feature_window_refuses_unavailable_or_invalid_returns(mutation):
    _, _, snapshot, returns = _market_snapshot()
    if mutation == "delayed":
        returns[-1]["available"] = snapshot["now"] + 1
    elif mutation == "future":
        returns.append({**returns[-1], "event_time": snapshot["now"] + 60,
                        "available": snapshot["now"] + 60})
    elif mutation == "unknown_id":
        returns[-1]["input_ids"][1] = "not-in-the-snapshot"
    else:
        returns[-1]["ret_1"] = float("nan")
    with pytest.raises(Refused):
        features(snapshot, returns)


def test_future_observations_cannot_change_snapshot_features():
    document, _, snapshot, returns = _market_snapshot()
    expected = features(snapshot, returns)
    for row in document["observations"]:
        if row["available_epoch"] > snapshot["now"]:
            for name in ("open", "high", "low", "close"):
                row[name] *= 2
    rows, rejected = normalize(document)
    assert not rejected
    new_snapshot, new_returns = twin(rows, snapshot["now"], "SPY")
    assert features(new_snapshot, new_returns) == expected


@pytest.mark.parametrize("name,value", [("ret_5", None), ("spot", float("nan")),
                                        ("close_weighted_vwap_proxy", 0)])
def test_missing_or_invalid_snapshot_values_are_not_imputed(name, value):
    _, _, snapshot, returns = _market_snapshot()
    snapshot["fields"][name] = value
    with pytest.raises(Refused):
        features(snapshot, returns)


@pytest.mark.parametrize("future", ["availability", "target"])
def test_fit_refuses_labels_not_mature_at_training_cutoff(future):
    rows = _training_records(4)
    cutoff = rows[-1]["label_available_epoch"]
    if future == "availability":
        rows[0]["label_available_epoch"] = cutoff + 1
    else:
        rows[-1]["decision_epoch"] = cutoff - 899
        rows[-1]["target_epoch"] = cutoff + 1
        rows[-1]["label_available_epoch"] = cutoff + 4
    with pytest.raises(Refused, match="AVAILABILITY_FIREWALL"):
        fit_ridge(rows, cutoff=cutoff, horizon_minutes=15)


def test_training_cutoff_is_inclusive_and_label_cannot_precede_target():
    rows = _training_records(4)
    model = _fit(rows)
    assert model["max_label_available_epoch"] == model["cutoff_epoch"]
    rows[0]["label_available_epoch"] = rows[0]["target_epoch"] - 1
    with pytest.raises(Refused, match="LABEL_AVAILABLE_BEFORE_TARGET"):
        _fit(rows)


def test_delayed_decision_keeps_origin_based_horizon_and_recomputable_records():
    rows = _training_records(8)
    expected = _fit(rows)
    for row in rows:
        row["price_origin_epoch"] = row["decision_epoch"]
        row["decision_epoch"] += 5
    model = _fit(rows)
    assert model["coefficients"] == expected["coefficients"]
    assert all(record["decision_epoch"] == record["price_origin_epoch"] + 5
               for record in model["training_records"])
    assert all(record["target_epoch"] == record["price_origin_epoch"] + 900
               for record in model["training_records"])
    assert _fit(model["training_records"]) == model
    assert all(record["price_origin_epoch"] == record["decision_epoch"]
               for record in expected["training_records"])


@pytest.mark.parametrize("mutation,reason", [
    ("future_origin", "PRICE_ORIGIN_AFTER_DECISION"),
    ("mismatched_horizon", "TARGET_HORIZON_MISMATCH"),
    ("nonfinite_origin", "INVALID_RIDGE_PRICE_ORIGIN"),
    ("decision_at_target", "TARGET_HORIZON_MISMATCH"),
])
def test_invalid_price_origin_or_target_refuses(mutation, reason):
    rows = _training_records(8)
    row = rows[0]
    row["price_origin_epoch"] = row["decision_epoch"]
    if mutation == "future_origin":
        row["price_origin_epoch"] += 1
        row["target_epoch"] += 1
    elif mutation == "mismatched_horizon":
        row["target_epoch"] += 1
    elif mutation == "nonfinite_origin":
        row["price_origin_epoch"] = float("nan")
    else:
        row["decision_epoch"] = row["target_epoch"]
    with pytest.raises(Refused, match=reason):
        _fit(rows)


def test_scaler_is_independent_of_test_values_and_predictions_do_not_refit():
    training = _training_records()
    test = _training_records(20, offset=300, seed=8)
    model = _fit(training)
    frozen = canonical(model)
    x = np.array([[row["features"][name] for name in FEATURE_NAMES] for row in training])
    np.testing.assert_allclose(model["means"], x.mean(axis=0))
    np.testing.assert_allclose(model["scales"], x.std(axis=0, ddof=0))
    for row in test:
        predict_ridge(model, row["features"])
        row["features"]["ret_1"] += 1000
        predict_ridge(model, row["features"])
    assert canonical(model) == frozen
    assert _fit(training) == model


def test_planted_relation_is_learned_and_scores_disjoint_later_data():
    training = _training_records(300)
    test = _training_records(100, offset=400, seed=77)
    model = _fit(training)
    assert model["cutoff_epoch"] < min(row["decision_epoch"] for row in test)
    prediction = np.array([predict_ridge(model, row["features"]) for row in test])
    target = np.array([row["realized_log_return"] for row in test])
    training_mean = np.mean([row["realized_log_return"] for row in training])
    assert np.mean((prediction - target) ** 2) < .01 * np.mean((training_mean - target) ** 2)
    assert model["calibration_status"] == "RESEARCH_UNCALIBRATED"
    assert model["capital_authority"] == "NONE"


@pytest.mark.parametrize("bad_value", [None, float("nan"), float("inf"), True])
def test_missing_or_nonfinite_training_and_prediction_features_refuse(bad_value):
    rows = _training_records(8)
    model = _fit(rows)
    bad = copy.deepcopy(rows[0]["features"])
    bad["ret_15"] = bad_value
    with pytest.raises(Refused, match="MISSING_OR_NONFINITE_FEATURE:ret_15"):
        predict_ridge(model, bad)
    rows[0]["features"] = bad
    with pytest.raises(Refused, match="MISSING_OR_NONFINITE_FEATURE:ret_15"):
        _fit(rows)


def test_all_fit_inputs_and_coefficients_are_recomputable_from_json_artifact():
    model = json.loads(canonical(_fit(_training_records())))
    records = model["training_records"]
    x = np.array([[row["features"][name] for name in model["feature_names"]] for row in records])
    y = np.array([row["realized_log_return"] for row in records])
    means, scales = x.mean(axis=0), x.std(axis=0)
    scales[scales == 0] = 1
    z = (x - means) / scales
    coefficients = np.linalg.solve(z.T @ z + model["alpha"] * np.eye(len(FEATURE_NAMES)), z.T @ (y - y.mean()))
    np.testing.assert_array_equal(model["means"], means)
    np.testing.assert_array_equal(model["scales"], scales)
    np.testing.assert_array_equal(model["coefficients"], coefficients)
    assert model["intercept"] == float(y.mean())
    assert model["training_digest"] == digest(records)
    assert model["training_sample_ids"] == [r["sample_id"] for r in records]
    assert fit_ridge(records, cutoff=model["cutoff_epoch"], horizon_minutes=15, alpha=model["alpha"]) == model


def test_constant_features_keep_unpenalized_intercept():
    rows = _training_records(20)
    for row in rows:
        row["features"] = {name: .5 for name in FEATURE_NAMES}
        row["realized_log_return"] = .125
    model = _fit(rows)
    assert model["scales"] == [1.0] * len(FEATURE_NAMES)
    assert model["coefficients"] == [0.0] * len(FEATURE_NAMES)
    assert predict_ridge(model, rows[0]["features"]) == .125


def test_fit_copies_input_records_and_model_mutations_invalidate_identity():
    rows = _training_records(20)
    values = copy.deepcopy(rows[0]["features"])
    model = _fit(rows)
    expected = predict_ridge(model, values)
    original = canonical(model)
    rows[0]["features"]["ret_1"] = 99
    rows[0]["realized_log_return"] = 99
    assert canonical(model) == original
    assert predict_ridge(model, values) == expected
    modified = copy.deepcopy(model)
    modified["coefficients"][0] += 1
    with pytest.raises(Refused, match="RIDGE_MODEL_DIGEST_MISMATCH"):
        predict_ridge(modified, values)
    assert predict_ridge(model, values) == expected


@pytest.mark.parametrize("alpha", [0, -1, True, float("nan"), float("inf")])
def test_invalid_ridge_penalty_refuses(alpha):
    with pytest.raises(Refused, match="INVALID_RIDGE_ALPHA"):
        _fit(_training_records(4), alpha=alpha)
