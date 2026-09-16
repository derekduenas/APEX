"""A hash-valid prefix must never receive a successful completion marker."""
from dataclasses import replace
import json
from pathlib import Path

import pytest

from apex.core import Config, Refused, canonical, digest
from apex.data import session
from apex.engine import run
from apex.fixtures import demo_document, research_demo_document
from apex.ledger import Ledger, read_complete, read_verified
from apex.research import run_research
from apex.strategy_lab import LabPolicy, run_lab


def test_path_replacement_is_not_hidden_by_an_open_writer_descriptor(tmp_path):
    path = tmp_path / "ledger.jsonl"
    writer = Ledger(path)
    opening = writer.append("RUN_OPEN", 1, {})
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_bytes(path.read_bytes())
    replacement.replace(path)
    try:
        with pytest.raises(Refused, match="LEDGER_RETAINED_FILE_REPLACED"):
            writer.append("RUN_CLOSE", 2, {})
    finally:
        writer.close()
    assert read_verified(path) == [opening]  # Valid chain, demonstrably incomplete.


def test_same_inode_truncation_is_detected_before_next_append(tmp_path):
    path = tmp_path / "ledger.jsonl"
    writer = Ledger(path)
    opening = writer.append("RUN_OPEN", 1, {})
    prefix, original_inode = path.read_bytes(), path.stat().st_ino
    writer.append("SAMPLE", 2, {})
    path.write_bytes(prefix)
    assert path.stat().st_ino == original_inode
    try:
        with pytest.raises(Refused, match="LEDGER_RETAINED_SIZE_CHANGED"):
            writer.append("RUN_CLOSE", 3, {})
    finally:
        writer.close()
    assert read_verified(path) == [opening]


def test_final_retained_head_must_match_writer_even_after_valid_same_length_rehash(tmp_path, monkeypatch):
    path = tmp_path / "ledger.jsonl"
    writer = Ledger(path)
    writer.append("RUN_OPEN", 1, {})
    writer.append("RUN_CLOSE", 2, {"value": 1})
    original_size = path.stat().st_size
    original_close = writer.close

    def replace_after_close():
        original_close()
        rows = read_verified(path)
        rows[-1]["payload"]["value"] = 2
        rows[-1]["hash"] = digest({k: v for k, v in rows[-1].items() if k != "hash"})
        path.write_text("\n".join(canonical(r) for r in rows) + "\n")
        assert path.stat().st_size == original_size

    monkeypatch.setattr(writer, "close", replace_after_close)
    with pytest.raises(Refused, match="LEDGER_RETAINED_HEAD_DISAGREES_WITH_WRITER"):
        writer.verified_close(expected_last_kind="RUN_CLOSE", expected_epoch=2)
    assert read_complete(path, expected_last_kind="RUN_CLOSE", expected_epoch=2)


def test_correct_final_close_is_retained_and_returned(tmp_path):
    path = tmp_path / "ledger.jsonl"
    writer = Ledger(path)
    writer.append("RUN_OPEN", 1, {})
    closing = writer.append("RUN_CLOSE", 2, {})
    rows = writer.verified_close(expected_last_kind="RUN_CLOSE", expected_epoch=2)
    assert len(rows) == writer.seq == 2 and rows[-1] == closing
    assert rows[-1]["hash"] == writer.head


def test_a_prefix_completion_marker_does_not_make_the_prefix_complete(tmp_path):
    path = tmp_path / "ledger.jsonl"
    writer = Ledger(path)
    row = writer.append("RUN_OPEN", 1, {})
    writer.close()
    marker = tmp_path / "COMPLETE"
    marker.write_text(row["hash"])
    with pytest.raises(Refused, match="LEDGER_REQUIRED_CLOSE_MISSING_OR_INVALID"):
        read_complete(path, expected_last_kind="RUN_CLOSE", expected_epoch=2, completion_path=marker)


def test_replay_accounting_must_read_the_same_complete_ledger_as_its_writer(tmp_path, monkeypatch):
    from apex.ledger import reconstruct

    document, start, end = demo_document()
    observed = {}

    def truncate_before_accounting(path):
        rows = read_verified(path)
        assert rows[-1]["kind"] == "RUN_CLOSE"
        assert any(row["kind"] == "ENTRY" for row in rows)
        path.write_bytes(path.read_bytes().splitlines(keepends=True)[0])
        result = reconstruct(path)
        observed.update(result)
        return result

    monkeypatch.setattr("apex.engine.reconstruct", truncate_before_accounting)
    out = tmp_path / "replay"
    with pytest.raises(Refused, match="ACCOUNTING_RETAINED_HEAD_DISAGREES_WITH_WRITER"):
        run(canonical(document).encode(), out, start=start, end=end,
            config=Config(variance="ewma", paths=100))
    assert observed["status"] == "VALID"  # Accounting alone cannot certify completeness.
    assert (out / "FAILED.json").exists()
    assert not (out / "COMPLETE").exists() and not (out / "summary.json").exists()


