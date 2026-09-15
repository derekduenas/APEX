"""Reconstruct causal inputs, decisions, accounting and outcomes from one run.

This verifies retained artifacts with the production feature/decision readers.
It does not independently authenticate a provider or establish model accuracy.
"""
import hashlib
import json
import math
from collections import Counter
from decimal import Decimal
from pathlib import Path

import numpy as np

from .core import Config, Refused, digest, fee, money
from .data import normalize, session, twin, visible
from .decision import evaluate, quote_at
from .forecast import _draw_innovations, _innovation_law
from .ledger import read_verified, reconstruct
from .reused.contracts import ModelRefused
from .reused.vol_models import EWMA, GARCH


def _verify_simulation(prediction, paths, config):
    """Consume the persisted variance model, without repeating its optimizer fit."""
    p = prediction
    values = np.asarray([r["ret_1"] for r in p["training_rows"]])
    drift = float(values.mean()) * len(values) / (len(values) + 200)
    residuals = [{**r, "ret_1": float(e)} for r, e in zip(p["training_rows"], values - drift)]
    if (p["direction"] != {"name": "SHRUNK_EMPIRICAL_MEAN_V1", "per_minute_log_drift": drift, "shrinkage_pseudocount": 200}
            or p["residual_digest"] != digest(residuals)
            or p["seed"] != int(digest({"seed": config.seed, "snapshot": p["snapshot_id"]})[:8], 16)):
        raise Refused("FORECAST_MODEL_INPUT_DISAGREES")
    artifact = p["variance"]
    model_class = {"EWMA": EWMA, "GARCH11_T": GARCH}.get(artifact["model_id"])
    if (model_class is None or artifact["fit_cutoff_epoch"] != p["input_cutoff_epoch"]
            or artifact["fit_count"] != 1 or (config.variance == "ewma" and model_class is not EWMA)):
        raise Refused("VARIANCE_MODEL_CONTRACT_INVALID")
    attempts = p["fit_attempts"]
    if config.variance == "garch" and model_class is EWMA:
        if (len(attempts) != 2 or attempts[0].get("model") != "GARCH11_T" or attempts[0].get("status") != "REFUSED"
                or attempts[0].get("fit_calls") not in (0, 1) or not attempts[0].get("reason")
                or attempts[1] != {"model": "EWMA", "status": "FITTED_FALLBACK", "fit_calls": 1}):
            raise Refused("MODEL_ATTEMPT_ACCOUNTING_DISAGREES")
    elif attempts != [{"model": artifact["model_id"], "status": "FITTED", "fit_calls": 1}]:
        raise Refused("MODEL_ATTEMPT_ACCOUNTING_DISAGREES")
    try:
        model = model_class.load(artifact)
    except (ModelRefused, ValueError, KeyError, ArithmeticError) as exc:
        raise Refused("VARIANCE_ARTIFACT_INVALID") from exc
    law = _innovation_law(model.p["nu"] if isinstance(model, GARCH) else None)
    if p["simulation"] != law or (isinstance(model, GARCH) and model.p["gamma"] != 0):
        raise Refused("SIMULATION_LAW_DISAGREES")
    rng = np.random.default_rng(p["seed"])
    count, horizon = p["path_count"], p["horizon_minutes"]
    h = np.full(count, model.next_h(model.p["h_last"], model.p["e_last"]) if isinstance(model, GARCH) else model.h)
    expected = np.zeros_like(paths)
    for step in range(horizon):
        residual = np.sqrt(h) * _draw_innovations(rng, law, count)
        expected[:, step + 1] = expected[:, step] + (drift + residual)
        if isinstance(model, GARCH):
            params = model.p
            h = params["omega"] + (params["alpha"] + params["gamma"] * (residual < 0)) * residual * residual + params["beta"] * h
        else:
            h = model.lam * h + (1 - model.lam) * residual * residual
    # No optimizer runs here: the persisted model must actually explain paths.
    if not np.allclose(paths, expected, rtol=0, atol=1e-14):
        raise Refused("PERSISTED_MODEL_PATHS_DISAGREE")
    terminal = paths[:, -1]
    if (p["mean_terminal_price"] != float(p["spot"] * np.exp(terminal).mean())
            or p["mc_standard_error_mean_log_return"] != float(np.std(terminal, ddof=1) / math.sqrt(count))):
        raise Refused("PATH_MOMENTS_DISAGREE")


