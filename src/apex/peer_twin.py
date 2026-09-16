"""As-of market/sector repricing measurements for one frozen research hypothesis.

This is an observable peer-state projection, not a population of invented agents,
an order-flow measurement, a calibrated probability, or permission to trade.
"""
from __future__ import annotations

from collections import Counter
import math

import numpy as np

from .core import Refused, digest, finite
from .data import normalize, regular, session, twin, visible


HYPOTHESIS = {
    "hypothesis_id": "PEER_REPRICING_LAG_V1",
    "claim": "A stock lagging a positive market/sector shock may subsequently reprice after its residual starts recovering.",
    "status": "UNTESTED_HYPOTHESIS_NOT_CALIBRATED",
    "default_training_minutes": 120,
    "minimum_training_minutes": 20,
    "maximum_training_minutes": 384,
    "shock_minutes": 5,
    "estimator": "CENTERED_STANDARDIZED_TWO_FACTOR_RIDGE_V1",
    "ridge_lambda": 1e-6,
    "minimum_factor_std": 1e-10,
    "minimum_residual_std": 1e-10,
    "maximum_origin_age_seconds": 120,
    "setup_thresholds": {
        "market_shock_gt": 0.0, "sector_shock_gt": 0.0,
        "predicted_shock_gt": 0.0, "lag_gap_gt": 0.0,
        "last_minute_residual_gt": 0.0,
    },
    "threshold_basis": "Frozen research choices, not learned or calibrated acceptance thresholds.",
    "residual_z_definition": "(target shock return - fitted peer shock return) / sqrt(shock_minutes * training residual variance)",
    "residual_z_limitation": "Descriptive IID scaling only; serial dependence and beta uncertainty are not modeled. Not a probability or significance test.",
}


def _aligned_bars(observations, *, symbol, now, origin, first):
    """Use the real visibility gate; check normalized identities before consuming.

    Only the requested as-of session window can enter the fit. A correction that
    becomes available after ``now`` cannot change this result or its digest.
    """
    candidates = []
    for row in observations:
        if not isinstance(row, dict):
            raise Refused("PEER_INPUT_NOT_NORMALIZED")
        if row.get("symbol") != symbol or row.get("kind") != "bar":
            continue
        if not finite(row.get("available_epoch")) or not finite(row.get("event_epoch")):
            raise Refused("PEER_INVALID_TIME:" + symbol)
        if row["available_epoch"] > now or row["event_epoch"] < first:
            continue
        if session(row["event_epoch"]) != session(now) or not regular(row["event_epoch"]):
            continue
        checked, rejected = normalize({"schema": "APEX_DATA_V1", "observations": [row]})
        if rejected or len(checked) != 1 or checked[0]["observation_id"] != row.get("observation_id"):
            raise Refused("PEER_CORRUPT_OR_UNNORMALIZED_BAR:" + symbol)
        if row["event_epoch"] + 60 > now:
            raise Refused("PEER_INCOMPLETE_BAR:" + symbol)
        candidates.append(row)
    bars, conflicts = visible(candidates, now=now, symbol=symbol, kind="bar")
    required = set(range(int(first), int(origin) + 1, 60))
    if any(c["event_epoch"] in required for c in conflicts):
        raise Refused("PEER_CONFLICTING_BAR:" + symbol)
    if not bars:
        raise Refused("PEER_COMPLETED_BARS_UNAVAILABLE:" + symbol)
    if bars[-1]["event_epoch"] != origin:
        raise Refused("PEER_ORIGIN_MISMATCH_OR_STALE:" + symbol)
    by_event = {b["event_epoch"]: b for b in bars}
    if not required <= by_event.keys():
        raise Refused("PEER_MISSING_OR_NONCONTIGUOUS_BAR:" + symbol)
    return [by_event[t] for t in sorted(required)]


