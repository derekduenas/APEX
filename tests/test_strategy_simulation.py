from dataclasses import FrozenInstanceError, asdict

import numpy as np
import pytest

from apex.core import Refused, digest
from apex.strategy_simulation import (
    STRATEGY_REGISTRY, STRATEGY_REGISTRY_HASH, SimulationCosts,
    evaluate_strategies, registry_record,
)

FREE = SimulationCosts(0, 0, 0, 0)
ALL = {spec.strategy_id: True for spec in STRATEGY_REGISTRY}


def run(prices, **kwargs):
    return evaluate_strategies(np.log(np.asarray(prices) / 100), spot=100, capital=10000,
                               max_notional=1050, eligible_setups=ALL, costs=FREE, **kwargs)


def strategy(prices, name, **kwargs):
    return run(prices, **kwargs)["strategies"][name]


def test_registry_is_frozen_and_complete_inputs_outputs_are_bound():
    with pytest.raises(FrozenInstanceError):
        STRATEGY_REGISTRY[1].stop_bps = 10
    assert STRATEGY_REGISTRY_HASH == digest(registry_record())
    result = run([[100, 101, 102], [100, 99, 98]], vwap=101)
    assert result["input_hash"] == digest(result["inputs"])
    assert result["result_hash"] == digest({k: v for k, v in result.items() if k != "result_hash"})
    assert result == run([[100, 101, 102], [100, 99, 98]], vwap=101)
    changed = run([[100, 101, 102], [100, 99, 97]], vwap=101)
    assert result["inputs"]["paths_sha256"] != changed["inputs"]["paths_sha256"]
    assert result["calibration"] == "UNCALIBRATED"


def test_hold_uses_all_rising_and_falling_terminal_paths_with_fixed_quantity():
    record = strategy([[100, 101, 102], [100, 99, 98]], "HOLD_LONG")
    assert record["fixed_quantity"] == 10
    assert record["paths"]["entry_step"] == [0, 0]
    assert record["paths"]["exit_step"] == [2, 2]
    assert record["paths"]["net_pnl"] == pytest.approx([20, -20])
    assert record["summary"]["expected_net"] == pytest.approx(0)
    assert record["summary"]["win_scenario_frequency"] == .5
    assert record["summary"]["cvar_05_net"] == pytest.approx(-20)
    assert record["summary"]["mean_log_growth"] == pytest.approx(np.log1p(np.array([20, -20]) / 10000).mean())
    assert record["summary"]["capital_deployed_max"] == pytest.approx(1000)


def test_wait_never_pays_cost_and_absent_setup_is_ineligible_not_a_measured_signal():
    result = evaluate_strategies(np.zeros((4, 4)), spot=100, capital=10000, max_notional=1000,
                                eligible_setups={}, costs=SimulationCosts(20, 10, 1, 2))
    wait = result["strategies"]["WAIT"]
    assert wait["paths"]["net_pnl"] == [0] * 4
    assert wait["paths"]["fees"] == [0] * 4
    assert wait["summary"]["not_triggered_count"] == 0
    assert wait["summary"]["exit_counts"] == {"WAIT": 4}
    hold = result["strategies"]["HOLD_LONG"]
    assert hold["reason"] == "SETUP_NOT_ELIGIBLE_AT_DECISION"
    assert hold["summary"]["ineligible_count"] == 4
    assert hold["summary"]["not_triggered_count"] == 0


def test_no_trigger_no_fees_and_trigger_at_horizon_is_too_late():
    record = strategy([[100, 100, 101]], "MOMENTUM_BREAKOUT_LONG")
    assert record["summary"]["not_triggered_count"] == 1
    assert record["paths"]["entry_step"] == [-1]
    assert record["paths"]["fees"] == [0]
    assert record["paths"]["net_pnl"] == [0]


def test_breakout_jump_executes_observed_price_and_stop_gap_never_fills_at_stop():
    record = strategy([[100, 101, 95, 110]], "MOMENTUM_BREAKOUT_LONG")
    assert record["paths"]["entry_step"] == [1]
    assert record["paths"]["entry_price"] == pytest.approx([101])
    assert record["paths"]["exit_step"] == [2]
    assert record["paths"]["exit_price"] == pytest.approx([95])
    assert record["paths"]["net_pnl"] == pytest.approx([-60])
    assert record["paths"]["exit_reason"] == ["STOP_GRID"]
    assert record["summary"]["loss_bound"] == "STOP_NOT_GUARANTEED_GRID_GAPS_AND_COSTS_RETAINED"


def test_target_gap_is_observed_grid_price_and_not_optimistic_intraminute_path():
    result = run([[100, 100.3, 103, 95], [100, 100.3, 95, 103]])
    record = result["strategies"]["MOMENTUM_BREAKOUT_LONG"]
    assert record["paths"]["exit_reason"] == ["TARGET_GRID", "STOP_GRID"]
    assert record["paths"]["exit_price"] == pytest.approx([103, 95])
    assert result["execution_assumptions"]["barrier_monitoring"] == "GRID_ONLY_NO_INTRAMINUTE_HIGH_LOW_OR_ORDER_INFERRED"


