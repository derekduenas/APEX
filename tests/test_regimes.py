"""Independent time/value/eligibility checks for causal regime measurements."""
import math
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from apex.core import Refused, digest
from apex.data import normalize
from apex.regimes import classify_regime


ET = ZoneInfo("America/New_York")


def _time(date, hour=9, minute=30):
    return datetime.fromisoformat(date).replace(hour=hour, minute=minute, tzinfo=ET).timestamp()


def _bars(date, count=61, step=.01, volume=100, start=100, hour=9, minute=30):
    epoch = _time(date, hour, minute)
    return [{"kind": "bar", "symbol": "SPY", "event_epoch": epoch + 60 * i,
             "available_epoch": epoch + 60 * (i + 1), "availability_basis": "SYNTHETIC_CLOCK",
             "open": start + step * i, "close": start + step * i,
             "high": start + step * i + .001, "low": start + step * i - .001,
             "volume": volume} for i in range(count)]


def _normalize(raw):
    rows, refused = normalize({"schema": "APEX_DATA_V1", "observations": raw})
    assert refused == []
    return rows


def _history(current=None):
    rows = sum((_bars(day, count=390) for day in ("2026-09-08", "2026-09-09", "2026-09-10")), [])
    return rows + (current if current is not None else _bars("2026-09-11", count=31))


NOW = _time("2026-09-11", 10, 1) + 5


def test_trend_return_realized_volatility_and_ratio_independent_arithmetic():
    raw = _history()
    result = classify_regime(_normalize(raw), now=NOW, symbol="SPY")
    expected_r = [math.log((100 + .01 * i) / (100 + .01 * (i - 1))) for i in range(16, 31)]
    assert result["features"]["ret_15"] == pytest.approx(math.log(100.3 / 100.15))
    assert result["features"]["realized_vol_15"] == pytest.approx(math.sqrt(sum(r * r for r in expected_r)))
    assert result["features"]["trend_efficiency_30"] == pytest.approx(1)
    assert result["features"]["volatility_ratio"] == pytest.approx(1)
    assert result["features"]["relative_volume_15"] == pytest.approx(1)
    assert result["trend"] == "UP_TREND"
    assert result["support"]["prior_matching_clock_sessions"] == 3
    assert result["eligible_setups"]["MOMENTUM_BREAKOUT_LONG"] is True
    assert result["regime_id"] == digest({k: v for k, v in result.items() if k != "regime_id"})


def test_trend_perturbation_changes_real_setup_eligibility():
    up = classify_regime(_normalize(_history()), now=NOW, symbol="SPY")
    down = classify_regime(_normalize(_history(_bars("2026-09-11", count=31, step=-.01))), now=NOW, symbol="SPY")
    assert up["eligible_setups"]["MOMENTUM_BREAKOUT_LONG"]
    assert not down["eligible_setups"]["MOMENTUM_BREAKOUT_LONG"]
    assert down["trend"] == "DOWN_TREND"
    assert down["regime_id"] != up["regime_id"]


def test_pullback_setup_reads_short_reversal_inside_longer_uptrend():
    current = _bars("2026-09-11", count=31, step=.1)
    prices = [100 + .1 * i for i in range(26)] + [102.45, 102.4, 102.35, 102.3, 102.35]
    for row, price in zip(current, prices):
        row.update(open=price, close=price, high=price + .001, low=price - .001)
    result = classify_regime(_normalize(_history(current)), now=NOW, symbol="SPY")
    assert result["trend"] == "UP_TREND"
    assert result["features"]["ret_5"] < 0 < result["features"]["last_return"]
    assert result["features"]["distance_to_vwap"] > 0
    assert result["eligible_setups"]["PULLBACK_LONG"]
    assert not result["eligible_setups"]["MOMENTUM_BREAKOUT_LONG"]


