"""Subscription planner boundary through the actual director and completion reader."""
import json

import pytest

from apex import codex_planner, director
from apex.core import Config, Refused, canonical, digest
from apex.peer_fixtures import peer_demo_document


def _source(tmp_path):
    document, plan = peer_demo_document()
    path = tmp_path / "input.json"
    path.write_text(canonical(document))
    return path, plan


def _planned(context, symbols, model):
    return {"proposal": {"schema": "APEX_RESEARCH_PROPOSAL_V1", **symbols,
            "action": "DEFER_MISSING_DATA", "rationale": "Synthetic transport test of the subscription boundary."},
            "model": model, "provenance": "CODEX_CHATGPT_CLI_NOT_INDEPENDENTLY_AUTHENTICATED",
            "request_digest": digest(codex_planner.make_codex_request(context, symbols, model)),
            "response_sha256": "a" * 64}


def test_subscription_proposal_is_retained_and_reconstructed(tmp_path, monkeypatch):
    path, plan = _source(tmp_path)
    monkeypatch.setattr(codex_planner, "request_codex_plan", _planned)
    monkeypatch.setattr(director, "request_plan", lambda *_: pytest.fail("No API-billed fallback"))
    out = tmp_path / "run"
    result = director.run_director(path, out, plan=plan, config=Config(symbol="AAPL", paths=100),
        market_symbol="SPY", sector_symbol="XLK", codex_model="synthetic-subscription-model")
    assert result["status"] == "WAIT" and result["research_attempts"] == 0
    assert result["planner_provenance"] == "CODEX_CHATGPT_CLI_NOT_INDEPENDENTLY_AUTHENTICATED"
    assert result["orders"] == result["fills"] == 0
    assert director.verify_director(out)["status"] == "VALID"
    retained = json.loads((out / "planner.json").read_text())
    retained["request_digest"] = "b" * 64
    (out / "planner.json").write_text(canonical(retained))
    assert director.verify_director(out)["status"] == "MISMATCH"


def test_subscription_failure_does_not_use_other_planners(tmp_path, monkeypatch):
    path, plan = _source(tmp_path)
    def unavailable(*_):
        raise Refused("CODEX_ISOLATION_UNVERIFIED")
    monkeypatch.setattr(codex_planner, "request_codex_plan", unavailable)
    monkeypatch.setattr(director, "request_plan", lambda *_: pytest.fail("No API-billed fallback"))
    out = tmp_path / "run"
    with pytest.raises(Refused, match="CODEX_ISOLATION_UNVERIFIED"):
        director.run_director(path, out, plan=plan, config=Config(symbol="AAPL", paths=100),
            market_symbol="SPY", sector_symbol="XLK", codex_model="synthetic-subscription-model")
    assert (out / "FAILED.json").exists() and not (out / "COMPLETE").exists()
    assert not (out / "laboratory").exists()


def test_subscription_and_api_selection_are_mutually_exclusive(tmp_path):
    path, plan = _source(tmp_path)
    with pytest.raises(Refused, match="CHOOSE_EXTERNAL"):
        director.run_director(path, tmp_path / "run", plan=plan, config=Config(symbol="AAPL"),
            market_symbol="SPY", sector_symbol="XLK", model="api-model", codex_model="subscription-model")
