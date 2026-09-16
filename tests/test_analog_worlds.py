import copy
from datetime import datetime, timezone

import numpy as np
import pytest

from apex.analog_worlds import sample_analog_worlds
from apex.core import Refused, canonical, digest
from apex.research_models import FEATURE_NAMES


def _episodes(count=90, *, per_session=30):
    rng = np.random.default_rng(71)
    base = datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc).timestamp()
    records = []
    for index in range(count):
        origin = base + (index // per_session) * 86400 + (index % per_session) * 60
        x = rng.normal(size=6) * [.002, .004, .008, .001, .003, .1]
        x += [0, 0, 0, .01, 0, .5]
        steps = rng.normal(size=15) * .001
        records.append({"sample_id": f"sample-{index:04d}", "decision_epoch": origin + 5,
                        "price_origin_epoch": origin, "target_epoch": origin + 900,
                        "label_available_epoch": origin + 905,
                        "features": dict(zip(FEATURE_NAMES, map(float, x))),
                        "regime": "TREND_UP" if index % 2 == 0 else "RANGE",
                        "path_log_returns": [0.0, *np.cumsum(steps).tolist()]})
    return records


def _sample(rows=None, query=None, **kwargs):
    rows = _episodes() if rows is None else rows
    cutoff = max(row["label_available_epoch"] for row in rows)
    options = {"cutoff": cutoff, "now": cutoff + 60, "horizon_minutes": 15, "paths": 100, "seed": 41}
    options.update(kwargs)
    return sample_analog_worlds(rows, query or rows[min(30, len(rows) - 1)]["features"], **options)


def test_worlds_preserve_whole_paths_and_bind_each_draw_to_a_source():
    rows = _episodes()
    artifact, worlds = _sample(rows)
    source = {row["sample_id"]: row for row in rows}
    assert worlds.shape == (100, 16)
    for sample_id, world in zip(artifact["sampled_source_ids"], worlds):
        np.testing.assert_array_equal(world, source[sample_id]["path_log_returns"])
    assert artifact["paths_hash"] == digest(worlds.tolist())
    assert artifact["effective_sample_size"] == 40
    assert artifact["effective_sample_size"] < artifact["paths"]
    assert sum(row["weight"] for row in artifact["neighbors"]) == pytest.approx(1)
    assert artifact["calibration_status"] == "EMPIRICAL_SCENARIO_RESAMPLING_UNCALIBRATED"
    assert artifact["capital_authority"] == "NONE"
    assert artifact["artifact_id"] == digest({k: v for k, v in artifact.items() if k != "artifact_id"})


def test_permutation_has_byte_identical_artifacts_and_paths():
    rows = _episodes()
    query = copy.deepcopy(rows[30]["features"])
    expected, paths = _sample(rows, query)
    np.random.default_rng(33).shuffle(rows)
    actual, actual_paths = _sample(rows, query)
    assert canonical(actual) == canonical(expected)
    np.testing.assert_array_equal(actual_paths, paths)


@pytest.mark.parametrize("mutation", ["target", "available", "decision"])
def test_low_level_firewall_refuses_a_future_episode_instead_of_filtering_it(mutation):
    rows = _episodes()
    cutoff = max(row["label_available_epoch"] for row in rows)
    if mutation == "available":
        rows[0]["label_available_epoch"] = cutoff + 1
    else:
        origin = cutoff + 60 if mutation == "decision" else cutoff - 100
        rows[0].update(price_origin_epoch=origin, decision_epoch=origin + 5,
                       target_epoch=origin + 900, label_available_epoch=origin + 905)
    with pytest.raises(Refused, match="TRAINING_AVAILABILITY_FIREWALL"):
        _sample(rows, cutoff=cutoff, now=cutoff + 10000)


def test_caller_partition_not_future_path_values_determines_frozen_membership():
    rows = _episodes(120)
    cutoff = rows[89]["label_available_epoch"]
    visible = [row for row in rows if row["label_available_epoch"] <= cutoff]
    before, paths_before = _sample(visible, cutoff=cutoff, now=cutoff + 86400)
    for row in rows[90:]:
        row["features"]["ret_1"] = 10000
        row["path_log_returns"][-1] = -10000
    after, paths_after = _sample([row for row in rows if row["label_available_epoch"] <= cutoff],
                                cutoff=cutoff, now=cutoff + 86400)
    assert before == after
    np.testing.assert_array_equal(paths_before, paths_after)
    with pytest.raises(Refused, match="TRAINING_AVAILABILITY_FIREWALL"):
        _sample(rows, cutoff=cutoff, now=cutoff + 86400)


def test_cutoff_cannot_postdate_decision_and_is_inclusive():
    rows = _episodes()
    cutoff = rows[-1]["label_available_epoch"]
    artifact, _ = _sample(rows, now=cutoff)
    assert artifact["cutoff_epoch"] == cutoff
    with pytest.raises(Refused, match="INVALID_CUTOFF"):
        _sample(rows, now=cutoff - 1)


@pytest.mark.parametrize("mutation,reason", [
    ("duplicate", "DUPLICATE_SAMPLE_ID"), ("available_before_target", "AVAILABLE_BEFORE_TARGET"),
    ("wrong_horizon", "HORIZON_MISMATCH"), ("origin_after_decision", "HORIZON_MISMATCH"),
    ("path_length", "INVALID_FORWARD_PATH"), ("path_origin", "INVALID_FORWARD_PATH"),
    ("path_nan", "INVALID_FORWARD_PATH"), ("missing_feature", "FEATURE_CONTRACT_MISMATCH"),
    ("extra_feature", "FEATURE_CONTRACT_MISMATCH"), ("feature_nan", "NONFINITE_FEATURE"),
    ("negative_rv", "FEATURE_DOMAIN_INVALID"), ("boolean_path", "INVALID_FORWARD_PATH"),
])
def test_malformed_episode_has_named_refusal(mutation, reason):
    rows = _episodes()
    row = rows[0]
    if mutation == "duplicate":
        rows[1]["sample_id"] = row["sample_id"]
    elif mutation == "available_before_target":
        row["label_available_epoch"] = row["target_epoch"] - 1
    elif mutation == "wrong_horizon":
        row["target_epoch"] -= 1
    elif mutation == "origin_after_decision":
        row["price_origin_epoch"] = row["decision_epoch"] + 1
    elif mutation == "path_length":
        row["path_log_returns"].pop()
    elif mutation == "path_origin":
        row["path_log_returns"][0] = 1
    elif mutation == "path_nan":
        row["path_log_returns"][4] = float("nan")
    elif mutation == "boolean_path":
        row["path_log_returns"][4] = True
    elif mutation == "missing_feature":
        row["features"].pop("ret_5")
    elif mutation == "extra_feature":
        row["features"]["tomorrow_return"] = 1
    elif mutation == "negative_rv":
        row["features"]["rv_30"] = -.1
    else:
        row["features"]["ret_5"] = float("nan")
    with pytest.raises(Refused, match=reason):
        _sample(rows)


def test_more_paths_do_not_manufacture_historical_support():
    with pytest.raises(Refused, match="INSUFFICIENT_EPISODES"):
        _sample(_episodes(19), paths=10000)
    with pytest.raises(Refused, match="INSUFFICIENT_TRAINING_SESSIONS"):
        _sample(_episodes(60, per_session=60), paths=10000)


def test_scaler_uses_training_only_and_copies_membership():
    rows = _episodes()
    query = copy.deepcopy(rows[30]["features"])
    query["ret_1"] = .5
    artifact, _ = _sample(rows, query)
    x = np.asarray([[row["features"][name] for name in FEATURE_NAMES] for row in rows])
    np.testing.assert_allclose(artifact["means"], x.mean(axis=0))
    np.testing.assert_allclose(artifact["scales"], x.std(axis=0))
    saved = canonical(artifact)
    rows[0]["features"]["ret_1"] = 99
    rows[1]["path_log_returns"][-1] = 99
    assert canonical(artifact) == saved


def test_far_query_is_out_of_support_and_monte_carlo_does_not_change_it():
    rows = _episodes()
    query = copy.deepcopy(rows[30]["features"])
    query["ret_1"] = 1
    artifact, _ = _sample(rows, query)
    assert artifact["in_support"] is False
    assert artifact["query_support_distance"] > artifact["support_distance_threshold"]
    more, _ = _sample(rows, query, paths=1000)
    assert more["support_reasons"] == artifact["support_reasons"]
    assert more["effective_sample_size"] == artifact["effective_sample_size"]


def test_support_reference_is_independently_computable_leave_one_out():
    rows = _episodes()
    artifact, _ = _sample(rows)
    x = np.asarray([[r["features"][name] for name in FEATURE_NAMES] for r in rows])
    z = (x - np.asarray(artifact["means"])) / np.asarray(artifact["scales"])
    reference = []
    for index, row in enumerate(z):
        distances = [float(np.sqrt(np.mean((other - row) ** 2))) for j, other in enumerate(z) if j != index]
        reference.append(sorted(distances)[39])
    np.testing.assert_allclose(artifact["training_leave_one_out_distances"], reference)
    assert artifact["support_distance_threshold"] == pytest.approx(np.quantile(reference, .95))


def test_constant_feature_shift_cannot_hide_behind_unit_scale():
    rows = _episodes()
    for row in rows:
        row["features"]["ret_1"] = 0.0
    query = copy.deepcopy(rows[30]["features"])
    query["ret_1"] = 1e-15
    artifact, _ = _sample(rows, query)
    assert artifact["in_support"] is False
    assert artifact["constant_feature_mismatches"] == ["ret_1"]


def test_selected_neighbors_need_session_diversity_too():
    rows = _episodes(60, per_session=20)
    for index, row in enumerate(rows):
        row["features"] = dict(zip(FEATURE_NAMES, [index // 20, 0, 0, .01, 0, .5]))
    artifact, _ = _sample(rows, rows[0]["features"], maximum_neighbors=20)
    assert len(artifact["training_sessions"]) == 3
    assert len(artifact["selected_sessions"]) == 1
    assert "NEIGHBORS_FROM_TOO_FEW_SESSIONS" in artifact["support_reasons"]
    assert artifact["in_support"] is False


def test_regime_is_a_real_reader_that_changes_selected_paths():
    rows = _episodes()
    for row in rows:
        row["features"] = dict(zip(FEATURE_NAMES, [0, 0, 0, .01, 0, .5]))
        sign = 1 if row["regime"] == "TREND_UP" else -1
        row["path_log_returns"] = (np.arange(16) * .001 * sign).tolist()
    up, up_paths = _sample(rows, regime="TREND_UP")
    range_, range_paths = _sample(rows, regime="RANGE")
    assert np.all(up_paths[:, -1] > 0) and np.all(range_paths[:, -1] < 0)
    assert all(row["regime"] == "TREND_UP" for row in up["neighbors"])
    assert all(row["regime"] == "RANGE" for row in range_["neighbors"])
    assert up["regime_role"] == "SQUARED_DISTANCE_MISMATCH_PENALTY"
    assert up["in_support"] and range_["in_support"]
    assert up["artifact_id"] != range_["artifact_id"]


def test_regime_penalty_also_enters_training_support_reference():
    rows = _episodes(60, per_session=20)
    for row in rows:
        row["features"] = dict(zip(FEATURE_NAMES, [0, 0, 0, .01, 0, .5]))
    artifact, _ = _sample(rows, regime="TREND_UP", maximum_neighbors=40, regime_mismatch_penalty=4)
    # Each regime has 30 episodes. A training row's 40th other neighbor must
    # cross regime; sqrt(4)=2. The same penalty is observable in retrieval.
    assert artifact["training_leave_one_out_distances"] == [2.0] * 60
    assert artifact["support_distance_threshold"] == 2.0
    assert {row["distance"] for row in artifact["neighbors"]} == {0.0, 2.0}


def test_provided_regime_requires_known_training_regimes():
    rows = _episodes()
    rows[0].pop("regime")
    with pytest.raises(Refused, match="INVALID_EPISODE_REGIME"):
        _sample(rows, regime="TREND_UP")
    artifact, _ = _sample(rows)
    assert artifact["regime_role"] == "NOT_USED"


def test_unseen_query_regime_cannot_be_in_support_even_with_permissive_geometric_threshold():
    rows = _episodes()
    artifact, _ = _sample(rows, regime="NEW_REGIME")
    assert artifact["in_support"] is False
    assert "QUERY_REGIME_NOT_IN_TRAINING" in artifact["support_reasons"]


def test_no_origin_field_uses_decision_target_contract():
    rows = _episodes()
    for row in rows:
        row["decision_epoch"] = row.pop("price_origin_epoch")
    artifact, _ = _sample(rows)
    assert all(row["price_origin_epoch"] == row["decision_epoch"] for row in artifact["training_records"])


def test_no_session_crossing_forward_paths():
    rows = _episodes()
    origin = datetime(2026, 8, 11, 3, 59, tzinfo=timezone.utc).timestamp()
    rows[0].update(price_origin_epoch=origin, decision_epoch=origin + 5,
                   target_epoch=origin + 900, label_available_epoch=origin + 905)
    with pytest.raises(Refused, match="PATH_CROSSES_SESSION"):
        _sample(rows)


def test_extreme_finite_features_refuse_without_nan_artifact():
    rows = _episodes()
    rows[0]["features"]["ret_1"] = 1e308
    rows[1]["features"]["ret_1"] = -1e308
    with pytest.raises(Refused, match="NUMERIC_FAILURE"):
        _sample(rows)