def test_reversion_setup_requires_range_discount_and_actual_turn():
    current = _bars("2026-09-11", count=31)
    prices = [100 if i % 2 == 0 else 102 for i in range(15)] + [100 - .01 * i for i in range(15)] + [99.87]
    for row, price in zip(current, prices):
        row.update(open=price, close=price, high=price + .001, low=price - .001)
    result = classify_regime(_normalize(_history(current)), now=NOW, symbol="SPY")
    assert result["trend"] == "RANGE"
    assert result["features"]["vwap_distance_vol_units"] < -1
    assert result["eligible_setups"]["VWAP_REVERSION_LONG"]
    current[-1].update(open=99.85, close=99.85, high=99.851, low=99.849)
    continuing_down = classify_regime(_normalize(_history(current)), now=NOW, symbol="SPY")
    assert continuing_down["features"]["vwap_distance_vol_units"] < -1
    assert not continuing_down["eligible_setups"]["VWAP_REVERSION_LONG"]


def test_future_rows_and_future_conflicting_revisions_do_not_change_output():
    raw = _history()
    before = classify_regime(_normalize(raw), now=NOW, symbol="SPY")
    corrected = dict(raw[-1], available_epoch=NOW + 1000, close=111, high=111)
    after = classify_regime(_normalize(raw + [corrected] + _bars("2026-09-14")), now=NOW, symbol="SPY")
    assert after == before


def test_matching_clock_baseline_never_uses_later_same_session_bars():
    raw = _history(_bars("2026-09-11", count=31, volume=500))
    before = classify_regime(_normalize(raw), now=NOW, symbol="SPY")
    for row in raw:
        if row["event_epoch"] < _time("2026-09-11") and datetime.fromtimestamp(row["event_epoch"], ET).hour >= 11:
            row["volume"] = 10**9
    after = classify_regime(_normalize(raw), now=NOW, symbol="SPY")
    assert before["features"]["relative_volume_15"] == 5
    assert after["features"]["relative_volume_15"] == 5
    assert after["historical_baselines"] == before["historical_baselines"]
    assert after["participation"] == "HIGH_PARTICIPATION"


def test_delayed_prior_window_not_admitted_until_available():
    raw = _history()
    for row in raw:
        if _time("2026-09-08", 9, 45) <= row["event_epoch"] <= _time("2026-09-08", 10, 0):
            row["available_epoch"] = NOW + 20
    early = classify_regime(_normalize(raw), now=NOW, symbol="SPY")
    late = classify_regime(_normalize(raw), now=NOW + 30, symbol="SPY")
    assert early["support"]["prior_matching_clock_sessions"] == 2
    assert early["features"]["relative_volume_15"] is None
    assert late["support"]["prior_matching_clock_sessions"] == 3
    assert late["features"]["relative_volume_15"] == 1


def test_premarket_gap_uses_known_open_and_prior_completed_close():
    pre = _bars("2026-09-11", count=2, hour=8, minute=0, start=106, volume=50)
    rows = _normalize(_history(_bars("2026-09-11", count=31, start=105)) + pre)
    result = classify_regime(rows, now=NOW, symbol="SPY")
    context = result["premarket"]
    assert context["previous_regular_close"] == pytest.approx(103.89)
    assert context["gap_fraction"] == pytest.approx(105 / 103.89 - 1)
    assert context["gap_reference"] == "REGULAR_OPEN"
    assert context["premarket_volume"] == 100
    assert context["premarket_bars"] == 2
    assert context["premarket_high"] == pytest.approx(106.011)
    assert context["content_age_seconds"] == NOW - _time("2026-09-11", 8, 2)
    assert context["news_catalyst_status"] == "NOT_SUPPLIED"


def test_missing_premarket_and_missing_prior_close_remain_missing_not_zero():
    result = classify_regime(_normalize(_bars("2026-09-11", count=31)), now=NOW, symbol="SPY")
    context = result["premarket"]
    assert context["premarket_volume"] is None
    assert context["premarket_bars"] == 0
    assert context["gap_fraction"] is None
    assert context["previous_regular_close"] is None
    assert result["features"]["relative_volume_15"] is None
    assert result["liquidity"] == "QUOTE_UNOBSERVED"


