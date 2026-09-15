"""Independent checks for forecast clocks, price anchors and simulation moments."""
from decimal import Decimal

import numpy as np
import pytest
from scipy import integrate, stats

from apex.core import Config, Refused
from apex.data import normalize, twin
from apex.decision import evaluate, quote_at
from apex.fixtures import demo_document
from apex.forecast import predict


def stale_inputs():
    document, start, _ = demo_document(drift=0)
    document["observations"] = [r for r in document["observations"] if r["available_epoch"] <= start]
    rows, _ = normalize(document)
    state, _ = twin(rows, start, "SPY")
    old_spot = state["fields"]["spot"]
    now = start + 120
    document["observations"].append({"kind": "quote", "symbol": "SPY", "event_epoch": now,
        "available_epoch": now, "availability_basis": "SYNTHETIC_CLOCK", "bid": old_spot * .99 - .01,
        "ask": old_spot * .99 + .01, "bid_size": 100, "ask_size": 100})
    rows, _ = normalize(document)
    return rows, start, now


def test_stale_bar_does_not_invent_quote_rebound():
    rows, _, now = stale_inputs()
    state, returns = twin(rows, now, "SPY")
    config = Config(variance="ewma")
    prediction, paths = predict(returns, state, config)
    quote, problem = quote_at(rows, now, config)
    candidate = evaluate(prediction, paths, quote, problem, cash=Decimal("10000"),
                         position_open=False, config=config)
    # Unchanged return scenarios must start at the executable current market.
    assert candidate["decision"] == "WAIT"
    assert candidate["expected_net"] < 0
    assert candidate["model_probability_net_positive"] < .55
    assert candidate["price_anchor"]["spot"] == (quote["bid"] + quote["ask"]) / 2
    assert candidate["price_anchor"]["quote_id"] == quote["observation_id"]


def test_same_return_paths_and_quote_are_invariant_to_old_bar_price():
    quote = {"bid": 89.99, "ask": 90.01, "ask_size": 100, "bid_size": 100,
             "observation_id": "controlled-current-quote", "event_epoch": 120.0}
    prediction = {"spot": 100.0, "created_epoch": 120.0, "horizon_minutes": 15}
    # A zero-return control has no prospective edge after spread and fees.
    paths = np.zeros((1000, 16))
    arguments = {"cash": Decimal("10000"), "position_open": False, "config": Config(variance="ewma")}
    stale = evaluate(prediction, paths, quote, None, **arguments)
    matching = evaluate({**prediction, "spot": 90.0}, paths, quote, None, **arguments)
    assert stale == matching
    assert stale["decision"] == "WAIT" and stale["expected_net"] < 0


def test_forecast_horizon_starts_at_its_bar_price_origin():
    rows, origin, now = stale_inputs()
    state, returns = twin(rows, now, "SPY")
    prediction, paths = predict(returns, state, Config(variance="ewma"))
    assert prediction["price_origin_epoch"] == origin
    assert prediction["created_epoch"] == now
    assert prediction["target_epoch"] == origin + 15 * 60
    assert prediction["target_epoch"] - prediction["price_origin_epoch"] == 15 * 60
    assert paths.shape == (1000, 16)
    with pytest.raises(Refused, match="FORECAST_TARGET_NOT_FUTURE"):
        predict(returns, state, Config(variance="ewma", horizon_minutes=1))


def test_fractional_receipt_forecast_target_is_a_completed_bar_grid_point():
    document, start, _ = demo_document()
    rows, _ = normalize(document)
    state, returns = twin(rows, start + .03, "SPY")
    prediction, _ = predict(returns, state, Config(variance="ewma"))
    assert prediction["created_epoch"] == start + .03
    assert prediction["target_epoch"] == start + 900
    assert prediction["target_epoch"] % 60 == 0


@pytest.mark.parametrize("nu", [None, 2.2, 8.0])
def test_bounded_innovation_variance_uses_independent_integral(nu):
    from apex.forecast import _innovation_law, _draw_innovations
    law = _innovation_law(nu)
    cap = law["raw_standardized_cap"]
    distribution = stats.norm() if nu is None else stats.t(df=nu, scale=np.sqrt((nu - 2) / nu))
    inside = integrate.quad(lambda x: x * x * distribution.pdf(x), -cap, cap, epsabs=1e-11)[0]
    second = inside / (1 - 2 * distribution.sf(cap))
    assert np.isclose(law["second_moment_before_normalization"], second, rtol=1e-10, atol=1e-12)
    shocks = _draw_innovations(np.random.default_rng(83), law, 400000)
    assert np.max(np.abs(shocks)) <= cap / np.sqrt(second) * (1 + 1e-12)
    assert abs(shocks.var() - 1) < .04


def test_forecast_records_distinct_fit_and_bounded_simulation_laws():
    document, start, _ = demo_document()
    rows, _ = normalize(document)
    state, returns = twin(rows, start, "SPY")
    prediction, _ = predict(returns, state, Config())
    assert prediction["variance"]["model_id"] == "GARCH11_T"
    assert prediction["simulation"]["innovation_law"] == "TRUNCATED_VARIANCE_NORMALIZED_V1"
    assert prediction["simulation"]["base_family"] == "STANDARDIZED_STUDENT_T"
    assert prediction["simulation"]["raw_standardized_cap"] == 8
    assert prediction["simulation"]["price_mean_exists"] is True


def test_symmetric_zero_drift_baseline_has_exact_half_probability():
    document, start, _ = demo_document()
    rows, _ = normalize(document)
    state, returns = twin(rows, start, "SPY")
    prediction, paths = predict(returns, state, Config(variance="ewma"))
    zero_drift_terminal = paths[:, -1] - prediction["direction"]["per_minute_log_drift"] * 15
    assert prediction["baseline_p_up"] == .5
    assert prediction["simulated_baseline_p_up"] == float(np.mean(zero_drift_terminal > 0))


def test_innovation_rejection_is_bounded_and_refuses_without_tail_substitution():
    from apex.forecast import _innovation_law, _draw_innovations
    class OutsideGenerator:
        calls = 0
        def standard_normal(self, count):
            self.calls += 1
            return np.full(count, 9.0)
    law = _innovation_law(None)
    generator = OutsideGenerator()
    with pytest.raises(Refused, match="INNOVATION_REJECTION_BUDGET_EXHAUSTED"):
        _draw_innovations(generator, law, 100)
    assert generator.calls == 1 + law["resample_budget_per_step"]
