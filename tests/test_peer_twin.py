import copy
from datetime import datetime
import math
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from apex.core import Refused, digest
from apex.data import normalize, twin
from apex.peer_twin import HYPOTHESIS, peer_state


SYMBOLS = ("AAPL", "SPY", "XLK")


def document(*, training=120, control="lag", shock_scale=1.0, collinear=False):
    """A declared algebraic control, not generated market evidence."""
    assert training % 4 == 0
    start = datetime(2026, 8, 3, 9, 30, tzinfo=ZoneInfo("America/New_York")).timestamp()
    market = np.tile([.0004, -.0004, .0004, -.0004], training // 4)
    sector = market.copy() if collinear else np.tile([.0003, .0003, -.0003, -.0003], training // 4)
    errors = np.tile([.00008, -.00008, -.00008, .00008], training // 4)
    training_target = .00002 + 1.2 * market + .8 * sector + errors
    market_shock = np.full(5, .001 * shock_scale)
    sector_shock = np.full(5, .0007 * shock_scale)
    residual_shock = {
        "lag": [-.0012, -.0012, -.0012, -.0012, .0004],
        "already_repriced": [.0002] * 5,
        "still_falling": [-.0012] * 5,
        "flat_residual": [0.] * 5,
    }[control]
    shock_target = .00002 + 1.2 * market_shock + .8 * sector_shock + residual_shock
    series = {"AAPL": np.r_[training_target, shock_target],
              "SPY": np.r_[market, market_shock], "XLK": np.r_[sector, sector_shock]}
    observations = []
    for symbol, initial in zip(SYMBOLS, (100., 400., 200.)):
        prices = initial * np.exp(np.r_[0., np.cumsum(series[symbol])])
        for index, close in enumerate(prices):
            opening = prices[max(0, index - 1)]
            observations.append({"kind": "bar", "symbol": symbol,
                                 "event_epoch": start + index * 60, "available_epoch": start + (index + 1) * 60,
                                 "availability_basis": "SYNTHETIC_CLOCK", "open": float(opening),
                                 "high": float(max(opening, close)), "low": float(min(opening, close)),
                                 "close": float(close), "volume": 1000.})
    return {"schema": "APEX_DATA_V1", "source": "SYNTHETIC_ALGEBRAIC_PEER_CONTROL", "observations": observations}, start + (training + 6) * 60


def state_for(doc, now, **kwargs):
    rows, rejects = normalize(doc)
    assert not rejects
    snapshot, _ = twin(rows, now, "AAPL")
    return peer_state(rows, snapshot, market_symbol="SPY", sector_symbol="XLK", **kwargs)


def test_hand_computed_two_factor_features_and_frozen_training_boundary():
    doc, now = document()
    state = state_for(doc, now)
    shrink = 1 + HYPOTHESIS["ridge_lambda"]
    fit, features = state["fit"], state["features"]
    assert fit["beta_market"] == pytest.approx(1.2 / shrink, abs=1e-11)
    assert fit["beta_sector"] == pytest.approx(.8 / shrink, abs=1e-11)
    assert fit["intercept_per_minute"] == pytest.approx(.00002, abs=1e-14)
    expected_prediction = .0001 + (1.2 * .005 + .8 * .0035) / shrink
    expected_observed = .0001 + 1.2 * .005 + .8 * .0035 - .0044
    expected_variance = (120 / 117) * (.00008 ** 2 + (.0004 * 1.2 * (1 - 1 / shrink)) ** 2
                                           + (.0003 * .8 * (1 - 1 / shrink)) ** 2)
    assert features["target_ret_5"] == pytest.approx(expected_observed, abs=1e-13)
    assert features["market_ret_5"] == pytest.approx(.005, abs=1e-13)
    assert features["sector_ret_5"] == pytest.approx(.0035, abs=1e-13)
    assert features["predicted_peer_ret_5"] == pytest.approx(expected_prediction, abs=1e-13)
    assert features["lag_gap"] == pytest.approx(expected_prediction - expected_observed, abs=1e-13)
    assert fit["training_residual_variance"] == pytest.approx(expected_variance, rel=1e-9)
    assert features["residual_z"] == pytest.approx((expected_observed - expected_prediction) / math.sqrt(5 * expected_variance))
    assert features["recovery_last_minute"] == pytest.approx(.0004 + .00176 * (1 - 1 / shrink), abs=1e-13)
    assert features["peer_agreement"] == 1.0 and state["eligible_setup"] is True
    assert fit["training_cutoff_complete_epoch"] == now - 5 * 60
    assert fit["training_max_available_epoch"] == now - 5 * 60
    assert state["source"]["max_available_epoch"] == now
    assert state["source"]["availability_basis_counts"] == {"SYNTHETIC_CLOCK": 378}
    assert all(len(ids) == 126 for ids in state["consumed_bar_ids"].values())
    assert all(len(ids) == 121 for ids in fit["training_bar_ids"].values())
    assert state["peer_state_id"] == digest({k: v for k, v in state.items() if k != "peer_state_id"})


def test_future_bars_and_later_corrections_cannot_change_state_or_digest():
    doc, now = document()
    before = state_for(doc, now)
    augmented = copy.deepcopy(doc)
    for symbol in SYMBOLS:
        old = next(r for r in doc["observations"] if r["symbol"] == symbol and r["event_epoch"] == now - 60)
        correction = {**old, "available_epoch": now + 10,
                      **{k: old[k] * 2 for k in ("open", "high", "low", "close")}}
        future = {**correction, "event_epoch": now, "available_epoch": now + 60}
        augmented["observations"].extend([correction, future])
    assert state_for(augmented, now) == before
    with pytest.raises(Refused):
        state_for(augmented, now + 10)


def test_shock_values_change_features_but_cannot_enter_beta_training():
    doc, now = document()
    changed, _ = document(shock_scale=2)
    a, b = state_for(doc, now), state_for(changed, now)
    assert a["fit"] == b["fit"]
    assert a["features"]["market_ret_5"] != b["features"]["market_ret_5"]
    assert a["features"]["predicted_peer_ret_5"] != b["features"]["predicted_peer_ret_5"]


def test_shifted_factor_pairing_changes_fitted_information():
    doc, now = document()
    changed = copy.deepcopy(doc)
    peers = [r for r in changed["observations"] if r["symbol"] == "XLK"]
    original = copy.deepcopy(peers)
    # Circularly shift only the training peer returns by one interval while
    # preserving clock alignment. These are different observations, not labels.
    returns = np.diff(np.log([r["close"] for r in original[:121]]))
    prices = original[0]["close"] * np.exp(np.r_[0., np.cumsum(np.roll(returns, 1))])
    for row, price in zip(peers[:121], prices):
        for key in ("open", "high", "low", "close"):
            row[key] = float(price)
    a, b = state_for(doc, now), state_for(changed, now)
    assert a["fit"]["training_return_digest"] != b["fit"]["training_return_digest"]
    assert a["fit"]["beta_sector"] != pytest.approx(b["fit"]["beta_sector"])


@pytest.mark.parametrize("offset,reason", [(60, "ORIGIN_MISMATCH_OR_STALE"), (360, "MISSING_OR_NONCONTIGUOUS"), (7200, "MISSING_OR_NONCONTIGUOUS")])
def test_missing_peer_intervals_are_refused(offset, reason):
    doc, now = document()
    doc["observations"] = [r for r in doc["observations"] if not (r["symbol"] == "XLK" and r["event_epoch"] == now - offset)]
    with pytest.raises(Refused, match=reason):
        state_for(doc, now)


def test_delayed_required_peer_bar_is_missing_until_available():
    doc, now = document()
    row = next(r for r in doc["observations"] if r["symbol"] == "XLK" and r["event_epoch"] == now - 360)
    row["available_epoch"] = now + 15
    with pytest.raises(Refused, match="MISSING_OR_NONCONTIGUOUS"):
        state_for(doc, now)
    assert state_for(doc, now + 15)["source"]["max_available_epoch"] == now + 15


def test_conflicting_and_corrupted_peer_bars_are_refused():
    doc, now = document()
    peer = next(r for r in doc["observations"] if r["symbol"] == "XLK" and r["event_epoch"] == now - 60)
    doc["observations"].append({**peer, **{k: peer[k] * 1.01 for k in ("open", "high", "low", "close")}})
    with pytest.raises(Refused, match="PEER_CONFLICTING_BAR:XLK"):
        state_for(doc, now)
    doc, now = document()
    rows, _ = normalize(doc)
    snapshot, _ = twin(rows, now, "AAPL")
    peer = next(r for r in rows if r["symbol"] == "XLK")
    peer["close"] = float("nan")
    with pytest.raises(Refused, match="PEER_CORRUPT_OR_UNNORMALIZED_BAR:XLK"):
        peer_state(rows, snapshot, market_symbol="SPY", sector_symbol="XLK")


def test_snapshot_is_bound_to_exact_actual_twin_and_fresh_origin():
    doc, now = document()
    rows, _ = normalize(doc)
    snapshot, _ = twin(rows, now, "AAPL")
    snapshot["fields"]["spot"] *= 2
    with pytest.raises(Refused, match="PEER_SNAPSHOT_ORIGIN_OR_INPUT_MISMATCH"):
        peer_state(rows, snapshot, market_symbol="SPY", sector_symbol="XLK")
    snapshot, _ = twin(rows, now, "AAPL")
    snapshot["now"] = now + 121
    with pytest.raises(Refused, match="PEER_ORIGIN_INCOMPLETE_OR_STALE"):
        peer_state(rows, snapshot, market_symbol="SPY", sector_symbol="XLK")


@pytest.mark.parametrize("control,scale,eligible", [("lag", 1, True), ("already_repriced", 1, False),
                                                    ("still_falling", 1, False), ("flat_residual", 1, False),
                                                    ("lag", -1, False)])
def test_positive_and_anti_edge_current_state_controls(control, scale, eligible):
    doc, now = document(control=control, shock_scale=scale)
    state = state_for(doc, now)
    assert state["eligible_setup"] is eligible
    assert state["status"] == "UNTESTED_HYPOTHESIS_NOT_CALIBRATED"


def test_collinear_factors_have_stable_regularized_fit_and_finite_features():
    doc, now = document(collinear=True)
    state = state_for(doc, now)
    assert all(math.isfinite(x) for x in state["features"].values())
    assert state["fit"]["regularized_condition_number"] < 2_000_010


@pytest.mark.parametrize("training,shock,reason", [(19, 5, "INVALID_TRAINING"), (True, 5, "INVALID_TRAINING"),
                                                  (10 ** 1000, 5, "INVALID_TRAINING"),
                                                  (120, 4, "FIVE_MINUTE_SHOCK"), (120, True, "FIVE_MINUTE_SHOCK")])
def test_invalid_or_unfrozen_windows_refused(training, shock, reason):
    doc, now = document()
    with pytest.raises(Refused, match=reason):
        state_for(doc, now, training_minutes=training, shock_minutes=shock)


def test_declared_short_training_keeps_full_aligned_window_and_minimum_gate():
    doc, now = document(training=20)
    state = state_for(doc, now, training_minutes=20)
    assert state["fit"]["training_minutes"] == 20
    assert len(state["consumed_bar_ids"]["XLK"]) == 26
    with pytest.raises(Refused, match="INSUFFICIENT_SAME_SESSION_HISTORY"):
        state_for(doc, now)


def test_degenerate_factor_variance_refuses_instead_of_imputing_information():
    doc, now = document()
    for row in doc["observations"]:
        if row["symbol"] == "XLK" and row["event_epoch"] < now - 300:
            for key in ("open", "high", "low", "close"):
                row[key] = 200.
    with pytest.raises(Refused, match="PEER_DEGENERATE_FACTOR_TRAINING_VARIANCE"):
        state_for(doc, now)


def test_duplicate_peer_identity_is_not_two_independent_factors():
    doc, now = document()
    rows, _ = normalize(doc)
    snapshot, _ = twin(rows, now, "AAPL")
    with pytest.raises(Refused, match="THREE_DISTINCT_SYMBOLS"):
        peer_state(rows, snapshot, market_symbol="SPY", sector_symbol="SPY")
