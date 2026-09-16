"""Empirical session evidence uses the complete family and actual sessions."""
import copy
import math

import numpy as np
import pytest

from apex.core import Refused, digest
from apex.strategy_evidence import session_bootstrap


def rows(values, *, second=None):
    return [{"sample_id": f"sample-{i:03}", "session": f"session-{i:03}",
             "net_returns": {"WAIT": 0.0, "TREND": value,
                             **({"REVERSION": second[i]} if second is not None else {})}}
            for i, value in enumerate(values)]


def run(outcomes, **kwargs):
    return session_bootstrap(outcomes, strategy_ids=list(outcomes[0]["net_returns"]),
                             bootstrap_samples=199, **kwargs)


def test_constant_known_example_and_finite_resampling_correction():
    result = run(rows([0.002] * 10))
    assert result["status"] == "DESCRIPTIVE_BOOTSTRAP_ONLY"
    assert result["session_count"] == result["opportunity_count"] == 10
    assert result["best_observed_strategy"] == "TREND"
    assert result["family_p_value"] == 1 / 200
    assert result["observed_max_statistic"] == pytest.approx(math.sqrt(10) * .002)
    assert result["by_strategy"]["TREND"]["simultaneous_lower_mean_excess"] == pytest.approx(.002)
    assert result["by_strategy"]["WAIT"]["simultaneous_lower_mean_excess"] == 0
    assert result["promotion"] == "NOT_AUTHORIZED"
    assert "REPEATED_LOOKS_AND_PRIOR_EXPERIMENTS_NOT_CONTROLLED" in result["limitations"]


@pytest.mark.parametrize("values", [[0.0] * 10, [-.001] * 10])
def test_no_edge_cannot_be_manufactured_by_selecting_a_strategy(values):
    result = run(rows(values))
    assert result["best_observed_strategy"] == "WAIT"
    assert result["observed_max_mean_excess"] == 0
    assert result["family_p_value"] == 1


def test_independent_manual_joint_resampling_matches_and_corrects_selection():
    a = np.asarray([-.002, .0025] * 5)
    b = a[::-1]
    seed, count = 917, 199
    result = run(rows(a.tolist(), second=b.tolist()), seed=seed)
    centered_a, centered_b = a - a.mean(), b - b.mean()
    rng = np.random.Generator(np.random.PCG64(seed))
    all_max, single = [], []
    for _ in range(count):
        selected = rng.integers(0, len(a), len(a))
        error_a = sum(float(centered_a[i]) for i in selected) / len(a)
        error_b = sum(float(centered_b[i]) for i in selected) / len(a)
        all_max.append(max(0, error_a, error_b))
        single.append(max(0, error_a))
    observed = float(a.mean())
    corrected = (1 + sum(v >= observed for v in all_max)) / (count + 1)
    uncorrected = (1 + sum(v >= observed for v in single)) / (count + 1)
    assert result["family_p_value"] == corrected
    assert corrected > uncorrected
    critical = sorted(all_max)[math.ceil(.95 * (count - 1))]
    assert result["simultaneous_error_quantile"] == pytest.approx(critical)
    assert result["by_strategy"]["TREND"]["simultaneous_lower_mean_excess"] == pytest.approx(observed - critical)
    assert result["fixed_family_count_including_benchmark"] == 3


def test_sessions_receive_equal_weight_not_trade_count_weight():
    outcomes = rows([0.0] * 10)
    outcomes[0]["net_returns"]["TREND"] = .1
    # A busy session still contributes one session mean, not 100 independent days.
    for i in range(99):
        outcomes.append({"sample_id": f"extra-{i}", "session": "session-000",
                         "net_returns": {"WAIT": 0.0, "TREND": .1}})
    result = run(outcomes)
    assert result["opportunity_count"] == 109
    assert result["session_count"] == 10
    assert result["by_strategy"]["TREND"]["mean_net_return"] == pytest.approx(.01)
    assert result["session_means"][0]["opportunities"] == 100


def test_many_opportunities_in_one_session_do_not_meet_evidence_threshold():
    outcomes = rows([.002] * 1000)
    for row in outcomes:
        row["session"] = "same-session"
    result = run(outcomes)
    assert result["session_count"] == 1 and result["opportunity_count"] == 1000
    assert result["status"] == "INSUFFICIENT_SESSIONS"
    assert result["family_p_value"] is None
    assert result["observed_max_statistic"] is None
    assert result["simultaneous_error_quantile"] is None
    assert result["centered_max_statistics_digest"] is None
    assert result["by_strategy"]["TREND"]["simultaneous_lower_mean_excess"] is None
    assert result["promotion"] == "NOT_AUTHORIZED"