def verify_run(root: Path) -> dict:
    manifest = json.loads((root / "manifest.json").read_text())
    if (manifest.get("schema") != "APEX_RUN_V1" or manifest.get("mode") != "OFFLINE_EXPERIMENTAL_REPLAY"
            or manifest.get("authority") != "NO_BROKER_OR_PRODUCTION_CAPITAL_AUTHORITY"):
        raise Refused("RUN_AUTHORITY_CONTRACT_INVALID")
    captured = (root / "input.json").read_bytes()
    if hashlib.sha256(captured).hexdigest() != manifest["input_sha256"]:
        raise Refused("CAPTURE_DIGEST_MISMATCH")
    rows = read_verified(root / "ledger.jsonl")
    if rows[0]["payload"]["manifest_digest"] != digest(manifest):
        raise Refused("MANIFEST_NOT_LEDGER_BOUND")
    if rows[0]["payload"]["config"] != manifest["config"]:
        raise Refused("CONFIG_DISAGREES")
    config = Config(**manifest["config"])
    if (not 0 < manifest["end"] - manifest["start"] <= 86400
            or session(manifest["start"]) != session(manifest["end"])):
        raise Refused("RUN_RANGE_INVALID")
    observations, rejected = normalize(json.loads(captured))
    by_id = {o["observation_id"]: o for o in observations}
    if manifest["rejected"] != rejected or manifest["availability_bases"] != sorted({r["availability_basis"] for r in observations}):
        raise Refused("MANIFEST_INPUT_ACCOUNTING_DISAGREES")
    if rows[-1]["kind"] != "RUN_CLOSE" or (root / "COMPLETE").read_text() != rows[-1]["hash"]:
        raise Refused("INCOMPLETE_RUN_OR_CHANGED_HEAD")
    if ((root / "FAILED.json").exists() or rows[0]["epoch"] != manifest["start"] or rows[-1]["epoch"] != manifest["end"]
            or sum(r["kind"] == "RUN_OPEN" for r in rows) != 1 or sum(r["kind"] == "RUN_CLOSE" for r in rows) != 1
            or any(not manifest["start"] <= r["epoch"] <= manifest["end"] for r in rows)):
        raise Refused("RUN_LIFECYCLE_INVALID")
    result = reconstruct(root / "ledger.jsonl")
    if result["status"] != "VALID":
        raise Refused("ACCOUNTING_RECONSTRUCTION_FAILED:" + ";".join(result["problems"]))

    seen, snapshots, forecasts, path_sets = {}, {}, {}, {}
    scores, scored, candidates, positions, unfilled = [], set(), set(), {}, set()
    all_entries, all_exits, score_evidence, expiry_evidence = {}, {}, {}, {}
    cash = money(config.starting_cash)
    scan_epochs = set(np.arange(manifest["start"], manifest["end"] - config.horizon_minutes * 60 + .001,
                               config.scan_minutes * 60).tolist())
    event_epochs = {manifest["start"], manifest["end"], *scan_epochs}
    event_epochs.update(r["available_epoch"] for r in observations if manifest["start"] <= r["available_epoch"] <= manifest["end"])
    for scan in scan_epochs:
        event_epochs.add(scan + config.horizon_minutes * 60)
        deadline = scan + config.horizon_minutes * 60 + config.exit_window_seconds
        if deadline <= manifest["end"]:
            event_epochs.add(deadline)
    scanned, forecast_epochs, refused_epochs, receipts, expired = set(), set(), set(), set(), set()
    decisions, variances = Counter(), Counter()
    for row in rows:
        p, kind, now = row["payload"], row["kind"], row["epoch"]
        if kind not in {"RUN_OPEN", "RUN_CLOSE", "QUOTE", "TWIN", "SCAN_REFUSED", "FORECAST", "LAYER_RECEIPT",
                        "CANDIDATE", "ENTRY", "EXIT", "FORECAST_SCORE", "EXIT_WAIT", "EXIT_WINDOW_CLOSED"}:
            raise Refused("UNKNOWN_REPLAY_EVENT")
        if kind == "QUOTE":
            if by_id.get(p.get("observation_id")) != p or p["kind"] != "quote" or p["available_epoch"] > now:
                raise Refused("QUOTE_NOT_CAPTURE_BOUND")
        elif kind == "TWIN":
            if now not in scan_epochs or now in scanned:
                raise Refused("TWIN_SCAN_INVALID")
            expected, returns = twin(observations, now, config.symbol)
            if p != expected:
                raise Refused("TWIN_INPUT_BINDING_INVALID")
            snapshots[p["snapshot_id"]] = (row, returns)
            scanned.add(now)
        elif kind == "SCAN_REFUSED":
            if now not in scan_epochs or now in forecast_epochs or now in refused_epochs:
                raise Refused("REFUSED_SCAN_INVALID")
            scanned.add(now)
            refused_epochs.add(now)
        elif kind == "FORECAST":
            identity = {k: v for k, v in p.items() if k not in ("forecast_id", "path_file")}
            if digest(identity) != p["forecast_id"] or digest(p["training_rows"]) != p["training_digest"]:
                raise Refused("FORECAST_IDENTITY_INVALID")
            snapshot, returns = snapshots.get(p["snapshot_id"], (None, None))
            if snapshot is None or snapshot["epoch"] != now or p["forecast_id"] in forecasts or now in forecast_epochs or now in refused_epochs:
                raise Refused("FORECAST_SNAPSHOT_BINDING_INVALID")
            origin = snapshot["payload"]["fields"]["last_bar_event"] + 60
            if (p["created_epoch"] != now or p["input_cutoff_epoch"] != now or p["spot"] != snapshot["payload"]["fields"]["spot"]
                    or p["path_count"] != config.paths or p["horizon_minutes"] != config.horizon_minutes
                    or p["price_origin_epoch"] != origin or p["price_origin_age_seconds"] != now - origin
                    or p["price_origin_basis"] != "LATEST_COMPLETED_BAR_CLOSE"
                    or p["target_epoch"] != origin + config.horizon_minutes * 60 or p["target_epoch"] <= now):
                raise Refused("FORECAST_TIME_OR_ORIGIN_DISAGREES")
            if p["training_rows"] != returns[-config.training_returns:]:
                raise Refused("TRAINING_INPUT_DISAGREES")
            if p["path_file"] != p["forecast_id"] + ".npy":
                raise Refused("INVALID_PATH_ARTIFACT_NAME")
            paths = np.load(root / p["path_file"], allow_pickle=False)
            if (paths.shape != (p["path_count"], p["horizon_minutes"] + 1) or not np.all(np.isfinite(paths))
                    or np.any(paths[:, 0] != 0) or digest(paths.tolist()) != p["paths_digest"]):
                raise Refused("SIMULATION_ARTIFACT_CHANGED")
            terminal = paths[:, -1]
            baseline = terminal - p["direction"]["per_minute_log_drift"] * p["horizon_minutes"]
            if (p["p_up"] != float((terminal > 0).mean()) or p["baseline_p_up"] != .5
                    or p["baseline_probability_basis"] != "EXACT_SYMMETRY_OF_ZERO_DRIFT_RETURN_LAW"
                    or p["simulated_baseline_p_up"] != float((baseline > 0).mean())):
                raise Refused("PATH_PROBABILITY_DISAGREES")
            if set(p["quantiles"]) != {"0.05", "0.5", "0.95"}:
                raise Refused("PATH_QUANTILE_DISAGREES")
            for q, value in p["quantiles"].items():
                if value != float(np.quantile(terminal, float(q))):
                    raise Refused("PATH_QUANTILE_DISAGREES")
            _verify_simulation(p, paths, config)
            forecasts[p["forecast_id"]], path_sets[p["forecast_id"]] = p, paths
            forecast_epochs.add(now)
            variances[p["variance"]["model_id"]] += 1
        elif kind == "LAYER_RECEIPT":
            source, output = seen.get(p.get("input_ref")), seen.get(p.get("output_ref"))
            if (p.get("layer") != "forecast" or not source or source["kind"] != "TWIN" or not output
                    or output["kind"] != "FORECAST" or source["epoch"] != now or output["epoch"] != now
                    or source["payload"]["snapshot_id"] != output["payload"]["snapshot_id"] or p["output_ref"] in receipts):
                raise Refused("LAYER_RECEIPT_DISCONNECTED")
            receipts.add(p["output_ref"])
        elif kind == "CANDIDATE":
            forecast, snapshot = seen.get(p.get("forecast_ref")), seen.get(p.get("snapshot_ref"))
            if (not forecast or forecast["kind"] != "FORECAST" or not snapshot or snapshot["kind"] != "TWIN"
                    or forecast["epoch"] != now or snapshot["epoch"] != now
                    or forecast["payload"]["snapshot_id"] != snapshot["payload"]["snapshot_id"]
                    or p["forecast_ref"] in candidates):
                raise Refused("CANDIDATE_MISSING_INPUT")
            quote, problem = quote_at(observations, now, config)
            quote_row = seen.get(p.get("quote_ref"))
            if ((quote is None and p.get("quote_ref") is not None) or (quote is not None
                    and (not quote_row or quote_row["kind"] != "QUOTE" or quote_row["payload"] != quote or quote_row["epoch"] != now))):
                raise Refused("CANDIDATE_QUOTE_BINDING_INVALID")
            prediction = forecast["payload"]
            expected = evaluate(prediction, path_sets[prediction["forecast_id"]], quote, problem,
                                cash=cash, position_open=bool(positions), config=config)
            body = {k: v for k, v in p.items() if k not in ("forecast_ref", "snapshot_ref", "quote_ref")}
            if body != expected:
                raise Refused("CANDIDATE_BEHAVIOR_DISAGREES")
            candidates.add(p["forecast_ref"])
            decisions[p["reason"] or p["decision"]] += 1
            if p["decision"] == "EXPERIMENTAL_LONG":
                unfilled.add(row["hash"])
        elif kind == "ENTRY":
            quote = seen[p["quote_ref"]]["payload"]
            cash -= money(Decimal(str(quote["ask"])) * p["quantity"]) + fee(p["quantity"], config)
            positions[row["hash"]] = row
            all_entries[row["hash"]] = row
            unfilled.remove(p["candidate_ref"])
        elif kind == "EXIT":
            quote = seen[p["quote_ref"]]["payload"]
            selected_quote, _ = quote_at(observations, now, config)
            if selected_quote != quote:
                raise Refused("EXIT_QUOTE_SELECTION_DISAGREES")
            cash += money(Decimal(str(quote["bid"])) * p["quantity"]) - fee(p["quantity"], config)
            del positions[p["entry_ref"]]
            all_exits[p["entry_ref"]] = row
        elif kind in ("EXIT_WAIT", "EXIT_WINDOW_CLOSED"):
            position = positions.get(p.get("entry_ref"))
            if position is None:
                raise Refused("EXIT_OBLIGATION_DISCONNECTED")
            due = position["epoch"] + config.horizon_minutes * 60
            if kind == "EXIT_WINDOW_CLOSED":
                if now < due + config.exit_window_seconds or p.get("exposure_retained") is not True or p["entry_ref"] in expired:
                    raise Refused("EXIT_WINDOW_DISAGREES")
                expired.add(p["entry_ref"])
                expiry_evidence[p["entry_ref"]] = now
            elif not due <= now <= due + config.exit_window_seconds:
                raise Refused("EXIT_WAIT_OUTSIDE_WINDOW")
        elif kind == "FORECAST_SCORE":
            f, bar = forecasts.get(p.get("forecast_id")), by_id.get(p.get("bar_id"))
            if f is None or bar is None or p["forecast_id"] in scored:
                raise Refused("SCORE_FORECAST_OR_LABEL_INVALID")
            bars, _ = visible(observations, now=now, symbol=config.symbol, kind="bar")
            selected = next((b for b in bars if b["event_epoch"] + 60 == f["target_epoch"]), None)
            if (selected != bar or bar["available_epoch"] > now or p["target_epoch"] != f["target_epoch"]
                    or p["label_available_epoch"] != bar["available_epoch"] or now < f["target_epoch"]):
                raise Refused("LABEL_NOT_MATURE")
            realized = float(np.log(bar["close"] / f["spot"]))
            if not np.isclose(p["realized_log_return"], realized, rtol=0, atol=1e-14):
                raise Refused("LABEL_VALUE_DISAGREES")
            outcome = int(realized > 0)
            if (p["brier"] != (f["p_up"] - outcome) ** 2 or p["baseline_brier"] != (f["baseline_p_up"] - outcome) ** 2
                    or p["interval_covered"] != (f["quantiles"]["0.05"] <= realized <= f["quantiles"]["0.95"])):
                raise Refused("SCORE_DISAGREES")
            scored.add(p["forecast_id"])
            score_evidence[p["forecast_id"]] = (now, p["bar_id"])
            scores.append(p)
        seen[row["hash"]] = row
    close = rows[-1]["payload"]
    if unfilled:
        raise Refused("CANDIDATE_EXECUTION_MISSING")
    # A missing row is also a behavioral disagreement. Retained quotes require
    # the earliest eligible exit; retained labels require the first mature score.
    for entry_ref, entry in all_entries.items():
        due = entry["epoch"] + config.horizon_minutes * 60
        deadline = due + config.exit_window_seconds
        expected_exit = None
        for epoch in sorted(t for t in event_epochs if due <= t <= deadline):
            quote, _ = quote_at(observations, epoch, config)
            if quote and quote["bid_size"] >= entry["payload"]["quantity"]:
                expected_exit = (epoch, quote["observation_id"])
                break
        actual_exit = all_exits.get(entry_ref)
        actual_evidence = None if actual_exit is None else (
            actual_exit["epoch"], seen[actual_exit["payload"]["quote_ref"]]["payload"]["observation_id"])
        if actual_evidence != expected_exit:
            raise Refused("EXIT_OBLIGATION_NOT_SERVICED")
        if expected_exit is None and deadline <= manifest["end"] and expiry_evidence.get(entry_ref) != deadline:
            raise Refused("EXIT_WINDOW_EVIDENCE_MISSING")
    expected_scores = {}
    for forecast_id, forecast in forecasts.items():
        targets = [o for o in observations if o["kind"] == "bar" and o["symbol"] == config.symbol
                   and o["event_epoch"] + 60 == forecast["target_epoch"] and o["available_epoch"] <= manifest["end"]]
        if targets:
            first = min(o["available_epoch"] for o in targets)
            available, _ = visible(targets, now=first, symbol=config.symbol, kind="bar")
            if available:
                expected_scores[forecast_id] = (first, available[0]["observation_id"])
    if score_evidence != expected_scores:
        raise Refused("MATURED_SCORE_EVIDENCE_DISAGREES")
    counts = {"scans": len(scan_epochs), "forecasts": len(forecasts), "entries": sum(r["kind"] == "ENTRY" for r in rows),
              "exits": sum(r["kind"] == "EXIT" for r in rows), "scored_forecasts": len(scores)}
    if (scanned != scan_epochs or forecast_epochs | refused_epochs != scan_epochs
            or close["counts"] != counts or len(candidates) != len(forecasts) or len(receipts) != len(forecasts)
            or close["pending_forecast_ids"] != sorted(set(forecasts) - scored)
            or close["status"] != ("CLOSED_WITH_OUTSTANDING_OBLIGATIONS" if positions else "CLOSED")):
        raise Refused("RUN_CLOSE_ACCOUNTING_DISAGREES")
    saved = json.loads((root / "summary.json").read_text())
    if (saved.get("mode") != manifest["mode"]
            or saved.get("readiness") != "REPLAY_PROTOTYPE; NOT_LIVE_PAPER_OR_REAL_MONEY_READY"):
        raise Refused("SAVED_SCOPE_DISAGREES")
    if saved["accounting"] != result:
        raise Refused("SAVED_ACCOUNTING_DISAGREES")
    if saved["counts"] != counts or saved["decisions"] != dict(decisions) or saved["variance_models"] != dict(variances):
        raise Refused("SAVED_COUNTS_DISAGREE")
    feedback = {"count": len(scores), "mean_brier": float(np.mean([s["brier"] for s in scores])) if scores else None,
                "baseline_mean_brier": float(np.mean([s["baseline_brier"] for s in scores])) if scores else None,
                "status": "DESCRIPTIVE_SCORES_NOT_CALIBRATION_OR_PROMOTION"}
    if any(saved["forecast_feedback"].get(k) != v for k, v in feedback.items()):
        raise Refused("SAVED_FORECAST_FEEDBACK_DISAGREES")
    return {**result, "verified_forecasts": len(forecasts), "capture_and_path_bindings": "VERIFIED",
            "decision_and_feedback_bindings": "VERIFIED",
            "scope": "Internal causal/accounting consistency using production feature and decision readers; not authenticated source history or calibrated forecasting"}