def test_pullback_uses_actual_lower_entry_and_then_first_target():
    record = strategy([[100, 99.5, 100.5, 98]], "PULLBACK_LONG")
    assert record["paths"]["entry_step"] == [1]
    assert record["paths"]["entry_price"] == pytest.approx([99.5])
    assert record["paths"]["exit_reason"] == ["TARGET_GRID"]
    assert record["paths"]["exit_step"] == [2]
    assert record["paths"]["net_pnl"] == pytest.approx([10])


def test_vwap_uses_current_observed_level_and_refuses_unavailable_or_passed_target():
    record = strategy([[100, 100.2, 100.8, 101.1]], "VWAP_REVERSION_LONG", vwap=101)
    assert record["paths"]["exit_step"] == [3]
    assert record["paths"]["exit_reason"] == ["TARGET_GRID"]
    assert strategy([[100, 100.2, 101]], "VWAP_REVERSION_LONG")["reason"] == "CURRENT_VWAP_UNAVAILABLE"
    assert strategy([[100, 100.2, 101]], "VWAP_REVERSION_LONG", vwap=99)["reason"] == "CURRENT_SPOT_NOT_BELOW_VWAP"
    passed = strategy([[100, 101.5, 99]], "VWAP_REVERSION_LONG", vwap=101)
    assert passed["summary"]["skipped_target_passed_count"] == 1
    assert passed["paths"]["net_pnl"] == [0]


def test_funding_is_rechecked_at_trigger_without_resizing_or_waiting_for_better_price():
    # Decision sizes ten shares. A later price of 106 needs 1,060, above the 1,050
    # cap. It cannot reduce quantity using knowledge of that future path.
    record = strategy([[100, 106, 101, 103]], "MOMENTUM_BREAKOUT_LONG")
    assert record["fixed_quantity"] == 10
    assert record["summary"]["skipped_unaffordable_count"] == 1
    assert record["paths"]["trigger_step"] == [1]
    assert record["paths"]["trigger_entry_debit"] == pytest.approx([1060])
    assert record["paths"]["quantity"] == [0]
    assert record["paths"]["net_pnl"] == [0]


def test_no_funded_whole_share_is_an_explicit_skip_even_if_future_price_gets_cheaper():
    result = evaluate_strategies(np.log(np.array([[100, 10, 20]]) / 100), spot=100, capital=50,
                                max_notional=1000, eligible_setups=ALL, costs=FREE)
    record = result["strategies"]["PULLBACK_LONG"]
    assert record["fixed_quantity"] == 0
    assert record["summary"]["skipped_unaffordable_count"] == 1
    assert record["paths"]["quantity"] == [0]


def test_cost_assumption_can_flip_positive_gross_opportunity_negative():
    paths = np.log(np.array([[100, 100.05, 100.1]]) / 100)
    common = dict(spot=100, capital=10000, max_notional=1050, eligible_setups=ALL)
    free = evaluate_strategies(paths, costs=FREE, **common)["strategies"]["HOLD_LONG"]
    cost = evaluate_strategies(paths, costs=SimulationCosts(10, 5, .05, 1), **common)
    hold = cost["strategies"]["HOLD_LONG"]
    assert free["summary"]["expected_net"] > 0
    assert hold["summary"]["expected_net"] < 0
    assert hold["paths"]["fees"] == [2]
    assert cost["strategies"]["WAIT"]["paths"]["fees"] == [0]
    assert cost["inputs"]["costs"] == asdict(SimulationCosts(10, 5, .05, 1))


def test_fixed_quantity_reserves_minimum_entry_commission_inside_budget():
    result = evaluate_strategies(np.zeros((1, 3)), spot=100, capital=1000, max_notional=1000,
                                eligible_setups={"HOLD_LONG": True}, costs=SimulationCosts(0, 0, 0, 2))
    record = result["strategies"]["HOLD_LONG"]
    assert record["fixed_quantity"] == 9
    assert record["paths"]["entry_debit"] == [902]
    assert record["paths"]["net_pnl"] == [-4]


def test_fully_funded_gap_can_exhaust_account_and_log_growth_is_not_fabricated():
    result = evaluate_strategies(np.array([[0., -700.]]), spot=100, capital=100.01, max_notional=100.01,
                                eligible_setups={"HOLD_LONG": True}, costs=SimulationCosts(0, 0, 0, .01))
    record = result["strategies"]["HOLD_LONG"]
    assert record["summary"]["capital_exhaustion_scenario_count"] == 1
    assert record["summary"]["mean_log_growth"] is None


@pytest.mark.parametrize("paths", [[], [[0]], [[1, 2]], [[0, np.nan]], [[0, np.inf]], [[0, 1000]], [[0, -1000]], [1, 2]])
def test_invalid_paths_refuse(paths):
    with pytest.raises(Refused):
        evaluate_strategies(paths, spot=100, capital=1000, max_notional=1000, eligible_setups=ALL)


