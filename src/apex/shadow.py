"""Observe actual readers with no ledger, positions, broker calls or orders."""
import math

from .core import Config, Refused, digest, money
from .data import regular, session, twin, visible
from .decision import evaluate, quote_at
from .forecast import predict
from .regimes import classify_regime
from .strategy_simulation import SimulationCosts, evaluate_strategies


def _strategy_preview(intelligence, state, forecast, paths, *, now, config):
    """Read causal setup flags on this forecast's paths without changing authority.

    The compact record omits scenario-by-scenario trade arrays. ``result_hash``
    is the complete evaluator result's commitment, reproducible from the saved
    inputs/model; ``preview_hash`` identifies the compact displayed record.
    """
    costs = SimulationCosts(commission_per_share=float(config.commission_per_share),
                            minimum_commission=float(config.minimum_commission))
    horizon = config.horizon_minutes
    earliest = min(horizon, max(0, math.ceil((now - forecast["price_origin_epoch"]) / 60)))
    evaluated = evaluate_strategies(paths, spot=forecast["spot"], capital=float(config.starting_cash),
        max_notional=float(config.max_notional), eligible_setups=intelligence["eligible_setups"], costs=costs,
        vwap=state["fields"]["close_weighted_vwap_proxy"], earliest_entry_step=earliest)
    compact = {key: value for key, value in evaluated.items() if key != "strategies"}
    compact["strategies"] = {name: {key: value for key, value in result.items() if key != "paths"}
                             for name, result in evaluated["strategies"].items()}
    compact.update(status="EVALUATED", scope="SINGLE_MODEL_RESEARCH_PREVIEW_NOT_ROBUST_LAB_SELECTION",
        authority="NONE_SHADOW_ONLY", forecast_id=forecast["forecast_id"], regime_id=intelligence["regime_id"],
        decision_epoch=now, price_origin_epoch=forecast["price_origin_epoch"], target_epoch=forecast["target_epoch"],
        price_basis="LATEST_COMPLETED_BAR_CLOSE_MODEL_ORIGIN_NOT_AN_EXECUTABLE_ENTRY_QUOTE",
        cost_basis={"commission": config.fee_basis, "spread_slippage": "ILLUSTRATIVE_STRATEGY_RESEARCH_ASSUMPTIONS_NOT_MEASURED"},
        selection_status="NO_STRATEGY_SELECTED_OR_AUTHORIZED", missing_worlds=["CONDITIONAL_ANALOG", "RIDGE", "STRESS_WORLDS"],
        consumption={"eligible_setups": "evaluate_strategies eligibility at current decision",
                     "forecast_paths": "evaluate_strategies identical paths across hypotheses",
                     "observed_vwap": "VWAP_REVERSION_LONG current observed target"},
        omitted_fields="Each strategy's per-scenario paths; result_hash commits to the full evaluator result")
    compact["preview_hash"] = digest(compact)
    return compact


def observe(observations, now, config: Config):
    bars, bar_conflicts = visible(observations, now=now, symbol=config.symbol, kind="bar")
    quotes, quote_conflicts = visible(observations, now=now, symbol=config.symbol, kind="quote")
    session_bars = [b for b in bars if regular(b["event_epoch"])]
    gaps = [{"after_event": left["event_epoch"], "before_event": right["event_epoch"],
             "missing_minutes": int((right["event_epoch"] - left["event_epoch"]) / 60) - 1}
            for left, right in zip(session_bars, session_bars[1:])
            if session(left["event_epoch"]) == session(right["event_epoch"]) and right["event_epoch"] - left["event_epoch"] > 60]
    health = {"visible_bars": len(bars), "visible_quotes": len(quotes),
              "bar_conflicts": len(bar_conflicts), "quote_conflicts": len(quote_conflicts),
              "latest_bar_age_from_completion_s": now - bars[-1]["event_epoch"] - 60 if bars else None,
              "latest_quote_age_from_event_s": now - quotes[-1]["event_epoch"] if quotes else None,
              "internal_regular_session_gaps": gaps,
              "gap_scope": "Between observed bars within one session; does not establish opening/closing or exchange-calendar completeness",
              "calendar_basis": "REGULAR_WEEKDAY_CLOCK_ONLY_NOT_EXCHANGE_CALENDAR"}
    result = {"now": now, "health": health, "execution_authority": "NONE_SHADOW_ONLY", "forecast": None,
              "snapshot": None, "candidate": None, "intelligence": None, "strategy_preview": None}
    if not regular(now):
        return {**result, "status": "REFUSED", "reason": "OUTSIDE_REGULAR_CLOCK_WINDOW"}
    try:
        state, returns = twin(observations, now, config.symbol)
        result["snapshot"] = state
        try:
            result["intelligence"] = classify_regime(observations, now=now, symbol=config.symbol)
        except Refused as exc:
            result["intelligence"] = {"status": "REFUSED", "reason": str(exc), "authority": "NONE_SHADOW_ONLY"}
        forecast, paths = predict(returns, state, config)
        result["forecast"] = forecast
        if result["intelligence"].get("regime_id"):
            try:
                result["strategy_preview"] = _strategy_preview(result["intelligence"], state, forecast, paths,
                                                               now=now, config=config)
            except Refused as exc:
                result["strategy_preview"] = {"status": "REFUSED", "reason": str(exc), "authority": "NONE_SHADOW_ONLY"}
        else:
            result["strategy_preview"] = {"status": "NOT_AVAILABLE", "reason": "INTELLIGENCE_REFUSED",
                                          "authority": "NONE_SHADOW_ONLY"}
        q, problem = quote_at(observations, now, config)
        candidate = evaluate(forecast, paths, q, problem, cash=money(config.starting_cash), position_open=False, config=config)
        # This candidate is a hypothetical standalone decision. Repeated shadow
        # observations are not a portfolio and never reserve or spend money.
        candidate["authority"] = "NONE_SHADOW_ONLY"
        result.update(status="OBSERVED", reason=None, forecast=forecast, candidate=candidate,
                      quote_id=q["observation_id"] if q else None,
                      candidate_input_digest=digest({"forecast_id": forecast["forecast_id"], "quote": q, "config": config.record()}))
    except Refused as exc:
        result.update(status="REFUSED", reason=str(exc))
    return result
