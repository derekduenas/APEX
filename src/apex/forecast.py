"""Two forecast hypotheses: shrinking empirical drift and a zero-drift baseline.

GARCH Student-t (or named EWMA Gaussian fallback) supplies conditional variance.
Both direction candidates share innovations for a comparable path experiment.
No scenario frequency is represented as calibrated probability.
"""
from __future__ import annotations

import math
import numpy as np

from .core import Config, Refused, digest, finite
from .reused.contracts import ModelRefused
from .reused.vol_models import EWMA, GARCH, std_t_rvs


def predict(returns: list[dict], snapshot: dict, config: Config) -> tuple[dict, np.ndarray]:
    now = snapshot["now"]
    rows = returns[-config.training_returns:]
    if len(rows) < 200:
        raise Refused(f"INSUFFICIENT_ADJACENT_RETURNS:{len(rows)}<200")
    if any(not finite(r.get("available")) or r["available"] > now or r["event_time"] > now for r in rows):
        raise Refused("MODEL_AVAILABILITY_FIREWALL")
    values = np.asarray([r["ret_1"] for r in rows], dtype=float)
    if not np.all(np.isfinite(values)):
        raise Refused("NONFINITE_RETURN")
    # A deliberately simple, frozen research hypothesis, not an admitted signal.
    drift = float(values.mean()) * len(values) / (len(values) + 200)
    residuals = values - drift
    if float(np.var(residuals)) <= 1e-16:
        raise Refused("DEGENERATE_VARIANCE")
    model_rows = [{**r, "ret_1": float(e)} for r, e in zip(rows, residuals)]
    attempts = []
    model = GARCH() if config.variance == "garch" else EWMA()
    try:
        model.fit(model_rows, cutoff_epoch=now)
        attempts.append({"model": model.model_id, "status": "FITTED", "fit_calls": model.fit_count})
    except (ModelRefused, ArithmeticError, ValueError) as exc:
        # Firewall or input violations never become a different model's permission.
        if "FIREWALL" in str(exc) or config.variance != "garch":
            raise Refused(str(exc)) from exc
        attempts.append({"model": model.model_id, "status": "REFUSED", "reason": str(exc), "fit_calls": model.fit_count})
        model = EWMA()
        model.fit(model_rows, cutoff_epoch=now)
        attempts.append({"model": model.model_id, "status": "FITTED_FALLBACK", "fit_calls": model.fit_count})
    artifact = model.serialize()
    seed = int(digest({"seed": config.seed, "snapshot": snapshot["snapshot_id"]})[:8], 16)
    rng = np.random.default_rng(seed)
    horizon, count = config.horizon_minutes, config.paths
    if isinstance(model, GARCH):
        h = np.full(count, model.next_h(model.p["h_last"], model.p["e_last"]))
    else:
        h = np.full(count, model.h)
    cum = np.zeros(count)
    paths = np.zeros((count, horizon + 1))
    for step in range(horizon):
        z = std_t_rvs(rng, model.p["nu"], count) if isinstance(model, GARCH) else rng.standard_normal(count)
        e = np.sqrt(h) * z
        cum += drift + e
        paths[:, step + 1] = cum
        if isinstance(model, GARCH):
            p = model.p
            h = p["omega"] + (p["alpha"] + p["gamma"] * (e < 0)) * e * e + p["beta"] * h
        else:
            h = model.lam * h + (1 - model.lam) * e * e
    if not np.all(np.isfinite(paths)) or np.max(np.abs(paths)) > 100:
        raise Refused("SIMULATION_NUMERIC_FAILURE")
    baseline = paths[:, -1] - drift * horizon
    record = {"snapshot_id": snapshot["snapshot_id"], "created_epoch": now, "input_cutoff_epoch": now,
              "target_epoch": now + horizon * 60, "spot": snapshot["fields"]["spot"],
              "training_rows": rows, "training_digest": digest(rows), "residual_digest": digest(model_rows),
              "direction": {"name": "SHRUNK_EMPIRICAL_MEAN_V1", "per_minute_log_drift": drift, "shrinkage_pseudocount": 200},
              "variance": artifact, "fit_attempts": attempts, "seed": seed, "path_count": count,
              "horizon_minutes": horizon, "calibration_status": "SIMULATED_UNCALIBRATED",
              "quantiles": {str(q): float(np.quantile(paths[:, -1], q)) for q in (0.05, 0.5, 0.95)},
              "p_up": float(np.mean(paths[:, -1] > 0)), "baseline_p_up": float(np.mean(baseline > 0)),
              "mean_terminal_price": float(snapshot["fields"]["spot"] * np.exp(paths[:, -1]).mean()),
              "mc_standard_error_mean_log_return": float(np.std(paths[:, -1], ddof=1) / math.sqrt(count)),
              "paths_digest": digest(paths.tolist()),
              "limitations": ["No parameter-uncertainty simulation", "No jumps, skew surface, or causal catalyst model", "No calibrated capital authorization"]}
    record["forecast_id"] = digest(record)
    return record, paths
