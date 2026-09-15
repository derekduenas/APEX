"""Frozen, causal feature and ridge hypotheses for offline research only.

The six-feature contract is fixed before fitting. ``rv_30`` is the square root
of the sum of squared one-minute log returns over the latest 30 contiguous
returns in the current Eastern session, ending at the snapshot's last completed
bar. It is not annualized or demeaned. ``session_fraction`` measures elapsed
completed minutes since 09:30 Eastern divided by 390; missing bars do not shorten
the clock. The upstream reader's regular-session assumption is retained (this
is not an exchange holiday or early-close calendar).

Ridge predicts total log return over the declared horizon. Its fixed L2 penalty
applies only to coefficients after training-only population standardization.
These models confer neither calibrated probabilities nor capital authority.
"""
from __future__ import annotations

import math
from datetime import datetime

import numpy as np

from .core import Refused, digest, finite
from .data import EASTERN, regular, session


FEATURE_NAMES = (
    "ret_1", "ret_5", "ret_15", "rv_30", "log_spot_to_vwap", "session_fraction",
)


def _feature_values(values: dict) -> list[float]:
    if not isinstance(values, dict):
        raise Refused("INVALID_RIDGE_FEATURES")
    for name in FEATURE_NAMES:
        if not finite(values.get(name)):
            raise Refused("MISSING_OR_NONFINITE_FEATURE:" + name)
    return [float(values[name]) for name in FEATURE_NAMES]


def features(snapshot: dict, returns: list[dict]) -> dict:
    """Read a snapshot and its causal return history without imputing missing data.

    The latest 30 supplied returns must form one uninterrupted current-session
    chain. Future, delayed, duplicate, out-of-order, and unknown input rows in
    that consumed chain are refused rather than silently filtered or repaired.
    The upstream snapshot carries the spot, cumulative VWAP proxy, and returns.
    """
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("fields"), dict):
        raise Refused("INVALID_FEATURE_SNAPSHOT")
    fields = snapshot["fields"]
    now, last = snapshot.get("now"), fields.get("last_bar_event")
    if not finite(now) or not finite(last) or last % 60 != 0:
        raise Refused("INVALID_FEATURE_CLOCK")
    try:
        valid_clock = regular(last) and session(last) == session(now)
    except (ValueError, OverflowError, OSError) as exc:
        raise Refused("INVALID_FEATURE_CLOCK") from exc
    if not valid_clock or not 0 <= now - (last + 60) <= 120:
        raise Refused("FEATURE_AVAILABILITY_FIREWALL:LAST_BAR")
    for name in ("spot", "close_weighted_vwap_proxy", "ret_1", "ret_5", "ret_15"):
        if not finite(fields.get(name)):
            raise Refused("MISSING_OR_NONFINITE_FEATURE:" + name)
    if fields["spot"] <= 0 or fields["close_weighted_vwap_proxy"] <= 0:
        raise Refused("NONPOSITIVE_SPOT_OR_VWAP")
    if not isinstance(returns, list) or len(returns) < 30:
        raise Refused("INSUFFICIENT_CONTIGUOUS_SESSION_RETURNS:30")
    ids = snapshot.get("bar_ids")
    if not isinstance(ids, list) or any(not isinstance(i, str) or not i for i in ids):
        raise Refused("INVALID_FEATURE_INPUT_IDS")
    known_ids = set(ids)
    block = returns[-30:]
    values, previous_right = [], None
    for index, row in enumerate(block):
        if not isinstance(row, dict) or not all(finite(row.get(k)) for k in ("event_time", "available", "ret_1")):
            raise Refused("INVALID_FEATURE_RETURN")
        event, available = row["event_time"], row["available"]
        if available < event or available > now or event > now:
            raise Refused("FEATURE_AVAILABILITY_FIREWALL:RETURN")
        expected = last + 60 - (29 - index) * 60
        if event != expected or session(event - 60) != session(last) or not regular(event - 120):
            raise Refused("NONCONTIGUOUS_CURRENT_SESSION_RETURNS")
        input_ids = row.get("input_ids")
        if (not isinstance(input_ids, list) or len(input_ids) != 2
                or any(not isinstance(i, str) or i not in known_ids for i in input_ids)
                or input_ids[0] == input_ids[1]
                or (previous_right is not None and previous_right != input_ids[0])):
            raise Refused("INVALID_FEATURE_RETURN_PROVENANCE")
        previous_right = input_ids[1]
        values.append(row["ret_1"])
    clock = datetime.fromtimestamp(last, EASTERN)
    result = {name: float(fields[name]) for name in ("ret_1", "ret_5", "ret_15")}
    # hypot avoids overflow in the intermediate sum of squares.
    result.update(rv_30=math.hypot(*values),
                  log_spot_to_vwap=math.log(fields["spot"]) - math.log(fields["close_weighted_vwap_proxy"]),
                  session_fraction=(clock.hour * 60 + clock.minute + 1 - 570) / 390)
    _feature_values(result)
    return result


