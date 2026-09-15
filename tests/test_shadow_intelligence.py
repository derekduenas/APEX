"""The real capture/shadow reader consumes regime flags in a strategy preview."""
import json
from datetime import datetime, timezone

import pytest

from apex import shadow
from apex.capture import replay_capture
from apex.core import Config, Refused, digest
from apex.data import normalize, twin
from apex.fixtures import capture_demo
from apex.forecast import predict
from apex.shadow import observe
from apex.strategy_simulation import SimulationCosts, evaluate_strategies


def _world(*, current_direction=1, quote=True):
    raw = []
    for day in (8, 9, 10, 11):
        start = datetime(2026, 9, day, 13, 30, tzinfo=timezone.utc).timestamp()
        count = 390 if day < 11 else 40
        direction = 1 if day < 11 else current_direction
        for index in range(count):
            event = start + index * 60
            price = 100 + direction * index * .02
            raw.append({"kind": "bar", "symbol": "SPY", "event_epoch": event,
                        "available_epoch": event + 60, "availability_basis": "SYNTHETIC_CLOCK",
                        "open": price, "high": price + .001, "low": price - .001,
                        "close": price, "volume": 1000})
    now = start + count * 60 + 5
    if quote:
        raw.append({"kind": "quote", "symbol": "SPY", "event_epoch": now - 1,
                    "available_epoch": now, "availability_basis": "SYNTHETIC_CLOCK",
                    "bid": price - .01, "ask": price + .01, "bid_size": 100, "ask_size": 100})
    rows, refused = normalize({"schema": "APEX_DATA_V1", "observations": raw})
    assert not refused
    return rows, now


def test_real_shadow_reader_changes_strategy_eligibility_when_market_trend_changes():
    up_rows, now = _world(current_direction=1)
    down_rows, _ = _world(current_direction=-1)
    config = Config(variance="ewma", paths=100)
    up = observe(up_rows, now, config)
    down = observe(down_rows, now, config)
    assert up["status"] == down["status"] == "OBSERVED"
    assert up["intelligence"]["trend"] == "UP_TREND"
    assert down["intelligence"]["trend"] == "DOWN_TREND"
    for result in (up, down):
        preview = result["strategy_preview"]
        assert preview["status"] == "EVALUATED"
        assert preview["regime_id"] == result["intelligence"]["regime_id"]
        assert preview["forecast_id"] == result["forecast"]["forecast_id"]
        assert preview["inputs"]["eligible_setups"] == result["intelligence"]["eligible_setups"]
        assert preview["authority"] == result["candidate"]["authority"] == "NONE_SHADOW_ONLY"
        assert preview["selection_status"] == "NO_STRATEGY_SELECTED_OR_AUTHORIZED"
    strategy = "MOMENTUM_BREAKOUT_LONG"
    assert up["strategy_preview"]["strategies"][strategy]["eligible"] is True
    assert down["strategy_preview"]["strategies"][strategy]["eligible"] is False
    assert up["strategy_preview"]["strategies"][strategy]["summary"]["ineligible_count"] == 0
    assert down["strategy_preview"]["strategies"][strategy]["summary"]["ineligible_count"] == 100


def test_preview_hash_binds_real_evaluator_and_delayed_entry_cannot_reuse_origin():
    rows, now = _world()
    config = Config(variance="ewma", paths=100, commission_per_share="0.13", minimum_commission="0.25")
    result = observe(rows, now, config)
    state, returns = twin(rows, now, config.symbol)
    forecast, paths = predict(returns, state, config)
    args = {"spot": forecast["spot"], "capital": float(config.starting_cash),
            "max_notional": float(config.max_notional), "eligible_setups": result["intelligence"]["eligible_setups"],
            "costs": SimulationCosts(commission_per_share=.13, minimum_commission=.25),
            "vwap": state["fields"]["close_weighted_vwap_proxy"]}
    expected = evaluate_strategies(paths, earliest_entry_step=1, **args)
    retrospective = evaluate_strategies(paths, earliest_entry_step=0, **args)
    preview = result["strategy_preview"]
    assert now == preview["price_origin_epoch"] + 5
    assert preview["inputs"]["earliest_entry_step"] == 1
    assert preview["inputs"]["maximum_remaining_hold_minutes"] == 14
    assert preview["result_hash"] == expected["result_hash"] != retrospective["result_hash"]
    actual_entries = expected["strategies"]["HOLD_LONG"]["paths"]["entry_step"]
    assert any(value == 1 for value in actual_entries)
    assert all(value == -1 or preview["price_origin_epoch"] + value * 60 >= now for value in actual_entries)
    assert preview["preview_hash"] == digest({k: v for k, v in preview.items() if k != "preview_hash"})
    assert all("paths" not in value for value in preview["strategies"].values())
    assert preview["inputs"]["costs"]["commission_per_share"] == .13
    assert preview["scope"] == "SINGLE_MODEL_RESEARCH_PREVIEW_NOT_ROBUST_LAB_SELECTION"
    assert preview["price_basis"] == "LATEST_COMPLETED_BAR_CLOSE_MODEL_ORIGIN_NOT_AN_EXECUTABLE_ENTRY_QUOTE"


