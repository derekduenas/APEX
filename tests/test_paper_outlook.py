import copy

import numpy as np
import pytest

from apex.core import Config, Refused, digest
from apex.paper_outlook import path_outlook


def _case():
    config = Config(paths=100, horizon_minutes=5)
    paths = np.tile(np.linspace(0, .01, 6), (100, 1))
    prediction = {'forecast_id': 'test', 'paths_digest': digest(paths.tolist()),
                  'created_epoch': 1000., 'direction': {'per_minute_log_drift': .002}}
    quote = {'bid': 99.99, 'ask': 100.01, 'available_epoch': 999., 'observation_id': 'q'}
    return prediction, paths, quote, config


def test_world_report_exposes_dependence_and_costs_without_market_odds():
    prediction, paths, quote, config = _case()
    result = path_outlook(prediction, paths, quote, quantity=1, config=config)
    assert result['world_probabilities'] is None
    assert result['mean_net_sign_disagrees_across_worlds'] is True
    base = result['worlds']['FITTED_DIRECTION']
    assert base['terminal_price_quantiles']['0.5'] == pytest.approx(100 * np.exp(.01))
    assert result['worlds']['ZERO_DIRECTION']['counterfactual']['mean_net'] == pytest.approx(-.04)
    assert result['worlds']['ADVERSE_DIRECTION_HIGH_VOL']['counterfactual']['mean_net'] < -.04
    assert result['target_epoch'] == 1300
    assert base['path_min_return_quantiles']['0.05'] == 0


def test_paths_and_quote_must_be_available_and_bound():
    prediction, paths, quote, config = _case()
    changed = paths.copy(); changed[0, -1] += .01
    with pytest.raises(Refused, match='NOT_FORECAST_BOUND'):
        path_outlook(prediction, changed, quote, quantity=1, config=config)
    future = {**quote, 'available_epoch': 1001.}
    with pytest.raises(Refused, match='FUTURE_QUOTE'):
        path_outlook(prediction, paths, future, quantity=1, config=config)
    assert path_outlook(prediction, paths, None, quantity=0, config=config)['worlds'] is None
    assert path_outlook(prediction, paths, quote, quantity=0, config=config)['worlds']['FITTED_DIRECTION']['counterfactual'] is None