@pytest.mark.parametrize("cost", [dict(half_spread_bps=-1), dict(slippage_bps=np.nan), dict(commission_per_share=-1),
                                  dict(minimum_commission=True), dict(half_spread_bps=10000)])
def test_invalid_costs_refuse(cost):
    with pytest.raises(Refused):
        SimulationCosts(**cost)


@pytest.mark.parametrize("flags", [{"UNKNOWN": True}, {"HOLD_LONG": 1}, {"WAIT": False}, None])
def test_unknown_or_ambiguous_eligibility_refuses(flags):
    with pytest.raises(Refused):
        evaluate_strategies(np.zeros((1, 3)), spot=100, capital=1000, max_notional=1000, eligible_setups=flags)


def test_fixed_quantity_override_preserves_proposal_across_cost_stress_worlds():
    paths = np.zeros((2, 3))
    common = dict(spot=100, capital=1000, max_notional=1000, eligible_setups={"HOLD_LONG": True})
    original = evaluate_strategies(paths, costs=FREE, **common)
    qty = original["strategies"]["HOLD_LONG"]["fixed_quantity"]
    assert qty == 10
    stressed = evaluate_strategies(paths, costs=SimulationCosts(0, 0, 0, 1), fixed_quantity=qty, **common)
    hold = stressed["strategies"]["HOLD_LONG"]
    assert hold["fixed_quantity"] == 10
    assert hold["summary"]["skipped_unaffordable_count"] == 2
    assert hold["paths"]["quantity"] == [0, 0]
    assert stressed["inputs"]["quantity_basis"] == "CALLER_FROZEN_DECISION_QUANTITY"
    assert stressed["strategies"]["WAIT"]["paths"]["fees"] == [0, 0]


@pytest.mark.parametrize("qty", [-1, True, 1.2, 2**53])
def test_invalid_fixed_quantities_refuse(qty):
    with pytest.raises(Refused, match="INVALID_FIXED_STRATEGY_QUANTITY"):
        evaluate_strategies(np.zeros((1, 3)), spot=100, capital=1000, max_notional=1000,
                            eligible_setups=ALL, fixed_quantity=qty)


def test_signal_after_price_origin_cannot_buy_that_past_price():
    prices = np.array([[100, 110, 111], [100, 90, 91]])
    common = dict(spot=100, capital=10000, max_notional=1050,
                  eligible_setups={"HOLD_LONG": True}, costs=FREE)
    before = evaluate_strategies(np.log(prices / 100), **common)["strategies"]["HOLD_LONG"]
    delayed = evaluate_strategies(np.log(prices / 100), earliest_entry_step=1, **common)
    record = delayed["strategies"]["HOLD_LONG"]
    assert before["paths"]["entry_step"] == [0, 0]
    assert record["paths"]["entry_step"] == [-1, 1]
    assert record["paths"]["entry_price"] == pytest.approx([0, 90])
    assert record["paths"]["net_pnl"] == pytest.approx([0, 10])
    assert record["summary"]["skipped_unaffordable_count"] == 1
    assert delayed["inputs"]["horizon_minutes"] == 2
    assert delayed["inputs"]["maximum_remaining_hold_minutes"] == 1


def test_trigger_before_signal_is_ignored_and_horizon_is_not_extended():
    record = evaluate_strategies(np.log(np.array([[100, 100.5, 99.9, 100.3, 100.4]]) / 100),
                                spot=100, capital=10000, max_notional=1050, costs=FREE,
                                eligible_setups={"MOMENTUM_BREAKOUT_LONG": True}, earliest_entry_step=2)
    breakout = record["strategies"]["MOMENTUM_BREAKOUT_LONG"]
    assert breakout["paths"]["entry_step"] == [3]
    assert breakout["paths"]["exit_step"] == [4]
    assert breakout["paths"]["exit_reason"] == ["HORIZON"]
    assert breakout["paths"]["net_pnl"] == pytest.approx([1])


def test_no_entry_when_first_allowed_grid_is_already_the_target():
    record = evaluate_strategies(np.zeros((2, 2)), spot=100, capital=1000, max_notional=1000,
                                eligible_setups=ALL, costs=FREE, earliest_entry_step=1)
    for sid, strategy_result in record["strategies"].items():
        assert strategy_result["paths"]["net_pnl"] == [0, 0]
        assert strategy_result["paths"]["fees"] == [0, 0]
    assert record["strategies"]["HOLD_LONG"]["paths"]["exit_reason"] == ["NO_ENTRY_WINDOW_BEFORE_TARGET"] * 2


@pytest.mark.parametrize("step", [-1, True, .5, 3])
def test_invalid_earliest_entry_grid_refuses(step):
    with pytest.raises(Refused, match="INVALID_EARLIEST_STRATEGY_ENTRY_STEP"):
        evaluate_strategies(np.zeros((1, 3)), spot=100, capital=1000, max_notional=1000,
                            eligible_setups=ALL, earliest_entry_step=step)
