"""Causal, descriptive market regimes and premarket context for the strategy lab.

These are fixed research hypotheses, not learned state probabilities. Realized
volatility is sqrt(sum of squared minute log returns), without annualization;
the measurement convention follows Andersen et al., NBER WP 8160 (2001),
https://www.nber.org/papers/w8160. The thresholds and setups below are our
declared hypotheses; the paper does not validate them or confer trading edge.
"""
from __future__ import annotations

import math
from datetime import datetime
from statistics import median

from .core import Refused, digest, finite
from .data import EASTERN, regular, session, visible


POLICY = {
    "schema": "APEX_DESCRIPTIVE_REGIME_POLICY_V1", "trend_window_returns": 30,
    "comparison_window_returns": 15, "minimum_prior_sessions": 3,
    "maximum_prior_sessions": 20, "trend_efficiency_threshold": .35,
    "high_volatility_ratio": 1.5, "low_volatility_ratio": 2 / 3,
    "high_relative_volume": 1.5, "low_relative_volume": 2 / 3,
    "maximum_bar_age_seconds": 120, "maximum_quote_age_seconds": 15,
    "tight_spread_bps": 5.0, "wide_spread_bps": 20.0,
    "gap_continuation_minimum": .003,
}


def _minute(epoch: float) -> int:
    dt = datetime.fromtimestamp(epoch, EASTERN)
    return dt.hour * 60 + dt.minute


def _block(bars: list[dict], count: int) -> list[dict]:
    block = bars[-count:]
    if len(block) != count or any(b["event_epoch"] - a["event_epoch"] != 60
                                  for a, b in zip(block, block[1:])):
        return []
    return block


def _returns(bars: list[dict]) -> list[float]:
    return [math.log(b["close"]) - math.log(a["close"]) for a, b in zip(bars, bars[1:])]


def _range(bars: list[dict]) -> float:
    return (max(b["high"] for b in bars) - min(b["low"] for b in bars)) / bars[-1]["close"]


def _ratio(value, benchmark):
    return value / benchmark if value is not None and benchmark is not None and benchmark > 0 else None


def _premarket(bars: list[dict], *, now: float, today: str) -> tuple[dict, list[dict]]:
    prior = [b for b in bars if regular(b["event_epoch"]) and session(b["event_epoch"]) < today]
    prior_date = session(prior[-1]["event_epoch"]) if prior else None
    # An incomplete prior session is not quietly turned into a closing print.
    closing = next((b for b in reversed(prior) if session(b["event_epoch"]) == prior_date
                    and _minute(b["event_epoch"]) == 959), None)
    pre = [b for b in bars if session(b["event_epoch"]) == today and 240 <= _minute(b["event_epoch"]) < 570]
    opening = next((b for b in bars if session(b["event_epoch"]) == today and _minute(b["event_epoch"]) == 570), None)
    reference = opening["open"] if opening else (pre[-1]["close"] if pre else None)
    previous_close = closing["close"] if closing else None
    out = {
        "authority": "PRIOR_CONTEXT_ONLY", "previous_observed_session": prior_date,
        "previous_regular_close": previous_close,
        "previous_close_status": "OBSERVED_1559_COMPLETED_BAR" if closing else "NOT_AVAILABLE",
        "gap_fraction": reference / previous_close - 1 if reference is not None and previous_close is not None else None,
        "gap_reference": "REGULAR_OPEN" if opening else ("LATEST_AVAILABLE_PREMARKET_CLOSE" if pre else "NOT_AVAILABLE"),
        "regular_open": opening["open"] if opening else None,
        "premarket_high": max(b["high"] for b in pre) if pre else None,
        "premarket_low": min(b["low"] for b in pre) if pre else None,
        "premarket_last_close": pre[-1]["close"] if pre else None,
        "premarket_volume": sum(b["volume"] for b in pre) if pre else None,
        "premarket_bars": len(pre), "coverage": "OBSERVED_BARS_ONLY_NOT_COMPLETE_FEED_COVERAGE",
        "last_content_completion_epoch": pre[-1]["event_epoch"] + 60 if pre else None,
        "last_content_available_epoch": pre[-1]["available_epoch"] if pre else None,
        "content_age_seconds": now - (pre[-1]["event_epoch"] + 60) if pre else None,
        "news_catalyst_status": "NOT_SUPPLIED", "calendar_status": "NOT_SUPPLIED",
    }
    return out, pre + ([closing] if closing else []) + ([opening] if opening else [])


