"""Forward-path interpretation under explicit, uncalibrated model assumptions.

These diagnostics expose model dependence. They neither select a strategy nor
change admission/risk policy, and scenario frequencies are not market odds.
"""
import numpy as np

from .core import Config, Refused, digest, fee


def _quantiles(values):
    return {str(q): float(np.quantile(values, q)) for q in (0.05, 0.5, 0.95)}


def path_outlook(forecast, paths, quote, *, quantity, config: Config):
    paths = np.asarray(paths, dtype=float)
    horizon = config.horizon_minutes
    if (paths.shape != (config.paths, horizon + 1) or not np.isfinite(paths).all()
            or np.max(np.abs(paths)) > 100 or np.any(paths[:, 0] != 0)
            or digest(paths.tolist()) != forecast['paths_digest']):
        raise Refused('PAPER_OUTLOOK_PATHS_NOT_FORECAST_BOUND')
    if type(quantity) is not int or quantity < 0:
        raise Refused('PAPER_OUTLOOK_QUANTITY_INVALID')
    result = {'schema': 'APEX_PAPER_PATH_OUTLOOK_V1', 'forecast_id': forecast['forecast_id'],
              'paths_digest': forecast['paths_digest'], 'created_epoch': forecast['created_epoch'],
              'calibration': 'UNVALIDATED_MODEL_SCENARIOS_NOT_CALIBRATED_MARKET_PROBABILITIES',
              'world_probabilities': None, 'admission_effect': 'DIAGNOSTIC_ONLY_UNCHANGED_PAPER_POLICY',
              'parameter_uncertainty': 'NOT_ESTIMATED',
              'regime_transition_model': 'NOT_ESTIMATED',
              'limitations': ['Shared innovations are conditional on the fitted model, not independent market evidence.',
                  'Stress worlds are declared sensitivities, not inferred alternative-world probabilities.',
                  'Price and excursion quantiles are not guaranteed bounds; intraminute paths are unobserved.',
                  'Same horizon law rebased to current quote is an assumption.',
                  'Illustrative spread and fees omit queue priority and endogenous market impact.']}
    if quote is None:
        return {**result, 'status': 'NO_QUOTE_ANCHOR', 'worlds': None}
    spot = (quote['bid'] + quote['ask']) / 2
    half_spread = (quote['ask'] - quote['bid']) / 2
    if not np.isfinite(spot) or spot <= 0 or half_spread < 0:
        raise Refused('PAPER_OUTLOOK_INVALID_QUOTE')
    if quote['available_epoch'] > forecast['created_epoch']:
        raise Refused('PAPER_OUTLOOK_FUTURE_QUOTE')
    drift = forecast['direction']['per_minute_log_drift'] * np.arange(horizon + 1)
    residual = paths - drift
    worlds = {'FITTED_DIRECTION': paths, 'ZERO_DIRECTION': residual,
              'ADVERSE_DIRECTION_HIGH_VOL': 1.5 * residual - np.abs(drift)}
    report = {}
    for name, values in worlds.items():
        prices = spot * np.exp(values)
        economics = None
        if quantity:
            # Fixed diagnostic position; actual book uses later observed quotes,
            # fees and configured adverse slippage, rather than these values.
            net = (prices[:, -1] - half_spread - quote['ask']) * quantity - float(2 * fee(quantity, config))
            tail_cut = float(np.quantile(net, .05))
            economics = {'quantity': quantity, 'mean_net': float(net.mean()),
                         'net_quantiles': _quantiles(net),
                         'worst_five_percent_mean_net': float(net[net <= tail_cut].mean()),
                         'simulated_fraction_net_positive': float(np.mean(net > 0)),
                         'basis': 'FIXED_CURRENT_SPREAD_AND_COMMISSION_COUNTERFACTUAL_NOT_PAPER_FILL_PNL'}
        report[name] = {'terminal_price_quantiles': _quantiles(prices[:, -1]),
                        'path_max_return_quantiles': _quantiles(np.max(prices / spot - 1, axis=1)),
                        'path_min_return_quantiles': _quantiles(np.min(prices / spot - 1, axis=1)),
                        'horizon_price_quantiles': {str(m): _quantiles(prices[:, m])
                            for m in sorted({min(horizon, m) for m in (5, 15, 30, 60, horizon)})},
                        'counterfactual': economics}
    means = [world['counterfactual']['mean_net'] for world in report.values()] if quantity else []
    return {**result, 'status': 'MODELED_OUTLOOK', 'quote_id': quote['observation_id'],
            'price_anchor': spot, 'target_epoch': forecast['created_epoch'] + horizon * 60,
            'worlds': report, 'mean_net_sign_disagrees_across_worlds': min(means) <= 0 < max(means) if means else None,
            'uncertainty_scope': 'Three deterministic sensitivity worlds; no calibrated epistemic uncertainty estimate'}
