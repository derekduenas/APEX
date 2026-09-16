"""One frozen causal peer-lag hypothesis, evaluated against paired baselines.

This consumes a fully verified strategy laboratory. Its output is a separate,
exclusive research run. No orders, fills, calibrated probabilities, statistical
significance, portfolio P&L, or model promotion are produced.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path

import numpy as np

from .core import Config, Refused, canonical, code_manifest, digest, finite
from .data import normalize, session, visible
from .ledger import Ledger, read_complete
from .peer_twin import peer_state
from .research import ResearchPlan, distribution_score
from .research_models import FEATURE_NAMES, fit_ridge, predict_ridge
from .strategy_lab import _compact_evaluation, _cost_stress, _path_episode
from .strategy_lab_verification import verify_lab
from .strategy_simulation import SimulationCosts, evaluate_strategies

MODELS = ("ZERO_DRIFT", "SHRUNK_MEAN", "OWN_STOCK_RIDGE", "PEER_LAG_RIDGE")
CONTRACT = {
    "hypothesis_id": "POSITIVE_PEER_SHOCK_DELAYED_REPRICING_V1",
    "feature": "six_own_stock_features_plus_lag_gap", "challenger": "PEER_LAG_RIDGE",
    "ablation": "OWN_STOCK_RIDGE uses the same admitted rows, cutoff, scaler policy and alpha; removes only lag_gap",
    "economic_comparison": "All policies share observed peer setup eligibility; isolates lag_gap forecast addition conditional on that setup, not the total contribution of peer information",
    "cost_stress_role": "Sensitivity only; 3x stress does not gate or claim robust admission",
    "baseline_models": ["ZERO_DRIFT", "SHRUNK_MEAN", "OWN_STOCK_RIDGE"],
    "feature_policy": "Frozen stock six plus lag_gap; training-only population standardization, unpenalized intercept",
    "training_policy": "All valid peer states with exact matured complete target paths, including non-setups",
    "freeze_policy": "At holdout start, keep labels and targets at or before holdout minus declared embargo",
    "entry_policy": "Fixed HOLD_LONG or WAIT at the first one-minute grid strictly after decision; fixed absolute target",
    "candidate_policy": "Observed eligible peer setup AND available fitted model AND modeled after-cost mean greater than zero",
    "paths": "Source GARCH/EWMA innovations shared across models; linear full-path drift overlay",
    "cost_stress_multiplier": 3., "tuning": "NONE", "promotion": "NONE",
    "peer_training_minutes": 120, "peer_shock_minutes": 5,
}
SCOPE = ("Persisted consistency: fully verify original laboratory, then reproduce every peer state, "
         "training cutoff, fit, common path, strategy evaluation, matured outcome and summary. "
         "Shared production calculations are not independent mathematical validation. Hashes do not "
         "authenticate market history or prior registration. No edge, significance or execution authority established.")


def _require(condition, reason):
    if not condition:
        raise Refused(reason)


def _same(left, right):
    return canonical(left) == canonical(right)


def _path_hash(paths):
    return hashlib.sha256(np.ascontiguousarray(paths, dtype="<f8").tobytes()).hexdigest()


def _fit(matured, cutoff, plan, horizon):
    train = [m for m in matured if m["peer_feature"] is not None
             and m["target_epoch"] <= cutoff and m["label_available_epoch"] <= cutoff][-plan.training_window:]
    records = [{k: m[k] for k in ("sample_id", "decision_epoch", "price_origin_epoch", "target_epoch", "label_available_epoch", "features", "peer_feature", "realized_log_return")}
               for m in train]
    names = list(FEATURE_NAMES) + ["lag_gap"]
    result = {"model": "PEER_LAG_RIDGE", "feature_names": names, "cutoff_epoch": cutoff,
              "alpha": plan.ridge_alpha, "training_records": records,
              "training_sample_ids": [m["sample_id"] for m in train], "training_count": len(train),
              "means": None, "scales": None, "intercept": None, "coefficients": None,
              "own_stock_model": None, "problem": None}
    if len(train) < plan.minimum_training_labels:
        result["problem"] = "INSUFFICIENT_MATURED_PEER_LABELS"
        return result
    try:
        own = fit_ridge(train, cutoff=cutoff, horizon_minutes=horizon, alpha=plan.ridge_alpha)
        x = np.asarray([[m["features"][k] for k in FEATURE_NAMES] + [m["peer_feature"]] for m in train], dtype=float)
        y = np.asarray([m["realized_log_return"] for m in train], dtype=float)
        with np.errstate(over="raise", invalid="raise", divide="raise"):
            means, scales, intercept = x.mean(axis=0), x.std(axis=0), float(y.mean())
            scales[scales == 0] = 1.  # Constant observed feature, never missing-data imputation.
            z = (x - means) / scales
            coefficients = np.linalg.solve(z.T @ z + plan.ridge_alpha * np.eye(len(names)), z.T @ (y - intercept))
        if not all(np.all(np.isfinite(v)) for v in (means, scales, intercept, coefficients)):
            raise Refused("NONFINITE_PEER_FIT")
        result.update(means=means.tolist(), scales=scales.tolist(), intercept=intercept,
                      coefficients=coefficients.tolist(), own_stock_model=own)
    except (Refused, FloatingPointError, np.linalg.LinAlgError) as exc:
        result["problem"] = "PEER_FIT_REFUSED:" + str(exc)
    return result


def _evaluate(paths, item, config, costs, quantity=None):
    return evaluate_strategies(paths, spot=item["spot"], capital=float(config.starting_cash),
        max_notional=float(config.max_notional), eligible_setups={"HOLD_LONG": True, "WAIT": True},
        costs=costs, fixed_quantity=quantity, earliest_entry_step=item["earliest_entry_step"])


def peer_events(source_rows, observations, *, plan, config, costs, market_symbol, sector_symbol, load_paths):
    """Deterministic writer and full verifier reader; forecast before outcome."""
    grouped = defaultdict(list)
    for row in source_rows:
        grouped[row["epoch"]].append(row)
    pending, samples, matured = {}, {}, []
    frozen = None
    for now, source_group in sorted(grouped.items()):
        for sample_id, item in list(pending.items()):
            if item["target_epoch"] > now:
                continue
            episode = _path_episode(item, observations, now, config.symbol, config.horizon_minutes)
            if episode is None:
                continue
            realized = float(episode["path_log_returns"][-1])
            label = {**episode, "peer_feature": item["peer_feature"], "realized_log_return": realized}
            matured.append(label)
            evaluation = _evaluate(np.asarray([episode["path_log_returns"]]), item, config, costs, item.get("fixed_quantity"))
            quantity = evaluation["strategies"]["HOLD_LONG"]["fixed_quantity"]
            stress = _evaluate(np.asarray([episode["path_log_returns"]]), item, config, _cost_stress(costs, 3.), quantity)
            modeled = item.get("modeled", {})
            scores = {name: distribution_score(values["terminal"], realized, model=name) for name, values in modeled.items()}
            decisions = {name: item.get("decisions", {}).get(name, "WAIT") for name in MODELS}
            net = evaluation["strategies"]["HOLD_LONG"]["summary"]["expected_net"]
            stressed_net = stress["strategies"]["HOLD_LONG"]["summary"]["expected_net"]
            payload = {"sample_id": sample_id, "phase": item["phase"], "session": session(item["decision_epoch"]),
                "decision_epoch": item["decision_epoch"], "price_origin_epoch": item["price_origin_epoch"],
                "target_epoch": item["target_epoch"], "label_available_epoch": episode["label_available_epoch"],
                "bar_ids": episode["bar_ids"], "realized_log_return": realized,
                "peer_feature": item["peer_feature"], "eligible_setup": item["eligible_setup"],
                "scores": scores, "paired": set(scores) == set(MODELS), "decisions": decisions,
                "hold_long_net": net, "hold_long_net_cost_3x": stressed_net, "wait_net": 0.,
                "policy_net": {name: net if decision == "HOLD_LONG" else 0. for name, decision in decisions.items()},
                "policy_net_cost_3x": {name: stressed_net if decision == "HOLD_LONG" else 0. for name, decision in decisions.items()},
                "execution_reconstruction": _compact_evaluation(evaluation),
                "cost_3x_execution_reconstruction": _compact_evaluation(stress),
                "basis": "INDEPENDENT_COUNTERFACTUAL_MINUTE_PATH_OPPORTUNITY_NOT_A_FILL_OR_PORTFOLIO_RETURN"}
            yield "PEER_OUTCOME", now, payload
            del pending[sample_id]
        if now >= plan.holdout_start and frozen is None:
            frozen = _fit(matured, plan.holdout_start - plan.embargo_minutes * 60, plan, config.horizon_minutes)
            yield "PEER_MODEL_FROZEN", now, {"fit": frozen, "authority": "NONE_RESEARCH_ONLY"}
        for row in source_group:
            payload, kind = row["payload"], row["kind"]
            if kind == "RESEARCH_REFUSED":
                yield "PEER_SOURCE_REFUSAL", now, {"source_row_hash": row["hash"], **payload}
            elif kind == "RESEARCH_SAMPLE":
                bars, _ = visible(observations, now=now, symbol=config.symbol, kind="bar")
                origin = next(b for b in bars if b["event_epoch"] + 60 == payload["price_origin_epoch"])
                state, problem = None, None
                try:
                    state = peer_state(observations, payload["snapshot"], market_symbol=market_symbol, sector_symbol=sector_symbol,
                        training_minutes=120, shock_minutes=5)
                    _require(finite(state["features"]["lag_gap"]) and type(state["eligible_setup"]) is bool,
                             "PEER_STATE_CONTRACT_INVALID")
                except Refused as exc:
                    problem = str(exc)
                item = {k: payload[k] for k in ("sample_id", "phase", "decision_epoch", "price_origin_epoch", "target_epoch", "spot", "features")}
                item.update(origin_bar_id=origin["observation_id"], regime_name="PEER_EXPERIMENT",
                    peer_state=state, peer_problem=problem,
                    peer_feature=state["features"]["lag_gap"] if state else None,
                    eligible_setup=state["eligible_setup"] if state else False,
                    earliest_entry_step=math.floor((now - payload["price_origin_epoch"]) / 60) + 1)
                samples[item["sample_id"]] = item
                pending[item["sample_id"]] = item
                yield "PEER_SAMPLE", now, {"sample_id": item["sample_id"], "phase": item["phase"],
                    "source_sample_hash": row["hash"], "peer_state": state, "peer_problem": problem,
                    "peer_feature": item["peer_feature"], "eligible_setup": item["eligible_setup"],
                    "earliest_entry_step": item["earliest_entry_step"]}
                if problem or not item["eligible_setup"]:
                    yield "PEER_REFUSAL", now, {"sample_id": item["sample_id"], "phase": item["phase"],
                        "category": "PEER_DATA_UNAVAILABLE" if problem else "NO_ELIGIBLE_PEER_SETUP",
                        "reason": problem or "NO_ELIGIBLE_PEER_SETUP"}
            elif kind == "RESEARCH_FORECAST":
                item = samples[payload["sample_id"]]
                cutoff = min(now, plan.holdout_start) - plan.embargo_minutes * 60
                fit = frozen if item["phase"] == "HOLDOUT" else _fit(matured, cutoff, plan, config.horizon_minutes)
                _require(fit is not None, "PEER_HOLDOUT_MODEL_NOT_FROZEN")
                base_paths = np.asarray(load_paths(payload["base_path_file"]), dtype=float)
                drift = payload["base_forecast"]["direction"]["per_minute_log_drift"] * config.horizon_minutes
                grid = np.arange(config.horizon_minutes + 1) / config.horizon_minutes
                noise = base_paths - grid * drift
                shifts = {name: payload["variants"][name]["total_log_drift"] for name in MODELS[:2]}
                problem = item["peer_problem"] or fit["problem"]
                if problem is None:
                    values = np.asarray([item["features"][k] for k in FEATURE_NAMES] + [item["peer_feature"]])
                    shift = float(fit["intercept"] + ((values - fit["means"]) / fit["scales"]) @ fit["coefficients"])
                    own_shift = predict_ridge(fit["own_stock_model"], item["features"])
                    if not all(finite(v) and abs(v) <= 100 for v in (shift, own_shift)):
                        problem = "NONFINITE_OR_EXTREME_PEER_DRIFT"
                    else:
                        shifts["OWN_STOCK_RIDGE"] = own_shift
                        shifts["PEER_LAG_RIDGE"] = float(shift)
                worlds, decisions, modeled = {}, {}, {}
                quantity = None
                for name, shift in shifts.items():
                    paths = noise + grid * shift
                    result = _evaluate(paths, item, config, costs, quantity)
                    if quantity is None:
                        quantity = result["strategies"]["HOLD_LONG"]["fixed_quantity"]
                    stressed = _evaluate(paths, item, config, _cost_stress(costs, 3.), quantity)
                    profitable = result["strategies"]["HOLD_LONG"]["summary"]["expected_net"] > 0
                    decisions[name] = "HOLD_LONG" if item["eligible_setup"] and profitable else "WAIT"
                    modeled[name] = {"terminal": paths[:, -1]}
                    worlds[name] = {"total_log_drift": shift, "full_paths_sha256": _path_hash(paths),
                        "terminal_digest": digest(paths[:, -1].tolist()), "evaluation": _compact_evaluation(result),
                        "cost_3x_evaluation": _compact_evaluation(stressed), "decision": decisions[name],
                        "decision_reason": "ELIGIBLE_SETUP_POSITIVE_MODELED_AFTER_COST_MEAN" if decisions[name] == "HOLD_LONG"
                            else "NO_ELIGIBLE_PEER_SETUP" if not item["eligible_setup"] else "NONPOSITIVE_MODELED_AFTER_COST_MEAN"}
                    if name in MODELS[:2]:
                        _require(worlds[name]["terminal_digest"] == payload["variants"][name]["terminal_digest"],
                                 "PEER_BASELINE_NO_LONGER_EQUALS_SOURCE:" + name)
                item.update(modeled=modeled, decisions=decisions, fixed_quantity=quantity)
                candidate = decisions.get("PEER_LAG_RIDGE", "WAIT")
                record = {"sample_id": item["sample_id"], "phase": item["phase"], "source_forecast_hash": row["hash"],
                    "source_base_paths_file": payload["base_path_file"], "source_base_paths_sha256": _path_hash(base_paths),
                    "shared_noise_sha256": _path_hash(noise), "path_shape": list(noise.shape),
                    "training_cutoff_epoch": cutoff, "fit": fit, "peer_problem": item["peer_problem"],
                    "challenger_problem": problem, "eligible_setup": item["eligible_setup"], "worlds": worlds,
                    "paired_forecast": set(worlds) == set(MODELS), "candidate": {
                        "strategy_id": candidate, "status": "RESEARCH_OPPORTUNITY" if candidate == "HOLD_LONG" else "WAIT",
                        "reason": problem or worlds.get("PEER_LAG_RIDGE", {}).get("decision_reason"),
                        "quote_id": payload["quote"]["observation_id"] if payload["quote"] else None,
                        "quote_status": payload["quote_problem"] or "OBSERVED_QUOTE_NOT_USED_FOR_BAR_PATH_ECONOMICS",
                        "execution_readiness": "NO_EXECUTABLE_CLAIM_REQUIRES_REVIEWED_QUOTE_AND_EXECUTION_ADAPTER",
                        "earliest_entry_step": item["earliest_entry_step"], "authority": "NONE_RESEARCH_ONLY"},
                    "calibration": "UNCALIBRATED_MODEL_CONDITIONAL_SCENARIOS", "authority": "NONE_RESEARCH_ONLY"}
                yield "PEER_FORECAST", now, record
                if problem:
                    yield "PEER_REFUSAL", now, {"sample_id": item["sample_id"], "phase": item["phase"],
                        "category": "CHALLENGER_UNAVAILABLE", "reason": problem}
    for sample_id, item in sorted(pending.items()):
        yield "PEER_REFUSAL", plan.end, {"sample_id": sample_id, "phase": item["phase"],
            "category": "NO_MATURED_COMPLETE_PATH", "reason": "NO_MATURED_COMPLETE_PATH_AT_RUN_END"}
    yield "PEER_CLOSE", plan.end, {"pending_sample_ids": sorted(pending), "matured_samples": len(matured),
                                  "source_samples": len(samples), "model_frozen": frozen is not None}


def summarize_peer(rows):
    samples = [r["payload"] for r in rows if r["kind"] == "PEER_SAMPLE"]
    forecasts = [r["payload"] for r in rows if r["kind"] == "PEER_FORECAST"]
    outcomes = [r["payload"] for r in rows if r["kind"] == "PEER_OUTCOME"]
    def phase_summary(phase):
        opportunities = [r for r in samples if r["phase"] == phase]
        all_outcomes = [r for r in outcomes if r["phase"] == phase]
        paired = [r for r in all_outcomes if r["paired"]]
        def mean(values):
            return float(np.mean(values)) if values else None
        return {"source_samples": len(opportunities), "matured_samples": len(all_outcomes),
                "peer_available": sum(r["peer_state"] is not None for r in opportunities),
                "eligible_setups": sum(r["eligible_setup"] for r in opportunities),
                "paired_samples": len(paired), "unpaired_matured_samples": len(all_outcomes) - len(paired),
                "sessions": sorted({r["session"] for r in paired}),
                "models": {name: {"crps": mean([r["scores"][name]["crps"] for r in paired]),
                    "brier": mean([r["scores"][name]["brier"] for r in paired]),
                    "policy_mean_net": mean([r["policy_net"][name] for r in paired]),
                    "policy_mean_net_cost_3x": mean([r["policy_net_cost_3x"][name] for r in paired]),
                    "hold_long_mean_net": mean([r["hold_long_net"] for r in paired]), "wait_mean_net": 0. if paired else None,
                    "paired_long_decisions": sum(r["decisions"][name] == "HOLD_LONG" for r in paired),
                    "all_matured_policy_mean_net_including_waits": mean([r["policy_net"][name] for r in all_outcomes]),
                    "all_matured_policy_mean_net_cost_3x_including_waits": mean([r["policy_net_cost_3x"][name] for r in all_outcomes])}
                    for name in MODELS},
                "paired_crps_delta_vs_zero": mean([r["scores"]["PEER_LAG_RIDGE"]["crps"] - r["scores"]["ZERO_DRIFT"]["crps"] for r in paired]),
                "paired_brier_delta_vs_zero": mean([r["scores"]["PEER_LAG_RIDGE"]["brier"] - r["scores"]["ZERO_DRIFT"]["brier"] for r in paired]),
                "paired_crps_delta_vs_own_stock": mean([r["scores"]["PEER_LAG_RIDGE"]["crps"] - r["scores"]["OWN_STOCK_RIDGE"]["crps"] for r in paired]),
                "paired_brier_delta_vs_own_stock": mean([r["scores"]["PEER_LAG_RIDGE"]["brier"] - r["scores"]["OWN_STOCK_RIDGE"]["brier"] for r in paired])}
    return {"schema": "APEX_PEER_EXPERIMENT_RESULT_V1", "status": "RESEARCH_COMPLETE",
        "input_class": rows[0]["payload"]["input_class"], "hypothesis": CONTRACT,
        "counts": {"source_samples": len(samples), "peer_available": sum(r["peer_state"] is not None for r in samples),
            "eligible_setups": sum(r["eligible_setup"] for r in samples), "forecasts": len(forecasts),
            "paired_outcomes": sum(r["paired"] for r in outcomes), "matured_outcomes": len(outcomes),
            "unmatured_samples": len(rows[-1]["payload"]["pending_sample_ids"]),
            "research_opportunities": sum(r["candidate"]["strategy_id"] == "HOLD_LONG" for r in forecasts)},
        "phase_counts": {p: dict(Counter(r["phase"] for r in samples))[p] if any(r["phase"] == p for r in samples) else 0 for p in ("WARMUP", "DEVELOPMENT", "HOLDOUT")},
        "refusals": dict(sorted(Counter(r["payload"]["category"] for r in rows if r["kind"] == "PEER_REFUSAL").items())),
        "source_refusals": dict(sorted(Counter(r["payload"]["reason"] for r in rows if r["kind"] == "PEER_SOURCE_REFUSAL").items())),
        "warmup": phase_summary("WARMUP"), "development": phase_summary("DEVELOPMENT"), "holdout": phase_summary("HOLDOUT"),
        "orders": 0, "fills": 0, "account_pnl": None,
        "readiness": "RESEARCH_ONLY_NO_PROMOTION_OR_TRADING_AUTHORITY",
        "limitations": ["One preregistered-in-this-manifest hypothesis; no external registration or global search-budget accounting",
            "Peer betas and lag gaps are descriptive association, not proof of forced flows or causality",
            "Full-path linear drift timing is an assumption; scenario frequencies remain uncalibrated",
            "Paired forecast comparisons retain identical samples; no significance test or winner promotion",
            "Cost stress holds the decision fixed; independent counterfactual opportunities are not portfolio P&L",
            "No quotes or executed fills are synthesized; source availability and revision limits remain"]}


def _source(source_run):
    source_run = Path(source_run).resolve()
    verdict = verify_lab(source_run)
    _require(verdict["status"] == "VALID", "PEER_SOURCE_LAB_INVALID:" + ";".join(verdict.get("problems", [])))
    manifest = json.loads((source_run / "manifest.json").read_text())
    plan, config, costs = ResearchPlan(**manifest["plan"]), Config(**manifest["config"]), SimulationCosts(**manifest["costs"])
    rows = read_complete(source_run / "forecast" / "ledger.jsonl", expected_last_kind="RUN_CLOSE", expected_epoch=plan.end,
                         completion_path=source_run / "forecast" / "COMPLETE")
    raw = (source_run / "forecast" / "input.json").read_bytes()
    observations, _ = normalize(json.loads(raw))
    return source_run, verdict, manifest, plan, config, costs, rows, raw, observations


def _loader(source):
    def load(filename):
        path = source / "forecast" / filename
        _require(isinstance(filename, str) and Path(filename).name == filename and not path.is_symlink()
                 and path.resolve().parent == (source / "forecast").resolve(), "PEER_INVALID_SOURCE_PATH")
        return np.load(path, allow_pickle=False)
    return load


def run_peer_experiment(source_run: Path, out: Path, *, market_symbol: str, sector_symbol: str) -> dict:
    out = Path(out)
    _require(out.resolve() != Path(source_run).resolve() and Path(source_run).resolve() not in out.resolve().parents,
             "PEER_OUTPUT_MUST_BE_SEPARATE_FROM_SOURCE")
    out.mkdir(parents=True, exist_ok=False)
    ledger = None
    try:
        source, verification, source_manifest, plan, config, costs, source_rows, raw, observations = _source(source_run)
        _require(all(isinstance(s, str) and s for s in (market_symbol, sector_symbol))
                 and len({config.symbol, market_symbol, sector_symbol}) == 3, "PEER_DISTINCT_TARGET_MARKET_SECTOR_REQUIRED")
        manifest = {"schema": "APEX_PEER_EXPERIMENT_MANIFEST_V1", "source_run": os.path.relpath(source, out.resolve()),
            "source_path_basis": "RELATIVE_TO_EXPERIMENT_ROOT",
            "source_manifest_digest": digest(source_manifest), "source_lab_head": verification["ledger_head"],
            "source_forecast_head": source_rows[-1]["hash"], "input_sha256": hashlib.sha256(raw).hexdigest(),
            "plan": asdict(plan), "config": config.record(), "costs": asdict(costs),
            "market_symbol": market_symbol, "sector_symbol": sector_symbol,
            "contract": CONTRACT, "code": code_manifest(), "authority": "NONE_RESEARCH_ONLY"}
        (out / "manifest.json").write_text(canonical(manifest))
        ledger = Ledger(out / "ledger.jsonl")
        ledger.append("RUN_OPEN", plan.start, {"manifest_digest": digest(manifest),
            "source_lab_head": verification["ledger_head"], "source_forecast_head": source_rows[-1]["hash"],
            "input_class": source_rows[0]["payload"]["input_class"]})
        for kind, epoch, payload in peer_events(source_rows, observations, plan=plan, config=config, costs=costs,
                market_symbol=market_symbol, sector_symbol=sector_symbol, load_paths=_loader(source)):
            ledger.append(kind, epoch, payload)
        rows = ledger.verified_close(expected_last_kind="PEER_CLOSE", expected_epoch=plan.end)
        ledger = None
        summary = summarize_peer(rows)
        (out / "summary.json").write_text(canonical(summary))
        (out / "COMPLETE").write_text(rows[-1]["hash"])
        return summary
    except BaseException as exc:
        (out / "FAILED.json").write_text(canonical({"error": type(exc).__name__, "reason": str(exc)}))
        raise
    finally:
        if ledger and not ledger.handle.closed:
            ledger.close()


def verify_peer_experiment(root: Path) -> dict:
    """Read-only exact reconstruction; hashes alone cannot pass this verifier."""
    try:
        root = Path(root)
        _require(not (root / "FAILED.json").exists(), "PEER_FAILED_RUN_NOT_COMPLETE")
        manifest = json.loads((root / "manifest.json").read_text())
        _require(manifest["schema"] == "APEX_PEER_EXPERIMENT_MANIFEST_V1"
                 and manifest["authority"] == "NONE_RESEARCH_ONLY" and _same(manifest["contract"], CONTRACT)
                 and manifest["source_path_basis"] == "RELATIVE_TO_EXPERIMENT_ROOT"
                 and isinstance(manifest["source_run"], str) and not Path(manifest["source_run"]).is_absolute(),
                 "PEER_MANIFEST_CONTRACT_INVALID")
        source, verification, source_manifest, plan, config, costs, source_rows, raw, observations = _source(root / manifest["source_run"])
        _require(all(isinstance(manifest[k], str) and manifest[k] for k in ("market_symbol", "sector_symbol"))
                 and len({config.symbol, manifest["market_symbol"], manifest["sector_symbol"]}) == 3,
                 "PEER_DISTINCT_TARGET_MARKET_SECTOR_REQUIRED")
        _require(manifest["source_manifest_digest"] == digest(source_manifest)
                 and manifest["source_lab_head"] == verification["ledger_head"]
                 and manifest["source_forecast_head"] == source_rows[-1]["hash"]
                 and manifest["input_sha256"] == hashlib.sha256(raw).hexdigest(), "PEER_SOURCE_IDENTITY_DISAGREES")
        _require(_same(manifest["plan"], asdict(plan)) and _same(manifest["config"], config.record())
                 and _same(manifest["costs"], asdict(costs)), "PEER_SOURCE_DECLARATIONS_DISAGREE")
        rows = read_complete(root / "ledger.jsonl", expected_last_kind="PEER_CLOSE", expected_epoch=plan.end,
                             completion_path=root / "COMPLETE")
        opening = {"manifest_digest": digest(manifest), "source_lab_head": verification["ledger_head"],
            "source_forecast_head": source_rows[-1]["hash"], "input_class": source_rows[0]["payload"]["input_class"]}
        _require(rows[0]["epoch"] == plan.start and _same(rows[0]["payload"], opening), "PEER_OPENING_NOT_SOURCE_BOUND")
        index, counts = 1, Counter()
        for kind, epoch, payload in peer_events(source_rows, observations, plan=plan, config=config, costs=costs,
                market_symbol=manifest["market_symbol"], sector_symbol=manifest["sector_symbol"], load_paths=_loader(source)):
            _require(index < len(rows), "PEER_EVENT_MISSING:" + kind)
            row = rows[index]
            _require(row["kind"] == kind and row["epoch"] == epoch and _same(row["payload"], payload),
                     "PEER_EVENT_RECONSTRUCTION_DISAGREES:" + kind)
            counts[kind] += 1
            index += 1
        _require(index == len(rows), "PEER_UNEXPECTED_TRAILING_EVENTS")
        _require(_same(json.loads((root / "summary.json").read_text()), summarize_peer(rows)), "PEER_SAVED_SUMMARY_DISAGREES")
        return {"status": "VALID", "problems": [], "ledger_head": rows[-1]["hash"],
                "verified_forecasts": counts["PEER_FORECAST"], "verified_outcomes": counts["PEER_OUTCOME"],
                "verified_events": index, "source_verification": verification["status"],
                "code_manifest_matches_current": manifest["code"] == code_manifest(), "scope": SCOPE}
    except Refused as exc:
        problem = str(exc)
    except (KeyError, TypeError, ValueError, OSError, OverflowError, AttributeError, IndexError) as exc:
        problem = "PEER_ARTIFACT_INVALID:" + type(exc).__name__
    return {"status": "MISMATCH", "problems": [problem], "code_manifest_matches_current": None, "scope": SCOPE}
