"""Frozen research strategies evaluated on identical one-minute price paths.

This module simulates hypotheses, not broker executions. Prices between grid
observations are unknown: barriers execute at the first observed grid price,
including jumps beyond a stop. A stop is never a guaranteed loss bound.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import math
from collections.abc import Mapping

import numpy as np

from .core import Refused, digest


@dataclass(frozen=True)
class SimulationCosts:
    """Per-side execution assumptions; continuous dollars, no broker rounding."""
    half_spread_bps: float = 2.0
    slippage_bps: float = 1.0
    commission_per_share: float = 0.005
    minimum_commission: float = 0.01

    def __post_init__(self):
        for name, value in asdict(self).items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise Refused("INVALID_STRATEGY_COST:" + name)
        if self.half_spread_bps + self.slippage_bps >= 10000:
            raise Refused("STRATEGY_EXECUTION_FRICTION_AT_LEAST_PRICE")


@dataclass(frozen=True)
class StrategySpec:
    strategy_id: str
    entry_rule: str
    entry_offset_bps: float | None
    stop_bps: float | None
    target_bps: float | None
    target_rule: str


# Parameters are research hypotheses fixed independently of evaluation data.
# VWAP's bounce trigger is only meaningful when the caller observes an eligible
# below-VWAP setup; the current VWAP is frozen and never updated from paths.
STRATEGY_REGISTRY = (
    StrategySpec("WAIT", "NEVER", None, None, None, "NONE"),
    StrategySpec("HOLD_LONG", "FIRST_ALLOWED_GRID", 0.0, None, None, "HORIZON_ONLY"),
    StrategySpec("MOMENTUM_BREAKOUT_LONG", "FIRST_GRID_AT_OR_ABOVE", 20.0, 40.0, 80.0, "ENTRY_RELATIVE"),
    StrategySpec("PULLBACK_LONG", "FIRST_GRID_AT_OR_BELOW", -25.0, 45.0, 90.0, "ENTRY_RELATIVE"),
    StrategySpec("VWAP_REVERSION_LONG", "FIRST_GRID_AT_OR_ABOVE", 10.0, 40.0, None, "CURRENT_OBSERVED_VWAP"),
)


def registry_record() -> dict:
    return {"schema": "STRATEGY_REGISTRY_V1", "strategies": [asdict(spec) for spec in STRATEGY_REGISTRY],
            "parameter_basis": "FIXED_RESEARCH_HYPOTHESES_NOT_OPTIMIZED_ON_EVALUATION_PATHS",
            "barrier_basis": "ONE_MINUTE_GRID_ONLY_FIRST_OBSERVED_PRICE_NOT_THRESHOLD",
            "entry_window": "FIRST_ALLOWED_GRID_OR_LATER_TRIGGER_STRICTLY_BEFORE_HORIZON",
            "position": "FUNDED_LONG_WHOLE_SHARES_ONLY_ONE_ENTRY_PER_SCENARIO"}


STRATEGY_REGISTRY_HASH = digest(registry_record())


def _number(value, name, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise Refused("INVALID_STRATEGY_INPUT:" + name)
    if value < 0 or (positive and value == 0):
        raise Refused("INVALID_STRATEGY_INPUT:" + name)
    return float(value)


def _quantity(spot, budget, costs):
    """Size once from information at decision, including assumed entry costs."""
    unit_price = spot * (1 + (costs.half_spread_bps + costs.slippage_bps) / 10000)
    if not math.isfinite(unit_price):
        raise Refused("NONFINITE_STRATEGY_ENTRY_PRICE")
    ratio = budget / (unit_price + costs.commission_per_share)
    if not math.isfinite(ratio) or ratio > 2**53 - 1:
        raise Refused("STRATEGY_QUANTITY_OUTSIDE_EXACT_INTEGER_RANGE")
    qty = int(math.floor(ratio))
    if qty and unit_price * qty + max(costs.minimum_commission, costs.commission_per_share * qty) > budget:
        qty = max(0, int(math.floor((budget - costs.minimum_commission) / unit_price)))
    if qty > 2**53 - 1:
        raise Refused("STRATEGY_QUANTITY_OUTSIDE_EXACT_INTEGER_RANGE")
    return qty


def _summary(net, deployed, *, capital, eligible, entered, triggered, unaffordable, target_passed, reasons):
    # CVaR is the exact lower 5% probability mass of the empirical distribution,
    # with fractional weight on its last order statistic. It is not a guarantee.
    n = len(net)
    mass = 0.05 * n
    sorted_net = np.sort(net)
    whole = int(math.floor(mass))
    fraction = mass - whole
    cvar = (float(sorted_net[:whole].sum()) + (float(sorted_net[whole]) * fraction if fraction else 0.0)) / mass
    ending = capital + net
    ruined = ending <= 0
    log_growth = None if ruined.any() else float(np.log1p(net / capital).mean())
    return {"scenario_count": n, "entered_count": int(entered.sum()),
            "ineligible_count": n if not eligible else 0,
            "triggered_count": int(triggered.sum()),
            "not_triggered_count": int((~triggered).sum()) if eligible else 0,
            "skipped_unaffordable_count": int(unaffordable.sum()),
            "skipped_target_passed_count": int(target_passed.sum()),
            "expected_net": float(net.mean()), "median_net": float(np.median(net)),
            "cvar_05_net": cvar, "worst_scenario_net": float(net.min()),
            "mean_log_growth": log_growth, "capital_exhaustion_scenario_count": int(ruined.sum()),
            "win_scenario_frequency": float((net > 0).mean()),
            "loss_scenario_frequency": float((net < 0).mean()),
            "no_trade_scenario_frequency": float((~entered).mean()),
            "conditional_entered_win_frequency": float((net[entered] > 0).mean()) if entered.any() else None,
            "capital_deployed_mean": float(deployed.mean()), "capital_deployed_max": float(deployed.max()),
            "capital_deployed_quantiles": {str(q): float(np.quantile(deployed, q)) for q in (0.05, 0.5, 0.95)},
            "capital_deployed_min_when_entered": float(deployed[entered].min()) if entered.any() else None,
            "exit_counts": {reason: int(np.sum(reasons == reason)) for reason in sorted(set(reasons.tolist()))},
            "distribution_basis": "UNCALIBRATED_MODEL_SCENARIO_FREQUENCIES",
            "loss_bound": "STOP_NOT_GUARANTEED_GRID_GAPS_AND_COSTS_RETAINED"}


def evaluate_strategies(log_paths, *, spot: float, capital: float, max_notional: float,
                        eligible_setups: Mapping[str, bool], costs: SimulationCosts = SimulationCosts(),
                        vwap: float | None = None, fixed_quantity: int | None = None,
                        earliest_entry_step: int = 0) -> dict:
    """Vectorized path tournament; all strategies consume the exact same paths.

    ``log_paths`` has shape ``(scenarios, H + 1)`` and origin column zero.
    Eligibility is a caller-observed setup condition, never inferred from future
    simulated performance. Unspecified setups remain ineligible. Entry quantity
    is fixed at the decision's price and is never increased or reduced using a
    future path. A triggered entry that cannot fund that quantity is skipped.
    ``fixed_quantity`` lets stress worlds and later realized paths retain the
    original proposed quantity; changed costs never quietly resize an order.
    ``earliest_entry_step`` prevents a signal computed after a bar close from
    retroactively entering at that close. Horizon stays fixed at the supplied
    path target, so delayed entries have a shorter remaining holding period.
    """
    spot = _number(spot, "spot", positive=True)
    capital = _number(capital, "capital", positive=True)
    max_notional = _number(max_notional, "max_notional")
    if vwap is not None:
        vwap = _number(vwap, "vwap", positive=True)
    if type(costs) is not SimulationCosts:
        raise Refused("STRATEGY_COST_POLICY_NOT_CANONICAL")
    if not isinstance(eligible_setups, Mapping):
        raise Refused("STRATEGY_SETUP_FLAGS_REQUIRED")
    known = {spec.strategy_id for spec in STRATEGY_REGISTRY}
    if set(eligible_setups) - known or any(type(flag) is not bool for flag in eligible_setups.values()):
        raise Refused("INVALID_STRATEGY_SETUP_FLAGS")
    if eligible_setups.get("WAIT", True) is not True:
        raise Refused("WAIT_CANNOT_BE_DISABLED")
    try:
        paths = np.asarray(log_paths, dtype=np.float64)
    except (ValueError, TypeError) as exc:
        raise Refused("INVALID_STRATEGY_PATHS") from exc
    if paths.ndim != 2 or paths.shape[0] < 1 or paths.shape[1] < 2 or not np.isfinite(paths).all():
        raise Refused("INVALID_STRATEGY_PATHS")
    if not np.equal(paths[:, 0], 0).all():
        raise Refused("STRATEGY_PATH_ORIGIN_NOT_ZERO")
    with np.errstate(over="ignore", under="ignore", invalid="ignore"):
        prices = spot * np.exp(paths)
    if not np.isfinite(prices).all() or (prices <= 0).any():
        raise Refused("NONFINITE_OR_NONPOSITIVE_STRATEGY_PRICE_PATH")
    # Portable little-endian float64 bytes; shape is committed separately.
    path_hash = hashlib.sha256(np.ascontiguousarray(paths, dtype="<f8").tobytes()).hexdigest()
    n, columns = prices.shape
    horizon = columns - 1
    if type(earliest_entry_step) is not int or not 0 <= earliest_entry_step <= horizon:
        raise Refused("INVALID_EARLIEST_STRATEGY_ENTRY_STEP")
    budget = min(capital, max_notional)
    if fixed_quantity is not None and (type(fixed_quantity) is not int or not 0 <= fixed_quantity <= 2**53 - 1):
        raise Refused("INVALID_FIXED_STRATEGY_QUANTITY")
    qty = _quantity(spot, budget, costs) if fixed_quantity is None else fixed_quantity
    fee = max(costs.minimum_commission, costs.commission_per_share * qty) if qty else 0.0
    friction = (costs.half_spread_bps + costs.slippage_bps) / 10000
    flags = {name: (True if name == "WAIT" else eligible_setups.get(name, False)) for name in sorted(known)}
    inputs = {"spot": spot, "capital": capital, "max_notional": max_notional, "funded_budget": budget,
              "eligible_setups": flags, "current_observed_vwap": vwap, "costs": asdict(costs),
              "path_shape": list(paths.shape), "paths_sha256": path_hash, "path_dtype": "LITTLE_ENDIAN_FLOAT64",
              "grid_minutes": 1, "horizon_minutes": horizon, "registry_hash": STRATEGY_REGISTRY_HASH,
              "fixed_quantity_override": fixed_quantity,
              "earliest_entry_step": earliest_entry_step,
              "maximum_remaining_hold_minutes": horizon - earliest_entry_step,
              "quantity_basis": "CALLER_FROZEN_DECISION_QUANTITY" if fixed_quantity is not None else "COMPUTED_FROM_DECISION_PRICE_AND_COSTS"}
    result = {"schema": "STRATEGY_SIMULATION_V1", "authority": "OFFLINE_RESEARCH_ONLY",
              "calibration": "UNCALIBRATED", "inputs": inputs, "input_hash": digest(inputs),
              "registry": registry_record(), "registry_hash": STRATEGY_REGISTRY_HASH,
              "execution_assumptions": {"price_basis": "SIMULATED_REFERENCE_PRICE_WITH_FIXED_BPS_FRICTION",
                "cost_basis": "RESEARCH_ASSUMPTIONS_NOT_LIVE_QUOTES_OR_BROKER_FEES",
                "fee_rounding": "NONE_CONTINUOUS_DOLLARS",
                "barrier_monitoring": "GRID_ONLY_NO_INTRAMINUTE_HIGH_LOW_OR_ORDER_INFERRED",
                "entry_execution": "ACTUAL_FIRST_QUALIFYING_GRID_PRICE_PLUS_FRICTION_NOT_TRIGGER_THRESHOLD",
                "entry_time": "AT_OR_AFTER_EARLIEST_ALLOWED_GRID_AND_STRICTLY_BEFORE_FIXED_TARGET",
                "exit_execution": "ACTUAL_FIRST_ELIGIBLE_GRID_PRICE_MINUS_FRICTION_NOT_STOP_OR_TARGET",
                "sizing": "FIXED_AT_DECISION_WITH_ENTRY_COSTS_RECHECK_FUNDING_AT_TRIGGER_NO_FUTURE_RESIZING",
                "funding": "WHOLE_SHARE_PURCHASE_PLUS_ENTRY_FEE_WITHIN_MIN_CAPITAL_NOTIONAL_CAP",
                "signal_law": "CALLER_ELIGIBILITY_FROZEN_AT_DECISION_NOT_RESELECTED_FROM_PATHS"},
              "strategies": {}}
    row_ids = np.arange(n)
    grid = np.arange(columns)[None, :]
    for spec in STRATEGY_REGISTRY:
        strategy_id = spec.strategy_id
        eligible = flags[strategy_id]
        eligibility_reason = None if eligible else "SETUP_NOT_ELIGIBLE_AT_DECISION"
        if strategy_id == "VWAP_REVERSION_LONG" and eligible:
            if vwap is None:
                eligible, eligibility_reason = False, "CURRENT_VWAP_UNAVAILABLE"
            elif vwap <= spot:
                eligible, eligibility_reason = False, "CURRENT_SPOT_NOT_BELOW_VWAP"
        entry_steps = np.full(n, -1, dtype=np.int64)
        if eligible and strategy_id == "HOLD_LONG" and earliest_entry_step < horizon:
            entry_steps[:] = earliest_entry_step
        elif eligible and strategy_id not in ("WAIT", "HOLD_LONG"):
            level = spot * (1 + spec.entry_offset_bps / 10000)
            if spec.entry_rule == "FIRST_GRID_AT_OR_ABOVE":
                crossing = prices >= level
            else:
                crossing = prices <= level
            crossing[:, :max(earliest_entry_step, 1)] = False
            crossing[:, -1] = False  # no new positions at a completed horizon
            triggered_rows = crossing.any(axis=1)
            entry_steps[triggered_rows] = crossing[triggered_rows].argmax(axis=1)
        triggered = entry_steps >= 0
        entry_mid = np.zeros(n)
        entry_mid[triggered] = prices[row_ids[triggered], entry_steps[triggered]]
        entry_price = entry_mid * (1 + friction)
        debit = entry_price * qty + fee
        target_passed = triggered & (entry_mid >= vwap) if spec.target_rule == "CURRENT_OBSERVED_VWAP" and vwap else np.zeros(n, dtype=bool)
        unaffordable = triggered & ((debit > budget) | (qty <= 0))
        target_passed &= ~unaffordable  # disjoint skip accounting, funding checked first
        entered = triggered & ~unaffordable & ~target_passed
        trigger_steps = entry_steps.copy()
        trigger_mid = entry_mid.copy()
        trigger_debit = np.where(triggered, debit, 0.0)
        # Attempting once at the first trigger is part of the frozen strategy.
        # An unaffordable order is not retried at a later, more favorable price.
        entry_steps[~entered] = -1
        entry_mid[~entered] = 0
        entry_price[~entered] = 0
        debit[~entered] = 0
        reasons = np.full(n, "NO_TRIGGER", dtype=object)
        if not eligible:
            reasons[:] = "INELIGIBLE"
        if strategy_id == "WAIT":
            reasons[:] = "WAIT"
        elif eligible and earliest_entry_step == horizon:
            reasons[:] = "NO_ENTRY_WINDOW_BEFORE_TARGET"
        reasons[unaffordable] = "UNAFFORDABLE_AT_TRIGGER"
        reasons[target_passed & ~unaffordable] = "TARGET_ALREADY_PASSED_AT_TRIGGER"
        exit_steps = np.full(n, -1, dtype=np.int64)
        exit_steps[entered] = horizon
        reasons[entered] = "HORIZON"
        if entered.any() and spec.stop_bps is not None:
            later = (grid > entry_steps[:, None]) & entered[:, None]
            stops = prices <= entry_mid[:, None] * (1 - spec.stop_bps / 10000)
            target = np.full(n, vwap) if spec.target_rule == "CURRENT_OBSERVED_VWAP" else entry_mid * (1 + spec.target_bps / 10000)
            targets = prices >= target[:, None]
            stop_hits, target_hits = later & stops, later & targets
            hit = stop_hits | target_hits
            hit_rows = hit.any(axis=1)
            first = hit[hit_rows].argmax(axis=1)
            exit_steps[hit_rows] = first
            hit_ids = row_ids[hit_rows]
            reasons[hit_rows] = np.where(stop_hits[hit_ids, first], "STOP_GRID", "TARGET_GRID")
        exit_mid = np.zeros(n)
        exit_mid[entered] = prices[row_ids[entered], exit_steps[entered]]
        exit_price = exit_mid * (1 - friction)
        credits = exit_price * qty - np.where(entered, fee, 0.0)
        net = credits - debit
        if not np.isfinite(net).all() or not np.isfinite(debit).all():
            raise Refused("NONFINITE_STRATEGY_ACCOUNTING")
        summary = _summary(net, debit, capital=capital, eligible=eligible, entered=entered, triggered=triggered,
                           unaffordable=unaffordable, target_passed=target_passed, reasons=reasons)
        quantities = np.where(entered, qty, 0)
        summary["quantity_counts"] = {str(int(q)): int(np.sum(quantities == q)) for q in np.unique(quantities)}
        # WAIT is an actual no-trade competitor, not a failed trigger.
        if strategy_id == "WAIT":
            summary["not_triggered_count"] = 0
        trades = {"trigger_step": trigger_steps.tolist(), "trigger_mid": trigger_mid.tolist(),
                  "trigger_entry_debit": trigger_debit.tolist(),
                  "entry_step": entry_steps.tolist(), "exit_step": exit_steps.tolist(),
                  "entry_mid": entry_mid.tolist(), "exit_mid": exit_mid.tolist(),
                  "entry_price": entry_price.tolist(), "exit_price": exit_price.tolist(),
                  "quantity": np.where(entered, qty, 0).tolist(), "entry_debit": debit.tolist(),
                  "exit_credit": credits.tolist(), "fees": np.where(entered, 2 * fee, 0.0).tolist(),
                  "net_pnl": net.tolist(), "exit_reason": reasons.tolist()}
        result["strategies"][strategy_id] = {"spec": asdict(spec), "spec_hash": digest(asdict(spec)),
                                             "eligible": eligible, "reason": eligibility_reason,
                                             "fixed_quantity": qty if strategy_id != "WAIT" else 0,
                                             "summary": summary, "paths": trades}
    try:
        result["result_hash"] = digest(result)
    except (ValueError, OverflowError) as exc:
        raise Refused("NONFINITE_STRATEGY_SUMMARY") from exc
    return result
