"""Observe actual readers with no ledger, positions, broker calls or orders."""
from .core import Config, Refused, digest, money
from .data import regular, session, twin, visible
from .decision import evaluate, quote_at
from .forecast import predict


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
              "snapshot": None, "candidate": None}
    if not regular(now):
        return {**result, "status": "REFUSED", "reason": "OUTSIDE_REGULAR_CLOCK_WINDOW"}
    try:
        state, returns = twin(observations, now, config.symbol)
        result["snapshot"] = state
        forecast, paths = predict(returns, state, config)
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