def fit_ridge(training_records: list[dict], *, cutoff: float, horizon_minutes: int,
              alpha: float = 10.0) -> dict:
    """Fit a fixed ridge and return a self-contained, JSON-serializable snapshot.

    Every label must have matured and become available by ``cutoff``. The input
    is training data only: callers partition chronologically before calling.
    ``price_origin_epoch`` optionally names the completed bar underlying the
    return target; it defaults to the decision for legacy records. A delayed
    decision may follow that origin but must precede the origin-based target.
    The returned model retains copied training inputs, scaler, fit settings,
    availability bounds and a content digest. Prediction verifies that digest;
    mutating source records cannot alter this model, and mutating this model
    invalidates its identity. The digest is internal consistency, not external
    authentication or evidence that a supplied availability claim is measured.
    """
    if not finite(cutoff):
        raise Refused("INVALID_RIDGE_CUTOFF")
    if type(horizon_minutes) is not int or horizon_minutes <= 0:
        raise Refused("INVALID_RIDGE_HORIZON")
    if not finite(alpha) or alpha <= 0:
        raise Refused("INVALID_RIDGE_ALPHA")
    if not isinstance(training_records, list) or len(training_records) < 2:
        raise Refused("INSUFFICIENT_RIDGE_TRAINING_ROWS:2")
    copied, sample_ids = [], set()
    for record in training_records:
        if not isinstance(record, dict):
            raise Refused("INVALID_RIDGE_TRAINING_RECORD")
        sample_id = record.get("sample_id")
        if not isinstance(sample_id, str) or not sample_id or sample_id in sample_ids:
            raise Refused("INVALID_OR_DUPLICATE_TRAINING_SAMPLE_ID")
        sample_ids.add(sample_id)
        times = ("decision_epoch", "target_epoch", "label_available_epoch")
        if not all(finite(record.get(name)) for name in times):
            raise Refused("INVALID_RIDGE_TRAINING_TIME")
        decision, target, available = (record[name] for name in times)
        origin = record.get("price_origin_epoch", decision)
        if not finite(origin):
            raise Refused("INVALID_RIDGE_PRICE_ORIGIN")
        if origin > decision:
            raise Refused("RIDGE_PRICE_ORIGIN_AFTER_DECISION")
        if decision >= target or target != origin + 60 * horizon_minutes:
            raise Refused("RIDGE_TARGET_HORIZON_MISMATCH")
        if available < target:
            raise Refused("RIDGE_LABEL_AVAILABLE_BEFORE_TARGET")
        if decision > cutoff or target > cutoff or available > cutoff:
            raise Refused("RIDGE_TRAINING_AVAILABILITY_FIREWALL")
        if not finite(record.get("realized_log_return")):
            raise Refused("NONFINITE_RIDGE_LABEL")
        x = _feature_values(record.get("features"))
        copied.append({"sample_id": sample_id,
                       **{name: float(record[name]) for name in times},
                       "price_origin_epoch": float(origin),
                       "features": dict(zip(FEATURE_NAMES, x)),
                       "realized_log_return": float(record["realized_log_return"])})
    x = np.asarray([_feature_values(r["features"]) for r in copied], dtype=float)
    y = np.asarray([r["realized_log_return"] for r in copied], dtype=float)
    try:
        with np.errstate(over="raise", invalid="raise", divide="raise"):
            means = x.mean(axis=0)
            scales = x.std(axis=0, ddof=0)
            scales[scales == 0] = 1.0  # Constant, observed features; no imputation.
            standardized = (x - means) / scales
            intercept = float(y.mean())
            coefficients = np.linalg.solve(
                standardized.T @ standardized + float(alpha) * np.eye(len(FEATURE_NAMES)),
                standardized.T @ (y - intercept))
    except (FloatingPointError, np.linalg.LinAlgError) as exc:
        raise Refused("RIDGE_NUMERIC_FAILURE") from exc
    if not all(np.all(np.isfinite(v)) for v in (means, scales, coefficients, intercept)):
        raise Refused("RIDGE_NUMERIC_FAILURE")
    model = {
        "schema": "APEX_RIDGE_MODEL_V1", "model_name": "RIDGE_DIRECTION_V1",
        "feature_names": list(FEATURE_NAMES), "means": means.tolist(), "scales": scales.tolist(),
        "coefficients": coefficients.tolist(), "intercept": intercept,
        "alpha": float(alpha), "horizon_minutes": horizon_minutes, "cutoff_epoch": float(cutoff),
        "objective": "sum_squared_errors + alpha * sum_squared_standardized_coefficients",
        "standardization": "training_population_mean_std_constant_scale_one",
        "target": "total_horizon_log_return", "training_records": copied,
        "training_sample_ids": [r["sample_id"] for r in copied], "training_rows": len(copied),
        "training_digest": digest(copied),
        "max_decision_epoch": max(r["decision_epoch"] for r in copied),
        "max_target_epoch": max(r["target_epoch"] for r in copied),
        "max_label_available_epoch": max(r["label_available_epoch"] for r in copied),
        "calibration_status": "RESEARCH_UNCALIBRATED",
        "capital_authority": "NONE",
    }
    model["model_id"] = digest(model)
    return model