def test_incomplete_latest_observed_prior_session_is_not_replaced_by_older_close():
    raw = _bars("2026-09-09", count=390) + _bars("2026-09-10", count=80) + _bars("2026-09-11", count=31)
    result = classify_regime(_normalize(raw), now=NOW, symbol="SPY")
    assert result["premarket"]["previous_observed_session"] == "2026-09-10"
    assert result["premarket"]["previous_regular_close"] is None
    assert result["premarket"]["gap_fraction"] is None


def test_zero_volume_is_observed_but_zero_baseline_does_not_become_infinite_ratio():
    raw = _history(_bars("2026-09-11", count=31, volume=0))
    result = classify_regime(_normalize(raw), now=NOW, symbol="SPY")
    assert result["features"]["relative_volume_15"] == 0
    assert result["features"]["close_weighted_vwap_proxy"] is None
    for row in raw:
        row["volume"] = 0
    result = classify_regime(_normalize(raw), now=NOW, symbol="SPY")
    assert result["features"]["relative_volume_15"] is None


def test_missing_bar_does_not_bridge_return_or_manufacture_trend_support():
    raw = _history(_bars("2026-09-11", count=31))
    raw = [r for r in raw if r["event_epoch"] != _time("2026-09-11", 9, 40)]
    result = classify_regime(_normalize(raw), now=NOW, symbol="SPY")
    assert result["support"]["contiguous_trend_returns"] == 0
    assert result["trend"] == "TREND_UNOBSERVED"
    assert result["eligible_setups"] == {"WAIT": True, "HOLD_LONG": False,
                                         "MOMENTUM_BREAKOUT_LONG": False, "PULLBACK_LONG": False,
                                         "VWAP_REVERSION_LONG": False}


def test_fresh_receipt_does_not_make_stale_quote_fresh():
    raw = _history()
    quote = {"kind": "quote", "symbol": "SPY", "event_epoch": NOW - 40,
             "available_epoch": NOW, "availability_basis": "SYNTHETIC_CLOCK",
             "bid": 100, "ask": 100.02, "bid_size": 2, "ask_size": 3}
    stale = classify_regime(_normalize(raw + [quote]), now=NOW, symbol="SPY")
    assert stale["liquidity"] == "QUOTE_STALE_OR_FUTURE"
    assert stale["features"]["spread_bps"] is None
    quote["event_epoch"] = NOW
    fresh = classify_regime(_normalize(raw + [quote]), now=NOW, symbol="SPY")
    assert fresh["liquidity"] == "TIGHT_SPREAD"
    assert fresh["features"]["spread_bps"] == pytest.approx(10000 * .02 / 100.01)


def test_stale_underlying_disables_setup_hypotheses_and_preserves_context():
    result = classify_regime(_normalize(_history()), now=NOW + 121, symbol="SPY")
    assert not result["support"]["latest_bar_fresh"]
    assert all(not value for key, value in result["eligible_setups"].items() if key != "WAIT")
    assert result["premarket"]["regular_open"] == 100


def test_preopen_produces_context_with_no_intraday_setups():
    raw = _bars("2026-09-10", count=390) + _bars("2026-09-11", count=10, hour=8, minute=0, start=104)
    result = classify_regime(_normalize(raw), now=_time("2026-09-11", 8, 15), symbol="SPY")
    assert result["premarket"]["gap_reference"] == "LATEST_AVAILABLE_PREMARKET_CLOSE"
    assert result["premarket"]["regular_open"] is None
    assert result["premarket"]["gap_fraction"] == pytest.approx(104.09 / 103.89 - 1)
    assert result["support"]["current_completed_bars"] == 0
    assert not result["eligible_setups"]["HOLD_LONG"]
    assert result["max_input_available_epoch"] <= result["decision_epoch"]


def test_identical_input_permutation_is_deterministic():
    rows = _normalize(_history())
    assert classify_regime(rows, now=NOW, symbol="SPY") == classify_regime(rows[::-1], now=NOW, symbol="SPY")


@pytest.mark.parametrize("now", [float("nan"), float("inf"), True])
def test_invalid_clock_refused(now):
    with pytest.raises(Refused, match="INVALID_REGIME_REQUEST"):
        classify_regime([], now=now, symbol="SPY")
