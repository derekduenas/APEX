"""Chronological challenger research through the real Twin and path simulator.

A frozen development/holdout boundary is recorded before the first forecast.
Only matured earlier labels train the ridge challenger. Holdout labels never
retrain or select it. No result authorizes trading or claims a calibrated edge.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path

import numpy as np

from .admissibility import require_mode
from .core import Config, Refused, canonical, code_manifest, digest, finite, money
from .data import normalize, regular, session, twin, visible
from .decision import evaluate, quote_at
from .forecast import predict
from .ledger import Ledger, read_verified
from .research_models import features, fit_ridge, predict_ridge

MODELS = ("ZERO_DRIFT", "SHRUNK_MEAN", "RIDGE_STATE")


def input_class_of(document, observations):
    """A merged document must not be able to launder a synthetic control into a recorded study.

    The union of a synthetic half and a recorded half is neither: it fails as a control because part of it is
    real, and it fails as evidence because part of it is invented. So it gets its own name, and a component's
    synthetic label survives the merge instead of being averaged away by the top-level `source` string.
    """
    components = document.get("components", [])
    synthetic = [str(c.get("source", "")).startswith("SYNTHETIC_") for c in components]
    if any(synthetic) and not all(synthetic):
        return "MIXED_SYNTHETIC_AND_RECORDED_INPUT"
    if components and all(synthetic):
        return "SYNTHETIC_RESEARCH_CONTROL"
    if str(document.get("source", "")).startswith("SYNTHETIC_") or (observations and all(r["availability_basis"] == "SYNTHETIC_CLOCK" for r in observations)):
        return "SYNTHETIC_RESEARCH_CONTROL"
    return "RECORDED_RESEARCH_WITH_DECLARED_AVAILABILITY_LIMITS"


@dataclass(frozen=True)
class ResearchPlan:
    start: float
    development_start: float
    holdout_start: float
    end: float
    scan_minutes: int = 30
    embargo_minutes: int = 1
    minimum_training_labels: int = 40
    training_window: int = 260
    minimum_comparison_sessions: int = 3
    ridge_alpha: float = 10.0
    decision_delay_seconds: float = 5.0

    def __post_init__(self):
        times = (self.start, self.development_start, self.holdout_start, self.end)
        if not all(finite(t) and t % 60 == 0 for t in times) or not all(a < b for a, b in zip(times, times[1:])):
            raise Refused("RESEARCH_PHASES_REQUIRE_ORDERED_MINUTE_BOUNDARIES")
        if len({session(t) for t in times}) != 4 or self.end - self.start > 93 * 86400:
            raise Refused("RESEARCH_PHASES_REQUIRE_SEPARATE_SESSIONS_AND_BOUNDED_RANGE")
        for name in ("scan_minutes", "embargo_minutes", "minimum_training_labels", "training_window", "minimum_comparison_sessions"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise Refused("INVALID_RESEARCH_PLAN:" + name)
        if self.training_window < self.minimum_training_labels or not finite(self.ridge_alpha) or self.ridge_alpha <= 0:
            raise Refused("INVALID_RESEARCH_TRAINING_POLICY")
        if any((t - self.start) % (self.scan_minutes * 60) for t in (self.development_start, self.holdout_start)):
            raise Refused("RESEARCH_PHASE_BOUNDARY_OFF_SCAN_GRID")
        if not finite(self.decision_delay_seconds) or not 0 <= self.decision_delay_seconds < 60:
            raise Refused("INVALID_RESEARCH_DECISION_DELAY")


def distribution_score(terminal, realized: float, *, model: str | None = None) -> dict:
    """Empirical CRPS in O(n log n), Brier, pinball and interval coverage."""
    values = np.sort(np.asarray(terminal, dtype=float))
    if values.ndim != 1 or len(values) < 2 or not np.all(np.isfinite(values)) or not finite(realized):
        raise Refused("INVALID_SCORING_INPUT")
    n = len(values)
    crps = float(np.mean(np.abs(values - realized)) - np.sum((2 * np.arange(1, n + 1) - n - 1) * values) / n**2)
    qs = {str(q): float(np.quantile(values, q)) for q in (.05, .5, .95)}
    p_up = .5 if model == "ZERO_DRIFT" else float(np.mean(values > 0))
    return {"crps": crps, "brier": (p_up - int(realized > 0))**2,
            "p_up": p_up, "outcome_up": int(realized > 0),
            "pinball": {q: max(float(q) * (realized - value), (float(q) - 1) * (realized - value)) for q, value in qs.items()},
            "interval_90_covered": qs["0.05"] <= realized <= qs["0.95"],
            "interval_90_width": qs["0.95"] - qs["0.05"], "quantiles": qs}


def _phase(now, plan):
    return "WARMUP" if now < plan.development_start else "DEVELOPMENT" if now < plan.holdout_start else "HOLDOUT"


def _model_metrics(scores):
    if not scores:
        return {"count": 0, "crps": None, "brier": None, "coverage_90": None, "reliability": []}
    bins = []
    for low in np.arange(0, 1, .2):
        high = min(1., float(low + .2))
        group = [s for s in scores if low <= s["p_up"] and (s["p_up"] < high or high == 1.)]
        bins.append({"lower": float(low), "upper": high, "count": len(group),
                     "mean_prediction": float(np.mean([s["p_up"] for s in group])) if group else None,
                     "observed_frequency": float(np.mean([s["outcome_up"] for s in group])) if group else None})
    return {"count": len(scores), "crps": float(np.mean([s["crps"] for s in scores])),
            "brier": float(np.mean([s["brier"] for s in scores])),
            "coverage_90": float(np.mean([s["interval_90_covered"] for s in scores])),
            "mean_interval_width": float(np.mean([s["interval_90_width"] for s in scores])),
            "pinball": {q: float(np.mean([s["pinball"][q] for s in scores])) for q in ("0.05", "0.5", "0.95")},
            "reliability": bins}


def _comparison(scores, phase, plan):
    groups = {}
    for s in scores:
        if s["phase"] == phase:
            groups.setdefault(s["sample_id"], {})[s["model"]] = s
    common = [g for g in groups.values() if set(g) == set(MODELS)]
    dates = sorted({g[MODELS[0]]["session"] for g in common})
    return {"paired_samples": len(common), "sessions": dates,
            "unpaired_samples": len(groups) - len(common),
            "sufficient_sessions": len(dates) >= plan.minimum_comparison_sessions,
            "models": {name: _model_metrics([g[name]["score"] for g in common]) for name in MODELS}}


def _selection(scores, plan):
    comparison = _comparison(scores, "DEVELOPMENT", plan)
    if not comparison["sufficient_sessions"]:
        return {"selected_model": "ZERO_DRIFT", "reason": "INSUFFICIENT_PAIRED_DEVELOPMENT_SESSIONS", "comparison": comparison}
    best = min(MODELS, key=lambda m: (comparison["models"][m]["crps"], MODELS.index(m)))
    return {"selected_model": best, "reason": "LOWEST_PAIRED_DEVELOPMENT_CRPS", "comparison": comparison}


def summarize(rows, plan):
    scores = [r["payload"] for r in rows if r["kind"] == "RESEARCH_SCORE"]
    selections = [r["payload"] for r in rows if r["kind"] == "SELECTION_FROZEN"]
    chosen = selections[0]["selected_model"] if selections else None
    dev, hold = _comparison(scores, "DEVELOPMENT", plan), _comparison(scores, "HOLDOUT", plan)
    # Treat whole sessions as the unit of descriptive variation. This is not
    # an IID confidence interval or a multiple-testing-adjusted significance test.
    paired = {}
    for s in scores:
        if s["phase"] == "HOLDOUT":
            paired.setdefault(s["sample_id"], {})[s["model"]] = s
    differences = {}
    for group in paired.values():
        if chosen and set(group) == set(MODELS):
            day = group[chosen]["session"]
            differences.setdefault(day, []).append(group[chosen]["score"]["crps"] - group["ZERO_DRIFT"]["score"]["crps"])
    session_deltas = {day: float(np.mean(values)) for day, values in sorted(differences.items())}
    forecasts = [r["payload"] for r in rows if r["kind"] == "RESEARCH_FORECAST"]
    candidates = [v["candidate"] for f in forecasts for v in f["variants"].values()]
    return {"schema": "APEX_RESEARCH_RESULT_V1", "status": "RESEARCH_COMPLETE",
            "input_class": rows[0]["payload"]["input_class"],
            "forecast_count": len(forecasts), "scored_model_forecasts": len(scores),
            "refusals": [r["payload"] for r in rows if r["kind"] == "RESEARCH_REFUSED"],
            "unmatured_samples": rows[-1]["payload"].get("pending_sample_ids", []),
            "development": dev, "holdout": hold, "selected_on_development": chosen,
            "frozen_selection": selections[0] if selections else None,
            "development_scope": "All labels matured by evaluation end; frozen_selection retains the earlier selection evidence separately",
            "holdout_session_crps_delta_vs_zero": session_deltas,
            "mean_holdout_session_crps_delta_vs_zero": float(np.mean(list(session_deltas.values()))) if session_deltas else None,
            "candidate_observations": {"long": sum(c["decision"] == "EXPERIMENTAL_LONG" for c in candidates),
                                       "wait": sum(c["decision"] == "WAIT" for c in candidates),
                                       "wait_reasons": dict(sorted(Counter(c["reason"] for c in candidates
                                                                           if c["decision"] == "WAIT").items())),
                                       "meaning": ("Hypothetical independent opportunities evaluated at the decision "
                                                   "instant. NOT trades: no fill, exit, stop ordering or realized "
                                                   "P&L is established here, and none can be without the "
                                                   "post-decision quote sequence, which this input does not carry.")},
            "variance_models": sorted({f["base_forecast"]["variance"]["model_id"] for f in forecasts}),
            "research_hypotheses": list(MODELS), "hypothesis_count_this_run": len(MODELS),
            "selection_rule": "Lowest development CRPS on identical matured samples; tie favors zero drift; holdout never selects",
            "readiness": "RESEARCH_ONLY_NO_CALIBRATION_OR_TRADING_AUTHORITY",
            "limitations": ["Fixed symbol; no universe search or survivorship correction",
                            "Repeated runs are not an independent holdout; no global search-budget registry",
                            "Session deltas are descriptive, not significance or guaranteed edge",
                            "Scenario frequencies are uncalibrated; fitted innovation law differs from bounded simulation law",
                            "Candidate observations are hypothetical independent opportunities, not trades or portfolio P&L",
                            "Historical availability/revision limitations remain those of the captured input"]}


def decision_instants(plan: ResearchPlan) -> list:
    """The scan grid: the instants at which this plan actually forms a candidate and reads a quote.

    The run also stops at the holdout boundary and at the end to mature labels, but those stops take no decision
    and read no quote, so they are not decision instants. One definition, so anything preparing inputs for a plan
    (a quote fetch, say) asks at the same instants the run will rather than at a second grid that can drift.
    """
    return sorted(float(t + plan.decision_delay_seconds)
                  for t in np.arange(plan.start, plan.end, plan.scan_minutes * 60)
                  if t + plan.decision_delay_seconds < plan.end)


def run_research(input_path: Path | bytes, out: Path, *, plan: ResearchPlan, config: Config) -> dict:
    if plan.scan_minutes < config.horizon_minutes:
        raise Refused("OVERLAPPING_TARGETS_NOT_SUPPORTED_BY_THIS_RESEARCH_PLAN")
    out.mkdir(parents=True, exist_ok=False)
    ledger = None
    try:
        raw = input_path.read_bytes() if isinstance(input_path, Path) else input_path
        (out / "input.json").write_bytes(raw)
        document = json.loads(raw)
        observations, rejected = normalize(document)
        input_class = input_class_of(document, observations)
        # The ceiling is enforced, not merely reported: a mixed input reaches no mode at all, and a synthetic
        # control is admitted only through the synthetic research path that declares itself as one.
        admitted = require_mode(document, observations,
                                "SYNTHETIC_CONTROL" if input_class == "SYNTHETIC_RESEARCH_CONTROL" else "OFFLINE_RESEARCH")
        manifest = {"schema": "APEX_RESEARCH_MANIFEST_V1", "plan": asdict(plan), "config": config.record(),
                    "input_sha256": hashlib.sha256(raw).hexdigest(), "input_class": input_class,
                    "code": code_manifest(), "models": list(MODELS), "rejected": rejected,
                    "admissibility": admitted,
                    "availability_bases": sorted({r["availability_basis"] for r in observations}),
                    "holdout_contract": "Ridge training labels and model selection frozen before holdout. Unlabelled variance inputs update causally.",
                    "authorization": "RESEARCH_ONLY_NO_BROKER"}
        (out / "manifest.json").write_text(canonical(manifest))
        ledger = Ledger(out / "ledger.jsonl")
        ledger.append("RUN_OPEN", plan.start, {"manifest_digest": digest(manifest), "config": config.record(), "input_class": input_class})
        pending, matured, scores = {}, [], []
        selected = False
        decision_times = set(decision_instants(plan))
        for now in sorted(decision_times | {plan.holdout_start, plan.end}):
            bars, _ = visible(observations, now=now, symbol=config.symbol, kind="bar")
            by_close = {b["event_epoch"] + 60: b for b in bars if regular(b["event_epoch"])}
            for sample_id, item in list(pending.items()):
                target = by_close.get(item["target_epoch"])
                if target is None or now < item["target_epoch"]:
                    continue
                realized = math.log(target["close"] / item["spot"])
                label = {"sample_id": sample_id, "decision_epoch": item["decision_epoch"], "target_epoch": item["target_epoch"],
                         "price_origin_epoch": item["price_origin_epoch"],
                         "label_available_epoch": target["available_epoch"], "features": item["features"], "realized_log_return": realized}
                ledger.append("RESEARCH_LABEL", now, {**label, "bar_id": target["observation_id"]})
                matured.append(label)
                for model, variant in item["variants"].items():
                    score = {"sample_id": sample_id, "model": model, "session": session(item["decision_epoch"]),
                             "phase": item["phase"], "label_bar_id": target["observation_id"], "realized_log_return": realized,
                             "score": distribution_score(variant["terminal"], realized, model=model)}
                    ledger.append("RESEARCH_SCORE", now, score)
                    scores.append(score)
                del pending[sample_id]
            if now >= plan.holdout_start and not selected:
                # Only labels observed strictly before the frozen boundary may
                # participate in selection, even if a late delivery arrives now.
                development_ids = {s["sample_id"] for s in scores if s["phase"] == "DEVELOPMENT"}
                eligible_ids = {m["sample_id"] for m in matured if m["sample_id"] in development_ids and m["label_available_epoch"] < plan.holdout_start and m["target_epoch"] < plan.holdout_start}
                eligible_scores = [s for s in scores if s["sample_id"] in eligible_ids]
                ledger.append("SELECTION_FROZEN", now, {**_selection(eligible_scores, plan), "label_cutoff_epoch": plan.holdout_start,
                                                       "eligible_sample_ids": sorted(eligible_ids)})
                selected = True
            origin = now - plan.decision_delay_seconds
            target_clock = origin + config.horizon_minutes * 60 - 60
            if now not in decision_times or not regular(now) or not regular(target_clock) or session(target_clock) != session(now):
                continue
            phase = _phase(now, plan)
            try:
                state, returns = twin(observations, now, config.symbol)
                # A frozen delivery allowance permits measured receipts. The
                # actual decision clock remains separate from the bar-price origin.
                if state["fields"]["last_bar_event"] + 60 != origin:
                    raise Refused("RESEARCH_DECISION_REQUIRES_CURRENT_COMPLETED_MINUTE")
                x = features(state, returns)
                sample_id = digest({"snapshot_id": state["snapshot_id"], "horizon": config.horizon_minutes, "features": x})
                item = {"sample_id": sample_id, "decision_epoch": now, "price_origin_epoch": origin,
                        "target_epoch": origin + config.horizon_minutes * 60,
                        "spot": state["fields"]["spot"], "features": x, "phase": phase, "variants": {}}
                ledger.append("RESEARCH_SAMPLE", now, {**item, "snapshot": state})
                pending[sample_id] = item
                if phase == "WARMUP":
                    continue
                base, paths = predict(returns, state, config)
                base_file = base["forecast_id"] + ".npy"
                np.save(out / base_file, paths, allow_pickle=False)
                drift_total = base["direction"]["per_minute_log_drift"] * config.horizon_minutes
                terminal_noise = paths[:, -1] - drift_total
                shifts = {"ZERO_DRIFT": 0., "SHRUNK_MEAN": drift_total}
                cutoff = min(now, plan.holdout_start) - plan.embargo_minutes * 60
                train = [m for m in matured if m["label_available_epoch"] <= cutoff and m["target_epoch"] <= cutoff][-plan.training_window:]
                ridge, ridge_problem = None, None
                if len(train) >= plan.minimum_training_labels:
                    try:
                        ridge = fit_ridge(train, cutoff=cutoff, horizon_minutes=config.horizon_minutes, alpha=plan.ridge_alpha)
                        shifts["RIDGE_STATE"] = predict_ridge(ridge, x)
                    except Refused as exc:
                        ridge_problem = str(exc)
                        ridge = None
                else:
                    ridge_problem = f"INSUFFICIENT_MATURED_LABELS:{len(train)}<{plan.minimum_training_labels}"
                q, problem = quote_at(observations, now, config)
                variants = {}
                for name, shift in shifts.items():
                    terminal = terminal_noise + shift
                    if not np.all(np.isfinite(terminal)) or float(np.max(np.abs(terminal))) > 100:
                        if name == "RIDGE_STATE":
                            ridge_problem = "RIDGE_SIMULATION_NUMERIC_FAILURE"
                            continue
                        raise Refused("BASELINE_SIMULATION_NUMERIC_FAILURE")
                    candidate = evaluate(base, terminal[:, None], q, problem, cash=money(config.starting_cash), position_open=False, config=config)
                    candidate["authority"] = "NONE_RESEARCH_ONLY"
                    filename = sample_id + "-" + name + ".npy"
                    np.save(out / filename, terminal, allow_pickle=False)
                    variants[name] = {"total_log_drift": shift, "terminal_file": filename, "terminal_digest": digest(terminal.tolist()),
                                      "candidate": candidate, "p_up": .5 if name == "ZERO_DRIFT" else float(np.mean(terminal > 0))}
                    item["variants"][name] = {"terminal": terminal}
                ledger.append("RESEARCH_FORECAST", now, {"sample_id": sample_id, "phase": phase, "base_forecast": base,
                                                         "base_path_file": base_file, "features": x, "variants": variants,
                                                         "ridge": ridge, "ridge_problem": ridge_problem,
                                                         "training_cutoff_epoch": cutoff, "quote": q, "quote_problem": problem})
            except Refused as exc:
                ledger.append("RESEARCH_REFUSED", now, {"phase": phase, "reason": str(exc)})
        ledger.append("RUN_CLOSE", plan.end, {"pending_sample_ids": sorted(pending)})
        ledger.close()
        ledger = None
        rows = read_verified(out / "ledger.jsonl")
        summary = summarize(rows, plan)
        (out / "summary.json").write_text(canonical(summary))
        (out / "COMPLETE").write_text(rows[-1]["hash"])
        return summary
    except BaseException as exc:
        (out / "FAILED.json").write_text(canonical({"type": type(exc).__name__, "reason": str(exc)}))
        raise
    finally:
        if ledger:
            ledger.close()