def predict_ridge(model: dict, feature_values: dict) -> float:
    """Return total-horizon log return with the persisted training-only scaler."""
    if not isinstance(model, dict) or model.get("schema") != "APEX_RIDGE_MODEL_V1":
        raise Refused("INVALID_RIDGE_MODEL")
    try:
        consistent = model.get("model_id") == digest({k: v for k, v in model.items() if k != "model_id"})
    except (TypeError, ValueError) as exc:
        raise Refused("INVALID_RIDGE_MODEL") from exc
    if not consistent:
        raise Refused("RIDGE_MODEL_DIGEST_MISMATCH")
    if model.get("feature_names") != list(FEATURE_NAMES):
        raise Refused("RIDGE_FEATURE_CONTRACT_MISMATCH")
    for name in ("means", "scales", "coefficients"):
        values = model.get(name)
        if (not isinstance(values, list) or len(values) != len(FEATURE_NAMES)
                or any(not finite(value) for value in values)):
            raise Refused("INVALID_RIDGE_MODEL:" + name)
    if not finite(model.get("intercept")) or any(value <= 0 for value in model["scales"]):
        raise Refused("INVALID_RIDGE_MODEL_SCALER_OR_INTERCEPT")
    x = np.asarray(_feature_values(feature_values), dtype=float)
    try:
        with np.errstate(over="raise", invalid="raise", divide="raise"):
            result = float((x - model["means"]) / model["scales"] @ model["coefficients"] + model["intercept"])
    except FloatingPointError as exc:
        raise Refused("RIDGE_PREDICTION_NUMERIC_FAILURE") from exc
    if not finite(result):
        raise Refused("RIDGE_PREDICTION_NUMERIC_FAILURE")
    return result
