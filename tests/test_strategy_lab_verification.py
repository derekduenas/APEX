"""Real lab artifacts: fully rehashed tampering must still fail replay."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil

import numpy as np
import pytest

from apex.core import Config, canonical, digest
from apex.fixtures import research_demo_document
from apex.ledger import read_verified
from apex.strategy_lab import LabPolicy, run_lab, summarize_lab
from apex.strategy_lab_verification import verify_lab


@pytest.fixture(scope="module")
def lab_fixture(tmp_path_factory):
    document, plan = research_demo_document()
    plan = replace(plan, scan_minutes=60, minimum_training_labels=20)
    config = Config(variance="ewma", paths=100)
    policy = LabPolicy(bootstrap_samples=199)
    root = tmp_path_factory.mktemp("lab-verification") / "run"
    summary = run_lab(canonical(document).encode(), root, plan=plan, config=config, policy=policy)
    return root, summary, config, policy


def _copy_run(lab_fixture, tmp_path):
    return Path(shutil.copytree(lab_fixture[0], tmp_path / "run"))


def _rehash(root, rows, *, refresh_summary=False, config=None, policy=None):
    previous = "GENESIS"
    for index, row in enumerate(rows, 1):
        row.pop("hash", None)
        row["prev_hash"], row["seq"] = previous, index
        row["hash"] = digest(row)
        previous = row["hash"]
    (root / "ledger.jsonl").write_text("\n".join(canonical(row) for row in rows) + "\n")
    (root / "COMPLETE").write_text(previous)
    if refresh_summary:
        (root / "summary.json").write_text(canonical(summarize_lab(rows, policy=policy, config=config)))


def _change_manifest(root, mutate):
    manifest = json.loads((root / "manifest.json").read_text())
    mutate(manifest)
    (root / "manifest.json").write_text(canonical(manifest))
    rows = read_verified(root / "ledger.jsonl")
    rows[0]["payload"]["manifest_digest"] = digest(manifest)
    _rehash(root, rows)


def _assert_problem(root, expected):
    result = verify_lab(root)
    assert result["status"] == "MISMATCH"
    assert any(expected in p for p in result["problems"]), result


def test_real_lab_reconstruction_checks_every_stage_without_changing_artifacts(lab_fixture, monkeypatch):
    root, summary, _, _ = lab_fixture
    before = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in root.rglob("*") if p.is_file()}

    def no_optimizer(*args, **kwargs):
        raise AssertionError("Successful persisted model verification must not rerun forecast optimization")

    monkeypatch.setattr("apex.research_verification.predict", no_optimizer)
    result = verify_lab(root)
    assert result["status"] == "VALID", result
    assert result["verified_tournaments"] == summary["tournaments"] > 0
    assert result["verified_intelligence"] == summary["intelligence_snapshots"] > result["verified_tournaments"]
    assert result["verified_outcomes"] == summary["completed_counterfactual_episodes"] > 0
    assert result["source_input_and_paths"] == "VERIFIED"
    assert result["summary_and_evidence"] == "RECOMPUTED_FROM_VERIFIED_EVENTS"
    assert "not a second independent mathematical implementation" in result["scope"]
    after = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in root.rglob("*") if p.is_file()}
    assert after == before


@pytest.mark.parametrize("change", ["world", "candidate", "outcome", "feedback", "regime"])
def test_fully_rehashed_calculations_cannot_replace_actual_reader_outputs(lab_fixture, tmp_path, change):
    root = _copy_run(lab_fixture, tmp_path)
    rows = read_verified(root / "ledger.jsonl")
    tournament = next(r["payload"] for r in rows if r["kind"] == "STRATEGY_TOURNAMENT")
    if change == "world":
        tournament["worlds"]["SELECTED_MODEL"]["strategies"]["HOLD_LONG"]["summary"]["expected_net"] += 123
        expected = "STRATEGY_TOURNAMENT"
    elif change == "candidate":
        tournament["candidate"]["strategy_id"] = "HOLD_LONG"
        tournament["candidate"]["status"] = "RESEARCH_OPPORTUNITY"
        tournament["candidate"]["capital_authority"] = "PAPER_AUTHORIZED"
        expected = "STRATEGY_TOURNAMENT"
    elif change == "outcome":
        outcome = next(r["payload"] for r in rows if r["kind"] == "STRATEGY_OUTCOME")
        outcome["strategy_outcomes"]["HOLD_LONG"] += 100
        outcome["net_returns"]["HOLD_LONG"] += .01
        expected = "STRATEGY_OUTCOME"
    elif change == "feedback":
        del tournament["feedback"]
        expected = "STRATEGY_TOURNAMENT"
    else:
        intelligence = next(r["payload"] for r in rows if r["kind"] == "INTELLIGENCE")
        intelligence["intelligence"]["regime"] = "KNOWN_FUTURE_BULL_MARKET"
        expected = "INTELLIGENCE"
    _rehash(root, rows)
    _assert_problem(root, "LAB_EVENT_PAYLOAD_DISAGREES:" + expected)


def test_changed_candidate_and_correspondingly_recalculated_summary_still_refuse(lab_fixture, tmp_path):
    root = _copy_run(lab_fixture, tmp_path)
    rows = read_verified(root / "ledger.jsonl")
    tournament = next(r["payload"] for r in rows if r["kind"] == "STRATEGY_TOURNAMENT")
    old = tournament["candidate"]["strategy_id"]
    tournament["candidate"]["strategy_id"] = "HOLD_LONG" if old == "WAIT" else "WAIT"
    _rehash(root, rows, refresh_summary=True, config=lab_fixture[2], policy=lab_fixture[3])
    _assert_problem(root, "LAB_EVENT_PAYLOAD_DISAGREES:STRATEGY_TOURNAMENT")


@pytest.mark.parametrize("kind", ["INTELLIGENCE", "STRATEGY_OUTCOME", "STRATEGY_TOURNAMENT"])
def test_deleted_earlier_layer_cannot_be_hidden_by_rehashing(lab_fixture, tmp_path, kind):
    root = _copy_run(lab_fixture, tmp_path)
    rows = read_verified(root / "ledger.jsonl")
    index = next(i for i, r in enumerate(rows) if r["kind"] == kind)
    del rows[index]
    _rehash(root, rows)
    _assert_problem(root, "LAB_EVENT_ORDER_OR_COMPLETENESS_DISAGREES:" + kind)


def test_future_analog_observation_cannot_be_inserted_in_a_rehashed_tournament(lab_fixture, tmp_path):
    root = _copy_run(lab_fixture, tmp_path)
    rows = read_verified(root / "ledger.jsonl")
    tournament = next(r["payload"] for r in rows if r["kind"] == "STRATEGY_TOURNAMENT" and r["payload"]["analog"])
    # Replace a declared sampled-source ID with an actual future episode and
    # repair both model identity and the outer ledger. The causal reader still
    # refuses this source, even though all persisted hashes agree internally.
    source_rows = read_verified(root / "forecast" / "ledger.jsonl")
    future = next(r["payload"] for r in reversed(source_rows) if r["kind"] == "RESEARCH_LABEL")
    assert future["label_available_epoch"] > tournament["analog"]["decision_epoch"]
    analog = tournament["analog"]
    analog["sampled_source_ids"][0] = future["sample_id"]
    analog["artifact_id"] = digest({k: v for k, v in analog.items() if k != "artifact_id"})
    _rehash(root, rows)
    _assert_problem(root, "LAB_EVENT_PAYLOAD_DISAGREES:STRATEGY_TOURNAMENT")


def test_rebound_changed_cost_policy_does_not_verify_old_execution_results(lab_fixture, tmp_path):
    root = _copy_run(lab_fixture, tmp_path)
    _change_manifest(root, lambda m: m["costs"].__setitem__("half_spread_bps", 20.0))
    _assert_problem(root, "LAB_EVENT_PAYLOAD_DISAGREES:")


@pytest.mark.parametrize("change,problem", [
    ("input", "LAB_INPUT_DIGEST_DISAGREES"),
    ("plan", "LAB_FORECAST_PLAN_OR_CONFIG_DISAGREES"),
    ("registry", "LAB_FROZEN_STRATEGY_REGISTRY_DISAGREES"),
    ("source_head", "LAB_MANIFEST_OR_SOURCE_HEAD_NOT_LEDGER_BOUND"),
])
def test_lab_cannot_silently_bind_to_another_input_plan_or_registry(lab_fixture, tmp_path, change, problem):
    root = _copy_run(lab_fixture, tmp_path)
    if change == "input":
        _change_manifest(root, lambda m: m.__setitem__("input_sha256", "0" * 64))
    elif change == "plan":
        _change_manifest(root, lambda m: m["plan"].__setitem__("minimum_training_labels", 25))
    elif change == "registry":
        _change_manifest(root, lambda m: m["registry"]["strategies"][1].__setitem__("strategy_id", "UNREGISTERED"))
    else:
        rows = read_verified(root / "ledger.jsonl")
        rows[0]["payload"]["forecast_head"] = "0" * 64
        _rehash(root, rows)
    _assert_problem(root, problem)


def test_nested_simulation_is_checked_before_strategy_replay(lab_fixture, tmp_path):
    root = _copy_run(lab_fixture, tmp_path)
    rows = read_verified(root / "forecast" / "ledger.jsonl")
    forecast = next(r["payload"] for r in rows if r["kind"] == "RESEARCH_FORECAST")
    path = root / "forecast" / forecast["base_path_file"]
    values = np.load(path, allow_pickle=False)
    values[0, 1] += .1
    np.save(path, values, allow_pickle=False)
    _assert_problem(root, "LAB_SOURCE_RESEARCH_REFUSED:RESEARCH_SIMULATION_ARTIFACT_CHANGED")


def test_current_code_manifest_comparison_is_informational(lab_fixture, monkeypatch):
    monkeypatch.setattr("apex.strategy_lab_verification.code_manifest", lambda: {"different.py": "0" * 64})
    result = verify_lab(lab_fixture[0])
    assert result["status"] == "VALID", result
    assert result["code_manifest_matches_current"] is False


@pytest.mark.parametrize("change,problem", [
    ("summary", "LAB_SAVED_SUMMARY_DISAGREES"),
    ("complete", "LAB_COMPLETION_INVALID"),
    ("failed", "LAB_FAILED_RUN_NOT_COMPLETE"),
])
def test_completion_and_report_are_evidence_not_unchecked_labels(lab_fixture, tmp_path, change, problem):
    root = _copy_run(lab_fixture, tmp_path)
    if change == "summary":
        summary = json.loads((root / "summary.json").read_text())
        summary["orders"] = 10
        (root / "summary.json").write_text(canonical(summary))
    elif change == "complete":
        (root / "COMPLETE").write_text("0" * 64)
    else:
        (root / "FAILED.json").write_text('{"reason":"interrupted"}')
    _assert_problem(root, problem)


def test_invalid_artifact_returns_named_mismatch_instead_of_crashing(tmp_path):
    root = tmp_path / "missing-run"
    root.mkdir()
    _assert_problem(root, "LAB_SOURCE_RESEARCH_REFUSED:RESEARCH_ARTIFACT_INVALID:")