def test_actual_capture_demo_persists_intelligence_and_preview_then_replays(tmp_path):
    root = tmp_path / "capture"
    summary = capture_demo(root, variance="ewma")
    assert summary["status"] == "CAPTURED" and summary["orders"] == 0
    records = [json.loads(path.read_text())["result"] for path in sorted(root.glob("shadow-*.json"))]
    assert len(records) == 3
    for record in records:
        assert record["intelligence"]["schema"] == "APEX_MARKET_INTELLIGENCE_V1"
        assert record["strategy_preview"]["status"] == "EVALUATED"
        assert record["strategy_preview"]["forecast_id"] == record["forecast"]["forecast_id"]
        assert record["strategy_preview"]["inputs"]["earliest_entry_step"] == 1
        # This actual capture fixture is 09:35: the 30-return regime window
        # does not yet exist. Its strategies honestly remain ineligible.
        assert not record["strategy_preview"]["strategies"]["HOLD_LONG"]["eligible"]
    assert replay_capture(root)["status"] == "AGREEMENT"
    assert not (root / "ledger.jsonl").exists()


def test_absent_premarket_news_and_quotes_remain_explicit_missing_context():
    rows, now = _world(quote=False)
    result = observe(rows, now, Config(variance="ewma", paths=100))
    premarket = result["intelligence"]["premarket"]
    assert premarket["premarket_bars"] == 0
    assert premarket["premarket_volume"] is None
    assert premarket["news_catalyst_status"] == "NOT_SUPPLIED"
    assert premarket["calendar_status"] == "NOT_SUPPLIED"
    assert result["intelligence"]["liquidity"] == "QUOTE_UNOBSERVED"
    assert result["candidate"]["decision"] == "WAIT"
    assert result["candidate"]["reason"] == "QUOTE_UNAVAILABLE_OR_CONFLICTING"
    assert result["strategy_preview"]["status"] == "EVALUATED"


@pytest.mark.parametrize("component", ["classify_regime", "evaluate_strategies"])
def test_optional_intelligence_refusal_preserves_original_forecast_and_candidate(monkeypatch, component):
    rows, now = _world()
    config = Config(variance="ewma", paths=100)
    baseline = observe(rows, now, config)
    def refuse(*args, **kwargs):
        raise Refused("CONTROLLED_OPTIONAL_LAYER_REFUSAL")
    monkeypatch.setattr(shadow, component, refuse)
    result = observe(rows, now, config)
    assert result["status"] == "OBSERVED"
    assert result["forecast"] == baseline["forecast"]
    assert result["candidate"] == baseline["candidate"]
    if component == "classify_regime":
        assert result["intelligence"]["reason"] == "CONTROLLED_OPTIONAL_LAYER_REFUSAL"
        assert result["strategy_preview"]["reason"] == "INTELLIGENCE_REFUSED"
    else:
        assert result["intelligence"] == baseline["intelligence"]
        assert result["strategy_preview"]["reason"] == "CONTROLLED_OPTIONAL_LAYER_REFUSAL"


def test_unavailable_variance_keeps_already_observed_regime_evidence():
    rows, now = _world()
    today = [row for row in rows if row["event_epoch"] >= now - 41 * 60]
    result = observe(today, now, Config(variance="ewma", paths=100))
    assert result["status"] == "REFUSED"
    assert result["forecast"] is None
    assert result["intelligence"]["trend"] == "UP_TREND"
    assert result["strategy_preview"] is None


def test_future_input_cannot_change_shadow_intelligence_or_preview():
    rows, now = _world()
    config = Config(variance="ewma", paths=100)
    before = observe(rows, now, config)
    altered = {**next(row for row in reversed(rows) if row["kind"] == "bar"), "available_epoch": now + 1,
               "open": 500, "high": 501, "low": 499, "close": 500}
    altered.pop("observation_id")
    extra, rejected = normalize({"schema": "APEX_DATA_V1", "observations": [altered]})
    assert not rejected
    after = observe(rows + extra, now, config)
    assert after == before