def test_zero_actual_no_entries_remain_in_denominator():
    outcomes = rows([.1] + [0.0] * 9)
    result = run(outcomes)
    assert result["by_strategy"]["TREND"]["mean_net_return"] == pytest.approx(.01)
    missing = copy.deepcopy(outcomes)
    del missing[-1]["net_returns"]["TREND"]
    with pytest.raises(Refused, match="INCOMPLETE_OR_UNREGISTERED_OUTCOME_FAMILY"):
        run(missing)


def test_order_independent_digest_and_seeded_result():
    outcomes = rows([-.003, .002, .004, -.001, .002] * 3)
    result = run(outcomes)
    permuted = session_bootstrap(list(reversed(outcomes)), strategy_ids=["WAIT", "TREND"], bootstrap_samples=199)
    assert result == permuted
    unhashed = dict(result)
    saved = unhashed.pop("evidence_hash")
    assert saved == digest(unhashed)


def test_evidence_does_not_mutate_inputs_and_binds_actually_consumed_returns():
    outcomes = rows([.001] * 10)
    original = copy.deepcopy(outcomes)
    before = run(outcomes)
    assert outcomes == original
    outcomes[-1]["net_returns"]["TREND"] = -.01
    after = run(outcomes)
    assert before["consumed_outcomes_digest"] != after["consumed_outcomes_digest"]
    assert before["evidence_hash"] != after["evidence_hash"]


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), True, "0.1"])
def test_missing_or_invalid_numerical_return_refuses_instead_of_dropping(bad):
    outcomes = rows([0.0] * 10)
    outcomes[-1]["net_returns"]["TREND"] = bad
    with pytest.raises(Refused, match="NONFINITE_RETURN"):
        run(outcomes)


def test_extra_strategy_cannot_be_hidden_in_a_later_outcome():
    outcomes = rows([0.0] * 10)
    outcomes[-1]["net_returns"]["UNREGISTERED"] = .1
    with pytest.raises(Refused, match="INCOMPLETE_OR_UNREGISTERED_OUTCOME_FAMILY"):
        run(outcomes)


def test_duplicate_sample_and_fictitious_wait_refuse():
    outcomes = rows([0.0] * 10)
    outcomes.append(copy.deepcopy(outcomes[0]))
    with pytest.raises(Refused, match="INVALID_OR_DUPLICATE_SAMPLE"):
        run(outcomes)
    outcomes.pop()
    outcomes[-1]["net_returns"]["WAIT"] = .1
    with pytest.raises(Refused, match="WAIT_MUST_BE_ZERO"):
        run(outcomes)


def test_empty_evidence_is_insufficient_not_an_all_clear():
    result = session_bootstrap([], strategy_ids=["TREND", "WAIT"])
    assert result["status"] == "INSUFFICIENT_SESSIONS"
    assert result["session_count"] == result["opportunity_count"] == 0
    assert result["family_p_value"] is None and result["by_strategy"] == {}


@pytest.mark.parametrize("kwargs,problem", [
    ({"strategy_ids": ["WAIT", "WAIT"]}, "INVALID_FAMILY"),
    ({"strategy_ids": ["TREND", "OTHER"]}, "BENCHMARK_NOT_REGISTERED"),
    ({"minimum_sessions": 9}, "MINIMUM_SESSIONS_BELOW_TEN"),
    ({"bootstrap_samples": 99}, "INSUFFICIENT_RESAMPLING_BUDGET"),
    ({"seed": -1}, "INVALID_SEED"),
    ({"confidence": 1.0}, "INVALID_CONFIDENCE"),
])
def test_declared_contract_validation(kwargs, problem):
    args = {"strategy_ids": ["TREND", "WAIT"], **kwargs}
    with pytest.raises(Refused, match=problem):
        session_bootstrap(rows([0.0] * 10), **args)


def test_finite_inputs_with_overflow_refuse_with_named_error():
    with pytest.raises(Refused, match="NUMERIC_FAILURE"):
        run(rows([1e308] * 10))


def test_incomplete_opportunity_paths_never_gain_inference_from_survivors():
    observations = [{'sample_id': str(i), 'session': f'day-{i:02d}', 'net_returns': {'WAIT': 0., 'LONG': .01+i*.001}} for i in range(12)]
    result = session_bootstrap(observations, strategy_ids=['WAIT','LONG'], opportunity_coverage_complete=False)
    assert result['status'] == 'INCOMPLETE_OPPORTUNITY_COVERAGE'
    assert result['family_p_value'] is None and result['bootstrap_samples_used'] == 0
    assert result['by_strategy']['LONG']['simultaneous_lower_mean_excess'] is None
    assert result['session_count'] == 12