@pytest.fixture(scope="module")
def small_research_input():
    document, plan = research_demo_document()
    days = sorted({session(row["event_epoch"]) for row in document["observations"]})[:4]
    document["observations"] = [row for row in document["observations"] if session(row["event_epoch"]) in days]
    starts = [min(row["event_epoch"] for row in document["observations"] if session(row["event_epoch"]) == day)
              for day in days]
    plan = replace(plan, start=starts[0], development_start=starts[1], holdout_start=starts[2],
                   end=starts[3] + 390 * 60, scan_minutes=60, minimum_training_labels=2,
                   training_window=20, minimum_comparison_sessions=1)
    return canonical(document).encode(), plan, Config(variance="ewma", paths=100)


class LoseCloseOnDisk(Ledger):
    """Fault injection after write/check returns, before the producer finalizes."""
    def append(self, kind, epoch, payload):
        row = super().append(kind, epoch, payload)
        if kind in ("RUN_CLOSE", "LAB_CLOSE"):
            self.path.write_bytes(self.path.read_bytes().splitlines(keepends=True)[0])
        return row


def test_real_research_runner_refuses_lost_retained_close_before_success(small_research_input, tmp_path, monkeypatch):
    raw, plan, config = small_research_input
    monkeypatch.setattr("apex.research.Ledger", LoseCloseOnDisk)
    out = tmp_path / "research"
    with pytest.raises(Refused, match="LEDGER_RETAINED_SIZE_CHANGED"):
        run_research(raw, out, plan=plan, config=config)
    assert (out / "FAILED.json").exists()
    assert not (out / "COMPLETE").exists() and not (out / "summary.json").exists()
    assert read_verified(out / "ledger.jsonl")[-1]["kind"] == "RUN_OPEN"
    # The actual model path ran; the refusal is at persistence, not a fixture
    # that skipped model execution or failed before producing anything.
    assert list(out.glob("*.npy"))


def test_real_lab_runner_refuses_own_lost_close_after_nested_forecast_finishes(small_research_input, tmp_path, monkeypatch):
    raw, plan, config = small_research_input
    monkeypatch.setattr("apex.strategy_lab.Ledger", LoseCloseOnDisk)
    out = tmp_path / "lab"
    with pytest.raises(Refused, match="LEDGER_RETAINED_SIZE_CHANGED"):
        run_lab(raw, out, plan=plan, config=config, policy=LabPolicy(bootstrap_samples=199))
    assert (out / "FAILED.json").exists() and not (out / "COMPLETE").exists()
    assert not (out / "summary.json").exists()
    assert read_verified(out / "ledger.jsonl")[-1]["kind"] == "RUN_OPEN"
    assert read_complete(out / "forecast" / "ledger.jsonl", expected_last_kind="RUN_CLOSE",
                         expected_epoch=plan.end, completion_path=out / "forecast" / "COMPLETE")


@pytest.mark.parametrize("fault,problem", [
    ("valid_prefix", "LEDGER_REQUIRED_CLOSE_MISSING_OR_INVALID"),
    ("wrong_marker", "LEDGER_COMPLETION_MARKER_DISAGREES"),
])
def test_outer_lab_checks_retained_source_after_real_producer_returns(small_research_input, tmp_path, monkeypatch, fault, problem):
    raw, plan, config = small_research_input

    def produce_then_lose_retained_evidence(input_path, out, **kwargs):
        summary = run_research(input_path, out, **kwargs)
        if fault == "valid_prefix":
            path = out / "ledger.jsonl"
            path.write_bytes(path.read_bytes().splitlines(keepends=True)[0])
            (out / "COMPLETE").write_text(read_verified(path)[-1]["hash"])
        else:
            (out / "COMPLETE").write_text("0" * 64)
        return summary

    monkeypatch.setattr("apex.strategy_lab.run_research", produce_then_lose_retained_evidence)
    out = tmp_path / "lab"
    with pytest.raises(Refused, match=problem):
        run_lab(raw, out, plan=plan, config=config, policy=LabPolicy(bootstrap_samples=199))
    assert (out / "FAILED.json").exists() and not (out / "COMPLETE").exists()
    assert not (out / "ledger.jsonl").exists()  # No downstream consumption starts.
