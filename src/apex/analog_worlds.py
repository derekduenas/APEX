"""Causal historical-analog worlds, preserving whole observed forward paths.

Nearest-neighbor distance is a transparent state-similarity heuristic. Equal
resampling weights are empirical scenario weights, not calibrated probabilities.
More draws reduce Monte Carlo noise; they do not create independent episodes.
Availability timestamps are supplied evidence, not authenticated here. The
caller owns point-in-time construction of each feature vector and forward path.
"""
from __future__ import annotations

import numpy as np

from .core import Refused, digest, finite
from .data import session
from .research_models import FEATURE_NAMES


def _features(value: dict) -> list[float]:
    if not isinstance(value, dict) or set(value) != set(FEATURE_NAMES):
        raise Refused("ANALOG_FEATURE_CONTRACT_MISMATCH")
    if any(not finite(value[name]) for name in FEATURE_NAMES):
        raise Refused("ANALOG_NONFINITE_FEATURE")
    if value["rv_30"] < 0 or not 0 <= value["session_fraction"] <= 1:
        raise Refused("ANALOG_FEATURE_DOMAIN_INVALID")
    return [float(value[name]) for name in FEATURE_NAMES]


def sample_analog_worlds(episodes: list[dict], feature_values: dict, *, cutoff: float,
                         now: float, horizon_minutes: int, paths: int, seed: int,
                         minimum_neighbors: int = 20, maximum_neighbors: int = 40,
                         minimum_training_sessions: int = 3, support_quantile: float = .95,
                         regime: str | None = None, regime_mismatch_penalty: float = 1.0) -> tuple[dict, np.ndarray]:
    """Return an auditable analog artifact and N cumulative log-return paths.

    All input episodes must have matured by ``cutoff <= now``; one future or
    malformed episode refuses the call instead of being silently filtered.
    ``price_origin_epoch`` defaults to the decision, matching the ridge target
    contract; delayed decisions must still precede the origin-based target.

    Training and nearest-neighbor sets must cover the declared minimum number
    of Eastern calendar sessions. Too few total sessions refuse fitting; too
    few selected sessions yield ``in_support=False``. The support diagnostic
    compares the current kth-neighbor distance with the declared quantile of
    leave-one-out training kth-neighbor distances. This is a geometric warning,
    not a coverage, calibration, significance, or profitability guarantee.
    When a current regime is supplied, a declared squared-distance penalty for
    regime mismatch affects neighbor selection and the leave-one-out reference.
    No volatility rescaling or stitching of historical paths is performed.
    """
    if not finite(cutoff) or not finite(now) or cutoff > now:
        raise Refused("ANALOG_INVALID_CUTOFF_OR_CLOCK")
    for name, value in (("horizon", horizon_minutes), ("paths", paths),
                        ("minimum_neighbors", minimum_neighbors), ("maximum_neighbors", maximum_neighbors),
                        ("minimum_training_sessions", minimum_training_sessions)):
        if type(value) is not int or value <= 0:
            raise Refused("ANALOG_INVALID_" + name.upper())
    if minimum_neighbors < 2 or maximum_neighbors < minimum_neighbors:
        raise Refused("ANALOG_INVALID_NEIGHBOR_BUDGET")
    if type(seed) is not int or seed < 0:
        raise Refused("ANALOG_INVALID_SEED")
    if not finite(support_quantile) or not 0 < support_quantile <= 1:
        raise Refused("ANALOG_INVALID_SUPPORT_QUANTILE")
    if regime is not None and (not isinstance(regime, str) or not regime):
        raise Refused("ANALOG_INVALID_CURRENT_REGIME")
    if not finite(regime_mismatch_penalty) or regime_mismatch_penalty <= 0:
        raise Refused("ANALOG_INVALID_REGIME_MISMATCH_PENALTY")
    query = np.asarray(_features(feature_values), dtype=float)
    if not isinstance(episodes, list) or len(episodes) < minimum_neighbors:
        raise Refused("ANALOG_INSUFFICIENT_EPISODES:" + str(minimum_neighbors))

    copied, ids = [], set()
    for row in episodes:
        if not isinstance(row, dict):
            raise Refused("ANALOG_INVALID_EPISODE")
        sample_id = row.get("sample_id")
        if not isinstance(sample_id, str) or not sample_id or sample_id in ids:
            raise Refused("ANALOG_INVALID_OR_DUPLICATE_SAMPLE_ID")
        ids.add(sample_id)
        names = ("decision_epoch", "target_epoch", "label_available_epoch")
        if not all(finite(row.get(name)) for name in names):
            raise Refused("ANALOG_INVALID_EPISODE_TIME")
        decision, target, available = (row[name] for name in names)
        origin = row.get("price_origin_epoch", decision)
        if not finite(origin) or origin > decision or decision >= target or target != origin + horizon_minutes * 60:
            raise Refused("ANALOG_TARGET_HORIZON_MISMATCH")
        if available < target:
            raise Refused("ANALOG_LABEL_AVAILABLE_BEFORE_TARGET")
        if max(decision, target, available) > cutoff:
            raise Refused("ANALOG_TRAINING_AVAILABILITY_FIREWALL")
        try:
            market_session = session(origin)
            same_session = market_session == session(target - 60)
        except (ValueError, OverflowError, OSError) as exc:
            raise Refused("ANALOG_INVALID_EPISODE_TIME") from exc
        if not same_session:
            raise Refused("ANALOG_PATH_CROSSES_SESSION")
        path = row.get("path_log_returns")
        if (not isinstance(path, list) or len(path) != horizon_minutes + 1
                or any(not finite(value) for value in path) or path[0] != 0):
            raise Refused("ANALOG_INVALID_FORWARD_PATH")
        x = _features(row.get("features"))
        row_regime = row.get("regime")
        if (row_regime is not None and (not isinstance(row_regime, str) or not row_regime)
                or regime is not None and row_regime is None):
            raise Refused("ANALOG_INVALID_EPISODE_REGIME")
        copied.append({"sample_id": sample_id, **{name: float(row[name]) for name in names},
                       "price_origin_epoch": float(origin), "session": market_session,
                       "features": dict(zip(FEATURE_NAMES, x)), "regime": row_regime,
                       "path_log_returns": [float(value) for value in path]})
    # Stable accumulation and tie breaking make input permutations byte-identical.
    copied.sort(key=lambda row: row["sample_id"])
    training_sessions = sorted({row["session"] for row in copied})
    if len(training_sessions) < minimum_training_sessions:
        raise Refused("ANALOG_INSUFFICIENT_TRAINING_SESSIONS:" + str(minimum_training_sessions))
    x = np.asarray([_features(row["features"]) for row in copied], dtype=float)
    k = min(maximum_neighbors, len(copied))
    # The reference uses the same k neighbors as the query. If only k rows
    # exist, leave-one-out can use k-1, so both use k-1 for this diagnostic.
    support_k = min(k, len(copied) - 1)
    try:
        with np.errstate(over="raise", invalid="raise", divide="raise"):
            constant = np.ptp(x, axis=0) == 0
            means, scales = x.mean(axis=0), x.std(axis=0, ddof=0)
            means[constant], scales[constant] = x[0, constant], 1.0
            standardized = (x - means) / scales
            query_z = (query - means) / scales
            distance_squared = np.mean((standardized - query_z) ** 2, axis=1)
            pairwise_squared = np.mean((standardized[:, None, :] - standardized[None, :, :]) ** 2, axis=2)
            if regime is not None:
                regimes = np.asarray([row["regime"] for row in copied])
                distance_squared += regime_mismatch_penalty * (regimes != regime)
                pairwise_squared += regime_mismatch_penalty * (regimes[:, None] != regimes[None, :])
            distances = np.sqrt(distance_squared)
            pairwise = np.sqrt(pairwise_squared)
            np.fill_diagonal(pairwise, np.inf)
            reference = np.partition(pairwise, support_k - 1, axis=1)[:, support_k - 1]
            threshold = float(np.quantile(reference, support_quantile))
    except (FloatingPointError, ValueError) as exc:
        raise Refused("ANALOG_NUMERIC_FAILURE") from exc
    if not all(np.all(np.isfinite(value)) for value in (means, scales, distances, reference, threshold)):
        raise Refused("ANALOG_NUMERIC_FAILURE")
    order = sorted(range(len(copied)), key=lambda index: (float(distances[index]), copied[index]["sample_id"]))
    selected = order[:k]
    selected_sessions = sorted({copied[index]["session"] for index in selected})
    constant_mismatch = [FEATURE_NAMES[index] for index in range(len(FEATURE_NAMES))
                         if constant[index] and query[index] != means[index]]
    support_distance = float(distances[order[support_k - 1]])
    reasons = []
    if support_distance > threshold:
        reasons.append("NEIGHBOR_DISTANCE_ABOVE_TRAINING_SUPPORT_QUANTILE")
    if constant_mismatch:
        reasons.append("QUERY_DIFFERS_ON_CONSTANT_TRAINING_FEATURE")
    if len(selected_sessions) < minimum_training_sessions:
        reasons.append("NEIGHBORS_FROM_TOO_FEW_SESSIONS")
    if regime is not None and regime not in {row["regime"] for row in copied}:
        reasons.append("QUERY_REGIME_NOT_IN_TRAINING")
    rng = np.random.default_rng(seed)
    sampled = rng.choice(np.asarray(selected), size=paths, replace=True)
    path_array = np.asarray([copied[int(index)]["path_log_returns"] for index in sampled], dtype=float)
    neighbors = [{"sample_id": copied[index]["sample_id"], "distance": float(distances[index]),
                  "weight": 1.0 / k, "session": copied[index]["session"],
                  "regime": copied[index]["regime"],
                  "regime_mismatch_squared_distance": (float(regime_mismatch_penalty)
                      if regime is not None and copied[index]["regime"] != regime else 0.0),
                  "path_hash": digest(copied[index]["path_log_returns"])}
                 for index in selected]
    artifact = {
        "schema": "APEX_ANALOG_WORLDS_V1", "model_name": "CONDITIONAL_HISTORICAL_ANALOG_V1",
        "cutoff_epoch": float(cutoff), "decision_epoch": float(now), "horizon_minutes": horizon_minutes,
        "feature_names": list(FEATURE_NAMES), "query_features": dict(zip(FEATURE_NAMES, query.tolist())),
        "current_regime": regime, "regime_role": ("SQUARED_DISTANCE_MISMATCH_PENALTY" if regime is not None else "NOT_USED"),
        "regime_mismatch_penalty": float(regime_mismatch_penalty),
        "means": means.tolist(), "scales": scales.tolist(),
        "standardization": "training_population_mean_std_constant_scale_one",
        "distance": "sqrt(mean_squared_standardized_feature_difference + regime_mismatch_penalty_if_used)",
        "training_records": copied, "training_sample_ids": [row["sample_id"] for row in copied],
        "training_digest": digest(copied), "training_rows": len(copied), "training_sessions": training_sessions,
        "minimum_neighbors": minimum_neighbors, "maximum_neighbors": maximum_neighbors,
        "minimum_training_sessions": minimum_training_sessions, "neighbors": neighbors,
        "selected_sessions": selected_sessions, "effective_sample_size": float(k),
        "effective_sample_size_basis": "1/sum(equal_neighbor_weights_squared), NOT number_of_simulated_draws",
        "dependence_note": "Historical episodes may overlap or share a session; weight ESS is not an independence guarantee",
        "support_quantile": float(support_quantile), "support_neighbor_rank": support_k,
        "training_leave_one_out_distances": reference.tolist(), "support_distance_threshold": threshold,
        "query_support_distance": support_distance, "constant_feature_mismatches": constant_mismatch,
        "in_support": not reasons, "support_reasons": reasons,
        "support_meaning": "EMPIRICAL_GEOMETRIC_DIAGNOSTIC_NOT_COVERAGE_OR_CALIBRATION",
        "resampling": "equal_weight_whole_historical_cumulative_log_paths_with_replacement",
        "path_transformation": "NONE", "seed": seed, "paths": paths,
        "availability_evidence_scope": "CALLER_SUPPLIED_EPISODE_TIMES_VALIDATED_NOT_AUTHENTICATED",
        "sampled_source_ids": [copied[int(index)]["sample_id"] for index in sampled],
        "paths_hash": digest(path_array.tolist()),
        "calibration_status": "EMPIRICAL_SCENARIO_RESAMPLING_UNCALIBRATED", "capital_authority": "NONE",
    }
    artifact["artifact_id"] = digest(artifact)
    return artifact, path_array
