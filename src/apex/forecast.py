"""Two forecast hypotheses: shrinking empirical drift and a zero-drift baseline.

GARCH Student-t (or named EWMA Gaussian fallback) supplies conditional variance.
Simulation truncates and variance-normalizes the fitted innovation family.
Both direction candidates share innovations for a comparable path experiment.
No scenario frequency is represented as calibrated probability.
"""
from __future__ import annotations

import math
import numpy as np
from scipy import special

from .core import Config, Refused, digest, finite
from .reused.contracts import ModelRefused
from .reused.vol_models import EWMA, GARCH, std_t_rvs


INNOVATION_CAP = 8.0
INNOVATION_RESAMPLE_BUDGET = 16


def _innovation_law(nu: float | None) -> dict:
    """A frozen bounded simulation law, distinct from the fitted likelihood.

    Student-t log shocks have no exponential moment. Gaussian shocks with
    recursively random EWMA variance also need bounding for a finite-horizon
    arithmetic price mean. Continuously truncated innovations give bounded paths at any
    finite horizon; exact second-moment normalization preserves the fitted
    conditional variance. The cap is a research assumption, not a market bound.
    """
    cap = INNOVATION_CAP
    if nu is None:
        tail = 2 * special.ndtr(-cap)
        inside_second = 1 - tail - 2 * cap * math.exp(-cap * cap / 2) / math.sqrt(2 * math.pi)
    else:
        fraction = cap * cap / (cap * cap + nu - 2)
        tail = special.betainc(nu / 2, .5, 1 - fraction)
        inside_second = special.betainc(1.5, (nu - 2) / 2, fraction)
    second = inside_second / (1 - tail)
    return {"innovation_law": "TRUNCATED_VARIANCE_NORMALIZED_V1",
            "base_family": "STANDARDIZED_STUDENT_T" if nu is not None else "STANDARD_NORMAL",
            "nu": nu, "raw_standardized_cap": cap,
            "base_tail_probability_removed": float(tail),
            "resample_budget_per_step": INNOVATION_RESAMPLE_BUDGET,
            "second_moment_before_normalization": float(second),
            "normalized_absolute_bound": float(cap / math.sqrt(second)),
            "price_mean_exists": True,
            "cap_sensitivity_status": "NOT_ESTABLISHED; compare frozen caps on chronological holdouts",
            "scope": "Bounded simulation assumption; fitted likelihood remains uncapped; not a bound on market losses"}


def _draw_innovations(rng: np.random.Generator, law: dict, count: int) -> np.ndarray:
    nu = law["nu"]
    raw = std_t_rvs(rng, nu, count) if nu is not None else rng.standard_normal(count)
    outside = np.abs(raw) > law["raw_standardized_cap"]
    for _ in range(law["resample_budget_per_step"]):
        if not np.any(outside):
            break
        size = int(outside.sum())
        raw[outside] = std_t_rvs(rng, nu, size) if nu is not None else rng.standard_normal(size)
        outside = np.abs(raw) > law["raw_standardized_cap"]
    if np.any(outside):
        raise Refused("INNOVATION_REJECTION_BUDGET_EXHAUSTED")
    return raw / math.sqrt(law["second_moment_before_normalization"])


def predict(returns: list[dict], snapshot: dict, config: Config) -> tuple[dict, np.ndarray]:
    now = snapshot["now"]
    origin = snapshot["fields"]["last_bar_event"] + 60
    target = origin + config.horizon_minutes * 60
    if target <= now:
        raise Refused("FORECAST_TARGET_NOT_FUTURE")
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
    simulation = _innovation_law(model.p["nu"] if isinstance(model, GARCH) else None)
    if isinstance(model, GARCH):
        h = np.full(count, model.next_h(model.p["h_last"], model.p["e_last"]))
    else:
        h = np.full(count, model.h)
    cum = np.zeros(count)
    paths = np.zeros((count, horizon + 1))
    for step in range(horizon):
        z = _draw_innovations(rng, simulation, count)
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
              "target_epoch": target, "spot": snapshot["fields"]["spot"], "price_origin_epoch": origin,
              "price_origin_basis": "LATEST_COMPLETED_BAR_CLOSE",
              "price_origin_age_seconds": now - origin,
              "training_rows": rows, "training_digest": digest(rows), "residual_digest": digest(model_rows),
              "direction": {"name": "SHRUNK_EMPIRICAL_MEAN_V1", "per_minute_log_drift": drift, "shrinkage_pseudocount": 200},
              "variance": artifact, "simulation": simulation, "fit_attempts": attempts, "seed": seed, "path_count": count,
              "horizon_minutes": horizon, "calibration_status": "SIMULATED_UNCALIBRATED",
              "quantiles": {str(q): float(np.quantile(paths[:, -1], q)) for q in (0.05, 0.5, 0.95)},
              "p_up": float(np.mean(paths[:, -1] > 0)), "baseline_p_up": 0.5,
              "baseline_probability_basis": "EXACT_SYMMETRY_OF_ZERO_DRIFT_RETURN_LAW",
              "simulated_baseline_p_up": float(np.mean(baseline > 0)),
              "mean_terminal_price": float(snapshot["fields"]["spot"] * np.exp(paths[:, -1]).mean()),
              "mc_standard_error_mean_log_return": float(np.std(paths[:, -1], ddof=1) / math.sqrt(count)),
              "paths_digest": digest(paths.tolist()),
              "limitations": ["No parameter-uncertainty simulation", "No jumps, skew surface, or causal catalyst model",
                              "Bounded innovation cap changes fitted tails; cap sensitivity not established; not a market-risk bound",
                              "Bar-origin distribution; candidate quote rebasing is a separate return-law assumption",
                              "No calibrated capital authorization"]}
    record["forecast_id"] = digest(record)
    return record, paths
