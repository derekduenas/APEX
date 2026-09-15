"""Reconstruct a research run from its retained input and chronological ledger.

The reader recomputes features, matured labels, ridge fits, decisions, scores,
selection, and summary. Hashes bind artifacts internally; they do not authenticate
provider history, executed source code, or the statistical validity of a model.
The persisted variance optimizer is not independently rerun for successful fits.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re

import numpy as np

from .core import Config, Refused, code_manifest, digest, money
from .data import normalize, regular, session, twin, visible
from .decision import evaluate, quote_at
from .forecast import _innovation_law, predict
from .ledger import read_verified
from .research import MODELS, ResearchPlan, _phase, _selection, distribution_score, summarize
from .research_models import features, fit_ridge, predict_ridge
from .reused.contracts import digest as model_digest
from .verification import _verify_simulation


def _require(condition, reason):
    if not condition:
        raise Refused(reason)


def _array(root: Path, filename: str, expected_name: str, shape: tuple, expected_digest: str) -> np.ndarray:
    _require(filename == expected_name and Path(filename).name == filename,
             "RESEARCH_INVALID_PATH_ARTIFACT_NAME")
    path = root / filename
    _require(not path.is_symlink() and path.resolve().parent == root.resolve(),
             "RESEARCH_INVALID_PATH_ARTIFACT_NAME")
    values = np.load(path, allow_pickle=False)
    _require(values.shape == shape and values.dtype.kind == "f" and np.all(np.isfinite(values))
             and digest(values.tolist()) == expected_digest, "RESEARCH_SIMULATION_ARTIFACT_CHANGED")
    return values


def _base_forecast(root, payload, snapshot, returns, config):
    base = payload["base_forecast"]
    forecast_id = base["forecast_id"]
    _require(re.fullmatch(r"[0-9a-f]{64}", forecast_id) is not None
             and digest({k: v for k, v in base.items() if k != "forecast_id"}) == forecast_id,
             "RESEARCH_FORECAST_IDENTITY_INVALID")
    now = snapshot["now"]
    origin = snapshot["fields"]["last_bar_event"] + 60
    _require(base["snapshot_id"] == snapshot["snapshot_id"] and base["created_epoch"] == now
             and base["input_cutoff_epoch"] == now and base["spot"] == snapshot["fields"]["spot"]
             and base["price_origin_epoch"] == origin
             and base["price_origin_basis"] == "LATEST_COMPLETED_BAR_CLOSE"
             and base["price_origin_age_seconds"] == now - origin
             and base["target_epoch"] == origin + config.horizon_minutes * 60 > now
             and base["path_count"] == config.paths and base["horizon_minutes"] == config.horizon_minutes,
             "RESEARCH_FORECAST_TIME_OR_ORIGIN_DISAGREES")
    training = returns[-config.training_returns:]
    _require(len(training) >= 200 and base["training_rows"] == training
             and base["training_digest"] == digest(training), "RESEARCH_TRAINING_INPUT_DISAGREES")
    values = np.asarray([r["ret_1"] for r in training], dtype=float)
    drift = float(values.mean()) * len(values) / (len(values) + 200)
    residual_rows = [{**row, "ret_1": float(value)} for row, value in zip(training, values - drift)]
    _require(base["direction"] == {"name": "SHRUNK_EMPIRICAL_MEAN_V1", "per_minute_log_drift": drift,
                                   "shrinkage_pseudocount": 200}
             and base["residual_digest"] == digest(residual_rows)
             and base["seed"] == int(digest({"seed": config.seed, "snapshot": snapshot["snapshot_id"]})[:8], 16),
             "RESEARCH_DIRECTION_OR_SEED_DISAGREES")
    paths = _array(root, payload["base_path_file"], forecast_id + ".npy",
                   (config.paths, config.horizon_minutes + 1), base["paths_digest"])
    _require(np.all(paths[:, 0] == 0) and np.max(np.abs(paths)) <= 100,
             "RESEARCH_SIMULATION_ARTIFACT_CHANGED")
    _verify_simulation(base, paths, config)
    terminal = paths[:, -1]
    noise = terminal - drift * config.horizon_minutes
    _require(base["p_up"] == float(np.mean(terminal > 0)) and base["baseline_p_up"] == .5
             and base["baseline_probability_basis"] == "EXACT_SYMMETRY_OF_ZERO_DRIFT_RETURN_LAW"
             and base["simulated_baseline_p_up"] == float(np.mean(noise > 0))
             and base["quantiles"] == {str(q): float(np.quantile(terminal, q)) for q in (.05, .5, .95)}
             and base["mean_terminal_price"] == float(base["spot"] * np.exp(terminal).mean())
             and base["mc_standard_error_mean_log_return"] == float(np.std(terminal, ddof=1) / math.sqrt(config.paths)),
             "RESEARCH_PATH_STATISTICS_DISAGREE")
    simulation = base["simulation"]
    _require(simulation == _innovation_law(simulation["nu"]), "RESEARCH_SIMULATION_LAW_DISAGREES")
    variance = base["variance"]
    _require(variance["model_id"] in ("EWMA", "GARCH11_T")
             and (simulation["nu"] is None) == (variance["model_id"] == "EWMA")
             and simulation["nu"] == variance["params"].get("nu")
             and variance["fit_cutoff_epoch"] == now and variance["fit_count"] == 1
             and variance["artifact_digest"] == model_digest({"model_id": variance["model_id"],
                                                               "params": variance["params"]}),
             "RESEARCH_VARIANCE_FAMILY_DISAGREES")
    _require(base["calibration_status"] == "SIMULATED_UNCALIBRATED", "RESEARCH_AUTHORITY_DISAGREES")
    return base, noise, drift * config.horizon_minutes


def _verify_forecast(root, payload, sample, returns, matured, observations, plan, config):
    now = sample["decision_epoch"]
    _require(payload["sample_id"] == sample["sample_id"] and payload["phase"] == sample["phase"]
             and payload["features"] == sample["features"], "RESEARCH_FORECAST_SAMPLE_BINDING_INVALID")
    cutoff = min(now, plan.holdout_start) - plan.embargo_minutes * 60
    _require(payload["training_cutoff_epoch"] == cutoff, "RESEARCH_TRAINING_CUTOFF_DISAGREES")
    base, noise, drift = _base_forecast(root, payload, sample["snapshot"], returns, config)
    training = [label for label in matured if label["label_available_epoch"] <= cutoff
                and label["target_epoch"] <= cutoff][-plan.training_window:]
    shifts = {"ZERO_DRIFT": 0.}
    if len(training) >= plan.minimum_training_labels:
        try:
            expected_ridge = fit_ridge(training, cutoff=cutoff, horizon_minutes=config.horizon_minutes,
                                       alpha=plan.ridge_alpha)
            ridge_shift = predict_ridge(expected_ridge, sample["features"])
        except Refused as exc:
            _require(payload["ridge"] is None and payload["ridge_problem"] == str(exc),
                     "RESEARCH_RIDGE_REFUSAL_DISAGREES")
        else:
            ridge = payload["ridge"]
            _require(isinstance(ridge, dict) and ridge["training_records"] == expected_ridge["training_records"],
                     "RESEARCH_RIDGE_TRAINING_NOT_EARLIER_LABELS")
            _require(ridge == expected_ridge, "RESEARCH_RIDGE_FIT_DISAGREES")
            ridge_terminal = noise + ridge_shift
            if np.all(np.isfinite(ridge_terminal)) and float(np.max(np.abs(ridge_terminal))) <= 100:
                _require(payload["ridge_problem"] is None, "RESEARCH_RIDGE_READINESS_DISAGREES")
                shifts["RIDGE_STATE"] = ridge_shift
            else:
                _require(payload["ridge_problem"] == "RIDGE_SIMULATION_NUMERIC_FAILURE",
                         "RESEARCH_RIDGE_READINESS_DISAGREES")
    else:
        _require(payload["ridge"] is None and payload["ridge_problem"] ==
                 f"INSUFFICIENT_MATURED_LABELS:{len(training)}<{plan.minimum_training_labels}",
                 "RESEARCH_RIDGE_READINESS_DISAGREES")
    shifts["SHRUNK_MEAN"] = drift
    _require(set(payload["variants"]) == set(shifts), "RESEARCH_VARIANTS_DISAGREE")
    quote, problem = quote_at(observations, now, config)
    _require(payload["quote"] == quote and payload["quote_problem"] == problem,
             "RESEARCH_QUOTE_NOT_ASOF_CAPTURE_BOUND")
    terminals = {}
    for name, shift in shifts.items():
        variant = payload["variants"][name]
        terminal = _array(root, variant["terminal_file"], sample["sample_id"] + "-" + name + ".npy",
                          (config.paths,), variant["terminal_digest"])
        _require(variant["total_log_drift"] == shift and np.array_equal(terminal, noise + shift),
                 "RESEARCH_COMMON_NOISE_OR_SHIFT_DISAGREES")
        expected_p = .5 if name == "ZERO_DRIFT" else float(np.mean(terminal > 0))
        _require(variant["p_up"] == expected_p, "RESEARCH_VARIANT_PROBABILITY_DISAGREES")
        candidate = evaluate(base, terminal[:, None], quote, problem, cash=money(config.starting_cash),
                             position_open=False, config=config)
        candidate["authority"] = "NONE_RESEARCH_ONLY"
        _require(variant["candidate"] == candidate, "RESEARCH_CANDIDATE_BEHAVIOR_DISAGREES")
        terminals[name] = terminal
    return terminals


def _verify_research(root: Path) -> dict:
    manifest = json.loads((root / "manifest.json").read_text())
    raw = (root / "input.json").read_bytes()
    _require(manifest["schema"] == "APEX_RESEARCH_MANIFEST_V1"
             and hashlib.sha256(raw).hexdigest() == manifest["input_sha256"], "RESEARCH_CAPTURE_DIGEST_MISMATCH")
    config, plan = Config(**manifest["config"]), ResearchPlan(**manifest["plan"])
    _require(plan.scan_minutes >= config.horizon_minutes, "RESEARCH_OVERLAPPING_TARGETS")
    document = json.loads(raw)
    observations, rejected = normalize(document)
    input_class = ("SYNTHETIC_RESEARCH_CONTROL" if str(document.get("source", "")).startswith("SYNTHETIC_") or
                   (observations and all(row["availability_basis"] == "SYNTHETIC_CLOCK" for row in observations))
                   else "RECORDED_RESEARCH_WITH_DECLARED_AVAILABILITY_LIMITS")
    _require(manifest["input_class"] == input_class and manifest["rejected"] == rejected
             and manifest["availability_bases"] == sorted({row["availability_basis"] for row in observations})
             and manifest["models"] == list(MODELS) and manifest["authorization"] == "RESEARCH_ONLY_NO_BROKER",
             "RESEARCH_MANIFEST_INPUT_ACCOUNTING_DISAGREES")
    code = manifest["code"]
    _require(isinstance(code, dict) and bool(code) and all(isinstance(name, str)
             and not PurePosixPath(name).is_absolute() and ".." not in PurePosixPath(name).parts
             and name.endswith(".py") and isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
             for name, value in code.items()), "RESEARCH_CODE_MANIFEST_INVALID")
    rows = read_verified(root / "ledger.jsonl")
    _require(rows[0]["payload"] == {"manifest_digest": digest(manifest), "config": config.record(),
                                    "input_class": input_class}, "RESEARCH_MANIFEST_NOT_LEDGER_BOUND")
    _require(rows[0]["epoch"] == plan.start and rows[-1]["epoch"] == plan.end
             and rows[-1]["kind"] == "RUN_CLOSE" and not (root / "FAILED.json").exists()
             and (root / "COMPLETE").read_text() == rows[-1]["hash"], "RESEARCH_RUN_LIFECYCLE_INVALID")
    decision_times = {float(t + plan.decision_delay_seconds)
                      for t in np.arange(plan.start, plan.end, plan.scan_minutes * 60)
                      if t + plan.decision_delay_seconds < plan.end}
    times = decision_times | {plan.holdout_start, plan.end}
    pending, matured, scores = {}, [], []
    index, forecasts, samples, selected, last_target = 1, 0, 0, False, None

    def take(kind, now):
        nonlocal index
        _require(index < len(rows) and rows[index]["kind"] == kind and rows[index]["epoch"] == now,
                 "RESEARCH_EVENT_ORDER_OR_COMPLETENESS_INVALID:" + kind)
        payload = rows[index]["payload"]
        index += 1
        return payload

    for now in sorted(times):
        bars, _ = visible(observations, now=now, symbol=config.symbol, kind="bar")
        targets = {bar["event_epoch"] + 60: bar for bar in bars if regular(bar["event_epoch"])}
        for sample_id, item in list(pending.items()):
            target = targets.get(item["target_epoch"])
            if target is None or now < item["target_epoch"]:
                continue
            realized = math.log(target["close"] / item["spot"])
            label = {"sample_id": sample_id, "decision_epoch": item["decision_epoch"],
                     "target_epoch": item["target_epoch"], "label_available_epoch": target["available_epoch"],
                     "price_origin_epoch": item["price_origin_epoch"],
                     "features": item["features"], "realized_log_return": realized}
            _require(take("RESEARCH_LABEL", now) == {**label, "bar_id": target["observation_id"]},
                     "RESEARCH_LABEL_NOT_MATURE_CAPTURE_BOUND")
            matured.append(label)
            # Producer order is fixed independently of the JSON object's key order.
            for name in MODELS:
                if name not in item["terminals"]:
                    continue
                score = {"sample_id": sample_id, "model": name, "session": session(item["decision_epoch"]),
                         "phase": item["phase"], "label_bar_id": target["observation_id"],
                         "realized_log_return": realized,
                         "score": distribution_score(item["terminals"][name], realized, model=name)}
                _require(take("RESEARCH_SCORE", now) == score, "RESEARCH_SCORE_DISAGREES")
                scores.append(score)
            del pending[sample_id]
        if now >= plan.holdout_start and not selected:
            development_ids = {score["sample_id"] for score in scores if score["phase"] == "DEVELOPMENT"}
            eligible_ids = {label["sample_id"] for label in matured
                            if label["sample_id"] in development_ids and label["label_available_epoch"] < plan.holdout_start
                            and label["target_epoch"] < plan.holdout_start}
            eligible_scores = [score for score in scores if score["sample_id"] in eligible_ids]
            expected = {**_selection(eligible_scores, plan), "label_cutoff_epoch": plan.holdout_start,
                        "eligible_sample_ids": sorted(eligible_ids)}
            _require(take("SELECTION_FROZEN", now) == expected, "RESEARCH_SELECTION_NOT_DEVELOPMENT_FROZEN")
            selected = True
        origin = now - plan.decision_delay_seconds
        target_clock = origin + config.horizon_minutes * 60 - 60
        if now not in decision_times or not regular(now) or not regular(target_clock) or session(target_clock) != session(now):
            continue
        phase = _phase(now, plan)
        try:
            snapshot, returns = twin(observations, now, config.symbol)
            if snapshot["fields"]["last_bar_event"] + 60 != origin:
                raise Refused("RESEARCH_DECISION_REQUIRES_CURRENT_COMPLETED_MINUTE")
            x = features(snapshot, returns)
        except Refused as exc:
            _require(take("RESEARCH_REFUSED", now) == {"phase": phase, "reason": str(exc)},
                     "RESEARCH_REFUSAL_DISAGREES")
            continue
        sample_id = digest({"snapshot_id": snapshot["snapshot_id"], "horizon": config.horizon_minutes, "features": x})
        item = {"sample_id": sample_id, "decision_epoch": now, "price_origin_epoch": origin,
                "target_epoch": origin + config.horizon_minutes * 60,
                "spot": snapshot["fields"]["spot"], "features": x, "phase": phase, "variants": {}}
        expected_sample = {**item, "snapshot": snapshot}
        _require(take("RESEARCH_SAMPLE", now) == expected_sample, "RESEARCH_SAMPLE_INPUT_BINDING_INVALID")
        _require(sample_id not in pending and all(label["sample_id"] != sample_id for label in matured)
                 and (last_target is None or origin >= last_target), "RESEARCH_DUPLICATE_OR_OVERLAPPING_SAMPLE")
        last_target = item["target_epoch"]
        pending[sample_id] = {**item, "terminals": {}}
        samples += 1
        if phase == "WARMUP":
            continue
        if index < len(rows) and rows[index]["kind"] == "RESEARCH_REFUSED":
            # Only actual failures of the production forecast justify omitting a
            # scored candidate. A rehashed blanket refusal cannot hide forecasts.
            try:
                base, paths = predict(returns, snapshot, config)
                noise = paths[:, -1] - base["direction"]["per_minute_log_drift"] * config.horizon_minutes
                if not np.all(np.isfinite(noise)) or float(np.max(np.abs(noise))) > 100:
                    raise Refused("BASELINE_SIMULATION_NUMERIC_FAILURE")
            except Refused as exc:
                _require(take("RESEARCH_REFUSED", now) == {"phase": phase, "reason": str(exc)},
                         "RESEARCH_REFUSAL_DISAGREES")
                continue
            raise Refused("RESEARCH_UNJUSTIFIED_FORECAST_REFUSAL")
        payload = take("RESEARCH_FORECAST", now)
        pending[sample_id]["terminals"] = _verify_forecast(root, payload, expected_sample, returns, matured,
                                                          observations, plan, config)
        forecasts += 1
    _require(take("RUN_CLOSE", plan.end) == {"pending_sample_ids": sorted(pending)} and index == len(rows),
             "RESEARCH_RUN_CLOSE_ACCOUNTING_DISAGREES")
    _require(json.loads((root / "summary.json").read_text()) == summarize(rows, plan),
             "RESEARCH_SAVED_SUMMARY_DISAGREES")
    return {"status": "VALID", "verified_samples": samples, "verified_labels": len(matured),
            "verified_forecasts": forecasts, "verified_scores": len(scores),
            "capture_and_path_bindings": "VERIFIED", "selection_and_summary_bindings": "VERIFIED",
            "code_manifest_matches_current": code == code_manifest(),
            "scope": "Internal causal consistency using current feature, decision, scoring and ridge readers; "
                     "not authenticated provider history or executed source code, independently rerun variance "
                     "optimization, calibrated forecasting, or trading authority"}


def verify_research(root: Path) -> dict:
    """Validate a completed run without altering its retained artifacts."""
    try:
        return _verify_research(Path(root))
    except Refused:
        raise
    except (KeyError, TypeError, ValueError, OSError, OverflowError, AttributeError) as exc:
        raise Refused("RESEARCH_ARTIFACT_INVALID:" + type(exc).__name__) from exc
