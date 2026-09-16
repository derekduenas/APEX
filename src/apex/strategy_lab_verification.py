"""Reconstruct strategy-lab artifacts through their real downstream readers.

The nested research verifier checks causal features, matured labels, forecast
selection and seeded simulation paths before this reader uses them. This layer
then reproduces the exact intelligence/outcome/tournament event sequence from
the original normalized input and those verified paths. Reusing deterministic
production calculations establishes persisted consistency; it is not a second
independent mathematical implementation, source authentication, or evidence of
profitability. Separate primitive tests establish their numerical contracts.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict
import hashlib
import json
from pathlib import Path

import numpy as np

from .core import Config, Refused, canonical, code_manifest, digest
from .data import normalize
from .ledger import read_verified
from .research import ResearchPlan
from .research_verification import verify_research
from .strategy_lab import LabPolicy, lab_events, summarize_lab
from .strategy_simulation import SimulationCosts, registry_record


SCOPE = (
    "Persisted causal consistency: nested research verification then exact replay through current "
    "regime, analog, strategy, feedback, selection and evidence readers. Successful variance "
    "optimization is not independently refitted. Shared deterministic calculations are not a "
    "second independent mathematical implementation. Hashes do not authenticate provider history, "
    "executed source, or prior research registration; no calibration, profit, or trading authority is established."
)


def _require(condition, reason):
    if not condition:
        raise Refused(reason)


def _same(left, right):
    # Canonical comparison also distinguishes true from 1 and binds every key.
    return canonical(left) == canonical(right)


def _verify_lab(root: Path) -> dict:
    _require(not (root / "FAILED.json").exists(), "LAB_FAILED_RUN_NOT_COMPLETE")
    try:
        source_verification = verify_research(root / "forecast")
    except Refused as exc:
        raise Refused("LAB_SOURCE_RESEARCH_REFUSED:" + str(exc)) from exc
    _require(source_verification["status"] == "VALID", "LAB_SOURCE_RESEARCH_INVALID")
    manifest = json.loads((root / "manifest.json").read_text())
    source_manifest = json.loads((root / "forecast" / "manifest.json").read_text())
    raw = (root / "forecast" / "input.json").read_bytes()
    _require(manifest["schema"] == "APEX_STRATEGY_LAB_MANIFEST_V1"
             and manifest["authority"] == "NONE_RESEARCH_ONLY", "LAB_MANIFEST_CONTRACT_INVALID")
    _require(hashlib.sha256(raw).hexdigest() == manifest["input_sha256"] == source_manifest["input_sha256"],
             "LAB_INPUT_DIGEST_DISAGREES")
    config, plan = Config(**manifest["config"]), ResearchPlan(**manifest["plan"])
    policy, costs = LabPolicy(**manifest["policy"]), SimulationCosts(**manifest["costs"])
    _require(_same(manifest["config"], config.record()) and _same(manifest["plan"], asdict(plan))
             and _same(manifest["policy"], asdict(policy)) and _same(manifest["costs"], asdict(costs)),
             "LAB_INCOMPLETE_DECLARED_CONTRACT")
    _require(_same(manifest["config"], source_manifest["config"])
             and _same(manifest["plan"], source_manifest["plan"]), "LAB_FORECAST_PLAN_OR_CONFIG_DISAGREES")
    _require(plan.scan_minutes >= config.horizon_minutes, "LAB_OVERLAPPING_OPPORTUNITIES")
    _require(_same(manifest["registry"], registry_record()), "LAB_FROZEN_STRATEGY_REGISTRY_DISAGREES")
    _require(_same(manifest["code"], source_manifest["code"]), "LAB_SOURCE_CODE_IDENTITY_DISAGREES")
    observations, _ = normalize(json.loads(raw))
    source_rows = read_verified(root / "forecast" / "ledger.jsonl")
    rows = read_verified(root / "ledger.jsonl")
    opening = {"manifest_digest": digest(manifest), "forecast_head": source_rows[-1]["hash"],
               "config": config.record(), "input_class": source_rows[0]["payload"]["input_class"]}
    _require(_same(rows[0]["payload"], opening) and rows[0]["epoch"] == plan.start,
             "LAB_MANIFEST_OR_SOURCE_HEAD_NOT_LEDGER_BOUND")
    _require(rows[-1]["kind"] == "LAB_CLOSE" and rows[-1]["epoch"] == plan.end
             and (root / "COMPLETE").read_text() == rows[-1]["hash"], "LAB_COMPLETION_INVALID")

    def load_paths(filename):
        path = root / "forecast" / filename
        _require(isinstance(filename, str) and Path(filename).name == filename
                 and not path.is_symlink() and path.resolve().parent == (root / "forecast").resolve(),
                 "LAB_INVALID_SOURCE_PATH_NAME")
        # Dimensions and full path digest/model identity have already been
        # checked by verify_research; pickle loading remains explicitly disabled.
        return np.load(path, allow_pickle=False)

    index, counts = 1, Counter()
    expected_events = lab_events(source_rows, observations, plan=plan, config=config,
                                 policy=policy, costs=costs, load_paths=load_paths)
    for kind, epoch, payload, _ in expected_events:
        _require(index < len(rows), "LAB_EVENT_MISSING:" + kind)
        row = rows[index]
        _require(row["kind"] == kind and row["epoch"] == epoch,
                 "LAB_EVENT_ORDER_OR_COMPLETENESS_DISAGREES:" + kind)
        _require(_same(row["payload"], payload), "LAB_EVENT_PAYLOAD_DISAGREES:" + kind)
        counts[kind] += 1
        index += 1
    _require(index == len(rows), "LAB_UNEXPECTED_TRAILING_EVENTS")
    expected_summary = summarize_lab(rows, policy=policy, config=config)
    saved_summary = json.loads((root / "summary.json").read_text())
    _require(_same(saved_summary, expected_summary), "LAB_SAVED_SUMMARY_DISAGREES")
    return {"status": "VALID", "problems": [], "ledger_head": rows[-1]["hash"],
            "verified_source_forecasts": source_verification["verified_forecasts"],
            "verified_intelligence": counts["INTELLIGENCE"],
            "verified_tournaments": counts["STRATEGY_TOURNAMENT"],
            "verified_outcomes": counts["STRATEGY_OUTCOME"], "verified_events": index,
            "source_input_and_paths": "VERIFIED", "event_order_and_completeness": "VERIFIED",
            "summary_and_evidence": "RECOMPUTED_FROM_VERIFIED_EVENTS",
            "code_manifest_matches_current": manifest["code"] == code_manifest(),
            "scope": SCOPE}


def verify_lab(path: Path) -> dict:
    """Return a fail-closed verdict without writing or altering any artifact."""
    try:
        return _verify_lab(Path(path))
    except Refused as exc:
        problem = str(exc)
    except (KeyError, TypeError, ValueError, OSError, OverflowError, AttributeError, IndexError) as exc:
        problem = "LAB_ARTIFACT_INVALID:" + type(exc).__name__
    return {"status": "MISMATCH", "problems": [problem],
            "code_manifest_matches_current": None, "scope": SCOPE}