def classify_regime(observations: list[dict], *, now: float, symbol: str) -> dict:
    """Read normalized observations AS OF now and emit auditable setup eligibility.

    Historical baselines use prior-session windows ending at the *same Eastern
    minute* as the current window. No later bars from those sessions, today's
    final volume, or future correction can enter a baseline. Missing support
    remains explicit. A valid pre-open call produces context and WAIT only.
    """
    if not finite(now) or not isinstance(symbol, str) or not symbol:
        raise Refused("INVALID_REGIME_REQUEST")
    today = session(now)
    all_bars, conflicts = visible(observations, now=now, symbol=symbol, kind="bar")
    bars = [b for b in all_bars if b["event_epoch"] + 60 <= now]
    regular_bars = [b for b in bars if regular(b["event_epoch"])]
    current = [b for b in regular_bars if session(b["event_epoch"]) == today]
    premarket, used = _premarket(bars, now=now, today=today)
    fresh = bool(current and 0 <= now - (current[-1]["event_epoch"] + 60) <= POLICY["maximum_bar_age_seconds"])
    block31 = _block(current, 31) if fresh else []
    block16 = _block(current, 16) if fresh else []
    block6 = _block(current, 6) if fresh else []
    r30, r15 = _returns(block31), _returns(block16)
    ret30 = sum(r30) if r30 else None
    efficiency = abs(ret30) / sum(map(abs, r30)) if r30 and sum(map(abs, r30)) > 0 else (0.0 if r30 else None)
    rv15 = math.hypot(*r15) if r15 else None
    ret15 = sum(r15) if r15 else None
    ret5 = sum(_returns(block6)) if block6 else None
    last_return = r15[-1] if r15 else None
    spot = current[-1]["close"] if fresh else None
    volume = sum(b["volume"] for b in current) if current else None
    vwap = sum(b["close"] * b["volume"] for b in current) / volume if volume else None
    distance = math.log(spot / vwap) if spot is not None and vwap is not None else None
    z_vwap = _ratio(distance, rv15)
    used.extend(current)

    by_session: dict[str, dict[int, dict]] = {}
    for bar in regular_bars:
        date = session(bar["event_epoch"])
        if date < today:
            by_session.setdefault(date, {})[_minute(bar["event_epoch"])] = bar
    baselines = []
    if block16:
        minutes = [_minute(b["event_epoch"]) for b in block16]
        for date in sorted(by_session)[-POLICY["maximum_prior_sessions"]:]:
            mapping = by_session[date]
            if not all(m in mapping for m in minutes):
                continue
            block = [mapping[m] for m in minutes]
            # 16 closing prices produce 15 returns; the corresponding volume
            # and high/low range cover the last 15 bars, excluding the anchor.
            baselines.append({"session": date, "realized_vol_15": math.hypot(*_returns(block)),
                              "volume_15": sum(b["volume"] for b in block[1:]), "range_15": _range(block[1:]),
                              "input_ids": [b["observation_id"] for b in block]})
            used.extend(block)
    supported = len(baselines) >= POLICY["minimum_prior_sessions"]
    volume15 = sum(b["volume"] for b in block16[1:]) if block16 else None
    range15 = _range(block16[1:]) if block16 else None
    vol_ratio = _ratio(rv15, median(b["realized_vol_15"] for b in baselines)) if supported else None
    relative_volume = _ratio(volume15, median(b["volume_15"] for b in baselines)) if supported else None
    range_ratio = _ratio(range15, median(b["range_15"] for b in baselines)) if supported else None
    if efficiency is None:
        trend = "TREND_UNOBSERVED"
    elif efficiency >= POLICY["trend_efficiency_threshold"] and ret30 != 0:
        trend = "UP_TREND" if ret30 > 0 else "DOWN_TREND"
    else:
        trend = "RANGE"
    volatility = ("VOL_UNOBSERVED" if vol_ratio is None else "HIGH_VOL" if vol_ratio >= POLICY["high_volatility_ratio"]
                  else "LOW_VOL" if vol_ratio <= POLICY["low_volatility_ratio"] else "NORMAL_VOL")
    participation = ("PARTICIPATION_UNOBSERVED" if relative_volume is None else "HIGH_PARTICIPATION"
                     if relative_volume >= POLICY["high_relative_volume"] else "LOW_PARTICIPATION"
                     if relative_volume <= POLICY["low_relative_volume"] else "NORMAL_PARTICIPATION")

    quotes, quote_conflicts = visible(observations, now=now, symbol=symbol, kind="quote")
    quote = quotes[-1] if quotes else None
    quote_age = now - quote["event_epoch"] if quote else None
    usable_quote = quote is not None and 0 <= quote_age <= POLICY["maximum_quote_age_seconds"]
    spread = 10000 * (quote["ask"] - quote["bid"]) / ((quote["ask"] + quote["bid"]) / 2) if usable_quote else None
    liquidity = ("QUOTE_UNOBSERVED" if quote is None else "QUOTE_STALE_OR_FUTURE" if not usable_quote
                 else "WIDE_SPREAD" if spread > POLICY["wide_spread_bps"]
                 else "TIGHT_SPREAD" if spread <= POLICY["tight_spread_bps"] else "NORMAL_SPREAD")
    if quote:
        used.append(quote)
    breakout = bool(block16 and spot > max(b["high"] for b in block16[:-1]))
    gap_continuation = bool(spot is not None and premarket["gap_fraction"] is not None
                            and premarket["gap_fraction"] >= POLICY["gap_continuation_minimum"]
                            and premarket["premarket_high"] is not None and spot > premarket["premarket_high"])
    eligible = {
        "WAIT": True,
        "HOLD_LONG": bool(block31),
        "MOMENTUM_BREAKOUT_LONG": bool(block31 and breakout and (trend == "UP_TREND" or gap_continuation)),
        "PULLBACK_LONG": bool(block31 and trend == "UP_TREND" and distance is not None and distance > 0
                              and ret5 < 0 and last_return > 0),
        "VWAP_REVERSION_LONG": bool(block31 and trend == "RANGE" and z_vwap is not None and z_vwap < -1
                                    and last_return > 0),
    }
    features = {"ret_15": ret15, "ret_30": ret30, "ret_5": ret5, "last_return": last_return,
                "trend_efficiency_30": efficiency, "realized_vol_15": rv15, "volatility_ratio": vol_ratio,
                "relative_volume_15": relative_volume, "range_ratio_15": range_ratio,
                "vwap_distance_vol_units": z_vwap, "distance_to_vwap": distance,
                "close_weighted_vwap_proxy": vwap, "breakout_above_prior_15_high": breakout,
                "gap_continuation": gap_continuation, "spread_bps": spread}
    out = {
        "schema": "APEX_MARKET_INTELLIGENCE_V1", "symbol": symbol, "decision_epoch": now,
        "regime": "|".join((trend, volatility, participation)), "trend": trend, "volatility": volatility,
        "participation": participation, "liquidity": liquidity, "features": features,
        "eligible_setups": eligible, "premarket": premarket, "historical_baselines": baselines,
        "support": {"current_completed_bars": len(current), "contiguous_trend_returns": len(r30),
                    "prior_matching_clock_sessions": len(baselines), "historical_ratios_supported": supported,
                    "latest_bar_fresh": fresh, "quote_age_seconds": quote_age,
                    "classification_uncertainty": "DESCRIPTIVE_THRESHOLDS_NO_CALIBRATED_STATE_PROBABILITIES"},
        "input_ids": sorted({b["observation_id"] for b in used}),
        "max_input_available_epoch": max((b["available_epoch"] for b in used), default=None),
        "bar_conflicts": conflicts, "quote_conflicts": quote_conflicts, "policy": dict(POLICY),
        "authority": "RESEARCH_SETUP_ELIGIBILITY_ONLY_NO_CAPITAL_AUTHORITY",
        "assumptions": ["Input rows are normalized APEX_DATA_V1 observations; provenance is not authenticated.",
                        "US Eastern weekday 09:30-16:00 regular clock; no holiday/early-close calendar.",
                        "Prior close is the latest observed prior session's completed 15:59 bar, not an official auction print.",
                        "VWAP is a close-volume proxy, not trade-level VWAP.",
                        "Fixed descriptive thresholds and eligible setups are unvalidated research hypotheses.",
                        "Quote absence describes unobserved liquidity, never a zero spread or illiquid market.",
                        "No news, calendar, options, or cross-asset input is inferred from bars."],
    }
    out["regime_id"] = digest(out)
    return out
