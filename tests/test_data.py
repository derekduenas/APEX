import copy
import math

import pytest

from apex.core import Config, Refused
from apex.data import normalize, twin, visible
from apex.fixtures import demo_document
from apex.forecast import predict


def test_real_feature_values_and_no_overnight_return():
    doc, start, _ = demo_document()
    rows, rejects = normalize(doc)
    state, returns = twin(rows, start, "SPY")
    today = [r for r in rows if r["kind"] == "bar" and start - 300 <= r["event_epoch"] < start]
    assert not rejects and len(returns) == 389 + 4
    assert state["fields"]["spot"] == today[-1]["close"]
    assert state["fields"]["ret_1"] == math.log(today[-1]["close"] / today[-2]["close"])
    assert state["fields"]["ret_5"] is None
    assert state["fields"]["volume"] == 5000
    assert all(r["available"] <= start for r in returns)


def test_malformed_row_is_named_and_does_not_abort_valid_rows():
    doc, _, _ = demo_document()
    bad = copy.deepcopy(doc["observations"][0])
    del bad["event_epoch"]
    rows, rejected = normalize({**doc, "observations": [doc["observations"][0], bad]})
    assert len(rows) == 1
    assert rejected == [{"input_index": 1, "reason": "MISSING_FIELDS:event_epoch"}]


@pytest.mark.parametrize("field,value,reason", [("close", float("nan"), "INVALID_OHLCV"), ("volume", -1, "INCONSISTENT_OHLCV"), ("available_epoch", 0, "AVAILABLE_BEFORE_EVENT")])
def test_invalid_data_refuses(field, value, reason):
    doc, _, _ = demo_document()
    row = {**doc["observations"][0], field: value}
    rows, rejected = normalize({**doc, "observations": [row]})
    assert not rows and rejected[0]["reason"] == reason


def test_future_conflict_cannot_change_past_and_matching_provenance_does_not_delete_price():
    doc, start, _ = demo_document()
    original = next(r for r in doc["observations"] if r["kind"] == "bar" and r["event_epoch"] == start - 60)
    same = {**original, "availability_basis": "MEASURED_RECEIPT", "available_epoch": start + 1}
    changed = {**original, "close": original["close"] + 1, "high": original["high"] + 1, "available_epoch": start + 10}
    rows, _ = normalize({**doc, "observations": [original, same, changed]})
    before, conflicts = visible(rows, now=start, symbol="SPY", kind="bar")
    assert len(before) == 1 and not conflicts
    agreeing, conflicts = visible(rows, now=start + 1, symbol="SPY", kind="bar")
    assert agreeing == before and not conflicts
    after, conflicts = visible(rows, now=start + 10, symbol="SPY", kind="bar")
    assert not after and len(conflicts) == 1


def test_future_values_do_not_change_forecast_or_paths():
    doc, start, _ = demo_document()
    rows, _ = normalize(doc)
    state, returns = twin(rows, start, "SPY")
    forecast, paths = predict(returns, state, Config(variance="ewma"))
    future = copy.deepcopy(doc)
    for row in future["observations"]:
        if row["kind"] == "bar" and row["available_epoch"] > start:
            for key in ("open", "high", "low", "close"):
                row[key] *= 2
    changed, _ = normalize(future)
    state2, returns2 = twin(changed, start, "SPY")
    forecast2, paths2 = predict(returns2, state2, Config(variance="ewma"))
    assert forecast2 == forecast and (paths2 == paths).all()
    returns[-1]["available"] = start + 1
    with pytest.raises(Refused, match="FIREWALL"):
        predict(returns, state, Config(variance="ewma"))


def test_consumed_history_perturbation_changes_model_output():
    doc, start, _ = demo_document()
    rows, _ = normalize(doc)
    state, returns = twin(rows, start, "SPY")
    f, _ = predict(returns, state, Config(variance="ewma"))
    changed = copy.deepcopy(returns)
    for r in changed:
        r["ret_1"] -= .001
    g, _ = predict(changed, state, Config(variance="ewma"))
    assert f["p_up"] > .9 and g["p_up"] < .1
    assert f["residual_digest"] != g["residual_digest"]