def peer_state(observations: list[dict], snapshot: dict, *, market_symbol: str,
               sector_symbol: str, training_minutes: int = 120, shock_minutes: int = 5) -> dict:
    """Build a causal two-factor stock state ending at the exact twin origin.

    ``training_minutes`` counts prior one-minute return intervals, excluding all
    five shock intervals. This requires training_minutes + 6 aligned bars for
    each of three distinct symbols in the current regular session. No missing
    interval is filled and no overnight return is used.
    """
    if type(training_minutes) is not int or not HYPOTHESIS["minimum_training_minutes"] <= training_minutes <= HYPOTHESIS["maximum_training_minutes"]:
        raise Refused("PEER_INVALID_TRAINING_WINDOW")
    if type(shock_minutes) is not int or shock_minutes != HYPOTHESIS["shock_minutes"]:
        raise Refused("PEER_FROZEN_HYPOTHESIS_REQUIRES_FIVE_MINUTE_SHOCK")
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("fields"), dict):
        raise Refused("PEER_INVALID_SNAPSHOT")
    symbol, now = snapshot.get("symbol"), snapshot.get("now")
    symbols = (symbol, market_symbol, sector_symbol)
    if any(not isinstance(s, str) or not s for s in symbols) or len(set(symbols)) != 3:
        raise Refused("PEER_REQUIRES_THREE_DISTINCT_SYMBOLS")
    origin = snapshot["fields"].get("last_bar_event")
    if not finite(now) or not finite(origin) or origin % 60:
        raise Refused("PEER_INVALID_ORIGIN")
    if not regular(origin) or session(origin) != session(now) or not 0 <= now - (origin + 60) <= HYPOTHESIS["maximum_origin_age_seconds"]:
        raise Refused("PEER_ORIGIN_INCOMPLETE_OR_STALE")
    first = origin - (training_minutes + shock_minutes) * 60
    if not regular(first) or session(first) != session(origin):
        raise Refused("PEER_INSUFFICIENT_SAME_SESSION_HISTORY")

    aligned = {s: _aligned_bars(observations, symbol=s, now=now, origin=origin, first=first)
               for s in symbols}
    try:
        actual_snapshot, _ = twin(observations, now, symbol)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise Refused("PEER_SNAPSHOT_REBUILD_REFUSED") from exc
    if actual_snapshot != snapshot:
        raise Refused("PEER_SNAPSHOT_ORIGIN_OR_INPUT_MISMATCH")

    returns = {s: np.diff(np.log(np.asarray([b["close"] for b in aligned[s]], dtype=float)))
               for s in symbols}
    if not all(np.isfinite(values).all() for values in returns.values()):
        raise Refused("PEER_NONFINITE_RETURN")
    y = returns[symbol][:training_minutes]
    x = np.column_stack([returns[s][:training_minutes] for s in symbols[1:]])
    means, scales = x.mean(axis=0), x.std(axis=0)
    if not np.isfinite(scales).all() or np.any(scales <= HYPOTHESIS["minimum_factor_std"]):
        raise Refused("PEER_DEGENERATE_FACTOR_TRAINING_VARIANCE")
    z = (x - means) / scales
    gram = z.T @ z / training_minutes + HYPOTHESIS["ridge_lambda"] * np.eye(2)
    try:
        standardized_beta = np.linalg.solve(gram, z.T @ (y - y.mean()) / training_minutes)
    except np.linalg.LinAlgError as exc:
        raise Refused("PEER_UNSTABLE_FACTOR_FIT") from exc
    beta = standardized_beta / scales
    intercept = float(y.mean() - means @ beta)
    residuals = y - (intercept + x @ beta)
    variance = float(residuals @ residuals / (training_minutes - 3))
    if not finite(variance) or variance <= HYPOTHESIS["minimum_residual_std"] ** 2:
        raise Refused("PEER_DEGENERATE_TRAINING_RESIDUAL_VARIANCE")

    shocks = {s: float(returns[s][training_minutes:].sum()) for s in symbols}
    predicted = float(shock_minutes * intercept + beta @ np.asarray([shocks[s] for s in symbols[1:]]))
    last_prediction = float(intercept + beta @ np.asarray([returns[s][-1] for s in symbols[1:]]))
    lag_gap = predicted - shocks[symbol]
    recovery = float(returns[symbol][-1] - last_prediction)
    agreement = shocks[market_symbol] > 0 and shocks[sector_symbol] > 0
    features = {
        "target_ret_5": shocks[symbol], "market_ret_5": shocks[market_symbol],
        "sector_ret_5": shocks[sector_symbol], "predicted_peer_ret_5": predicted,
        "lag_gap": lag_gap, "residual_z": -lag_gap / math.sqrt(shock_minutes * variance),
        "recovery_last_minute": recovery, "peer_agreement": float(agreement),
    }
    if not all(finite(v) for v in features.values()) or not np.isfinite(beta).all() or not finite(intercept):
        raise Refused("PEER_NONFINITE_FIT_OR_FEATURE")
    eligible = bool(agreement and predicted > 0 and lag_gap > 0 and recovery > 0)
    train_bars = {s: aligned[s][:training_minutes + 1] for s in symbols}
    consumed = [b for s in symbols for b in aligned[s]]
    source = {
        "clock": "EVENT_START_COMPLETION_AND_AVAILABLE_EPOCH_PRESERVED_SEPARATELY",
        "as_of_epoch": now, "origin_event_epoch": origin, "origin_complete_epoch": origin + 60,
        "origin_age_seconds": now - (origin + 60),
        "session": session(now), "same_session_contiguous_one_minute_bars": True,
        "session_clock_limitation": "US Eastern weekday 09:30-16:00 clock; not independently certified against an exchange calendar or early closes.",
        "max_available_epoch": max(b["available_epoch"] for b in consumed),
        "availability_basis_counts": dict(sorted(Counter(b["availability_basis"] for b in consumed).items())),
        "bar_clock_digest": digest([{k: b[k] for k in ("observation_id", "event_epoch", "available_epoch", "availability_basis")} for b in consumed]),
        "retrieval_clock": "Document-level retrieval metadata is not provided to this function; availability labels are not independently authenticated.",
        "historical_availability_limitation": "BAR_COMPLETION_ASSUMPTION_V1 does not establish original receipt time or unrevised prices.",
        "microstructure": "BAR_ONLY; no order-flow, liquidity, or quote-based execution claim.",
    }
    fit = {
        "method": HYPOTHESIS["estimator"], "ridge_lambda": HYPOTHESIS["ridge_lambda"],
        "training_minutes": training_minutes, "shock_minutes_excluded": shock_minutes,
        "training_first_interval_complete_epoch": first + 120,
        "training_cutoff_event_epoch": origin - shock_minutes * 60,
        "training_cutoff_complete_epoch": origin - shock_minutes * 60 + 60,
        "training_max_available_epoch": max(b["available_epoch"] for bars in train_bars.values() for b in bars),
        "intercept_per_minute": intercept,
        "beta_market": float(beta[0]), "beta_sector": float(beta[1]),
        "factor_means": {market_symbol: float(means[0]), sector_symbol: float(means[1])},
        "factor_scales": {market_symbol: float(scales[0]), sector_symbol: float(scales[1])},
        "regularized_condition_number": float(np.linalg.cond(gram)),
        "training_residual_variance": variance,
        "residual_variance_denominator": training_minutes - 3,
        "training_bar_ids": {s: [b["observation_id"] for b in train_bars[s]] for s in symbols},
        "training_return_digest": digest({s: returns[s][:training_minutes].tolist() for s in symbols}),
    }
    result = {
        "schema": "PEER_TWIN_V1", "hypothesis_id": HYPOTHESIS["hypothesis_id"],
        "hypothesis_digest": digest(HYPOTHESIS), "status": HYPOTHESIS["status"],
        "symbol": symbol, "market_symbol": market_symbol, "sector_symbol": sector_symbol,
        "now": now, "snapshot_id": snapshot["snapshot_id"], "origin_event_epoch": origin,
        "features": features, "eligible_setup": eligible, "fit": fit, "source": source,
        "consumed_bar_ids": {s: [b["observation_id"] for b in aligned[s]] for s in symbols},
    }
    return {**result, "peer_state_id": digest(result)}
