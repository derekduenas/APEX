import math

import numpy as np
import pytest

from apex.core import Config, Refused
from apex.data import normalize, twin
from apex.fixtures import demo_document
from apex.forecast import predict
from apex.reused.contracts import ModelRefused
from apex.reused.vol_models import GARCH, std_t_rvs, t_scale_from_variance, variance_from_t_scale


def inputs():
    doc, start, _ = demo_document()
    rows, _ = normalize(doc)
    return twin(rows, start, "SPY")


def test_real_garch_runs_and_paths_are_not_just_a_status_label():
    state, returns = inputs()
    forecast, paths = predict(returns, state, Config())
    assert forecast["variance"]["model_id"] == "GARCH11_T"
    p = forecast["variance"]["params"]
    assert p["convergence"]["success"] and p["persistence"] < .999
    assert paths.shape == (1000, 16) and np.all(paths[:, 0] == 0)
    assert np.unique(paths[:, -1]).size == 1000
    assert forecast["p_up"] == float((paths[:, -1] > 0).mean())
    assert len(forecast["training_rows"]) == 393


def test_garch_refusal_names_fallback_but_firewall_never_falls_back(monkeypatch):
    state, returns = inputs()
    def refused(*args, **kwargs):
        raise ModelRefused("NOT_CONVERGED: controlled failure")
    monkeypatch.setattr(GARCH, "fit", refused)
    f, _ = predict(returns, state, Config())
    assert f["fit_attempts"][0]["status"] == "REFUSED"
    assert f["fit_attempts"][1]["status"] == "FITTED_FALLBACK"
    assert f["variance"]["model_id"] == "EWMA"
    def firewall(*args, **kwargs):
        raise ModelRefused("FIREWALL: controlled refusal")
    monkeypatch.setattr(GARCH, "fit", firewall)
    with pytest.raises(Refused, match="FIREWALL"):
        predict(returns, state, Config())


def test_student_t_scale_and_one_step_simulation_variance():
    rng = np.random.default_rng(123)
    z = std_t_rvs(rng, 8, 200000)
    assert abs(float(z.var()) - 1) < .025
    assert math.isclose(variance_from_t_scale(t_scale_from_variance(.012, 8), 8), .012)
    state, returns = inputs()
    f, paths = predict(returns, state, Config(paths=50000, horizon_minutes=1))
    p = f["variance"]["params"]
    expected = p["omega"] + p["alpha"] * p["e_last"] ** 2 + p["beta"] * p["h_last"]
    assert np.isclose(paths[:, 1].var(), expected, rtol=.05)


def test_insufficient_or_constant_history_refuses():
    state, returns = inputs()
    with pytest.raises(Refused, match="INSUFFICIENT_ADJACENT_RETURNS"):
        predict(returns[:199], state, Config())
    with pytest.raises(Refused, match="DEGENERATE_VARIANCE"):
        predict([{**r, "ret_1": 0} for r in returns], state, Config())
