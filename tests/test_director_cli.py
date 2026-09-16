"""Untrusted external proposals must keep their strict contract at the CLI."""
import json
import sys

import pytest

from apex.cli import main
from apex.core import Refused
from apex.fixtures import research_demo_document


@pytest.mark.parametrize("body", [b'{"action":"DEFER_MISSING_DATA","action":"RUN_PEER_DISLOCATION_STUDY"}', b' ' * 8193])
def test_cli_refuses_duplicate_or_oversized_proposal_before_research(tmp_path, monkeypatch, body):
    from dataclasses import asdict
    _, plan = research_demo_document()
    plan_path, proposal_path = tmp_path / "plan.json", tmp_path / "proposal.json"
    plan_path.write_text(json.dumps(asdict(plan)))
    proposal_path.write_bytes(body)
    out = tmp_path / "run"
    monkeypatch.setattr(sys, "argv", ["apex", "director", "--input", str(tmp_path / "unused.json"),
        "--plan", str(plan_path), "--out", str(out), "--symbol", "AAPL",
        "--market-symbol", "SPY", "--sector-symbol", "XLK", "--proposal", str(proposal_path)])
    with pytest.raises(Refused):
        main()
    assert not out.exists()
