import copy
from dataclasses import replace
from decimal import Decimal
import json

import pytest

from apex.core import Config, Refused
from apex.fixtures import demo_document
from apex.paper_book import PaperAccountConfig, PaperBook
from apex import paper_runtime as runtime


def fixture(*, quotes=True, drift=.0003):
    doc, start, _ = demo_document(quotes=quotes, drift=drift)
    calendar = runtime.PaperSession(start - 300, start - 300 + 390 * 60, "XNYS", "SYNTHETIC_CALENDAR_FIXTURE", start - 3600)
    config = Config(variance="ewma", paths=100, training_returns=400, scan_minutes=15)
    return doc, start, calendar, config


def once(root, doc, now, calendar, config):
    return runtime._tick(root, doc, calendar=calendar, config=config, mode="SYNTHETIC_PAPER", now=now)


def book_document(root, config):
    with PaperBook(root / "paper.sqlite", config=PaperAccountConfig(starting_cash=config.starting_cash,
                   max_notional=config.max_notional, commission_per_share=config.commission_per_share,
                   minimum_commission=config.minimum_commission, max_quote_age_seconds=config.max_quote_age), mode="SYNTHETIC_PAPER") as book:
        return book._document()


def test_real_numerical_replay_records_later_quote_fills_and_profitable_pnl(tmp_path):
    doc, start, calendar, config = fixture()
    root = tmp_path / "positive"
    result = runtime.replay(doc, root, start=start, end=start + 900, session=calendar, config=config)
    account = result["account"]
    assert result["mode"] == "SYNTHETIC_PAPER" and result["verification"]["status"] == "VALID"
    assert account["fills"] == 2 and not account["positions"]
    assert Decimal(account["realized_net"]) > 0
    recorded = book_document(root, config)
    events = recorded["events"]
    fills = [e for e in events if e["kind"] == "FILL"]
    assert all(e["payload"]["execution_epoch"] > e["payload"]["decision_epoch"] for e in fills)
    assert fills[0]["payload"]["execution_epoch"] == start + 60
    assert fills[1]["payload"]["execution_epoch"] == start + 900
    decision = next(r for r in runtime._events(root) if r["kind"] == "DECISION")
    assert decision["payload"]["forecast"]["forecast_id"]
    assert decision["payload"]["intelligence"]["regime_id"]
    assert decision["payload"]["strategy_preview"]["status"] == "EVALUATED"
    assert decision["payload"]["outlook"]
    assert all(r["available"] <= start for r in decision["payload"]["forecast"]["training_rows"])


def test_unexpected_adverse_future_creates_real_paper_loss_not_forced_profit(tmp_path):
    doc, start, calendar, config = fixture()
    for row in doc["observations"]:
        if row["kind"] == "quote" and row["event_epoch"] > start:
            factor = 1 - .0005 * ((row["event_epoch"] - start) / 60)
            row["bid"] *= factor
            row["ask"] *= factor
    result = runtime.replay(doc, tmp_path / "loss", start=start, end=start + 900, session=calendar, config=config)
    assert result["account"]["fills"] == 2
    assert Decimal(result["account"]["realized_net"]) < 0
    assert result["verification"]["status"] == "VALID"


@pytest.mark.parametrize("quotes,drift,reason", [(False, .0003, "QUOTE_UNAVAILABLE_OR_CONFLICTING"),
                                                (True, -.0003, "AFTER_COST_MODEL_NOT_ELIGIBLE")])
def test_wait_and_missing_quotes_produce_zero_orders_and_fills(tmp_path, quotes, drift, reason):
    doc, start, calendar, config = fixture(quotes=quotes, drift=drift)
    result = once(tmp_path, doc, start, calendar, config)
    assert result["candidate"]["decision"] == "WAIT" and result["candidate"]["reason"] == reason
    assert result["account"]["orders"] == result["account"]["fills"] == 0


def test_restart_repeated_ticks_and_quotes_are_idempotent(tmp_path):
    doc, start, calendar, config = fixture()
    initial = once(tmp_path, doc, start, calendar, config)
    assert initial["account"]["orders"] == 1 and initial["account"]["fills"] == 0
    assert once(tmp_path, doc, start, calendar, config) == initial
    filled = once(tmp_path, doc, start + 60, calendar, config)
    assert filled["account"]["fills"] == 1
    assert once(tmp_path, doc, start + 60, calendar, config) == filled
    monitored = once(tmp_path, doc, start + 61, calendar, config)
    assert monitored["account"]["fills"] == 1 and monitored["account"]["orders"] == 1
    assert runtime.report(tmp_path)["verification"]["status"] == "VALID"


def test_due_exit_commits_before_new_forecast_failure(tmp_path, monkeypatch):
    doc, start, calendar, config = fixture()
    once(tmp_path, doc, start, calendar, config)
    once(tmp_path, doc, start + 60, calendar, config)
    prepared = once(tmp_path, doc, start + 899, calendar, config)
    assert prepared["account"]["pending_orders"][0]["side"] == "SELL"

    def failed_model(*args, **kwargs):
        stored = book_document(tmp_path, config)
        assert stored["state"]["fill_count"] == 2
        assert not stored["state"]["positions"]
        raise RuntimeError("synthetic model failure")

    monkeypatch.setattr(runtime, "predict", failed_model)
    result = once(tmp_path, doc, start + 900, calendar, config)
    assert result["status"] == "MODEL_FAILED_EXIT_SERVICE_RETAINED"
    assert result["account"]["fills"] == 2 and not result["account"]["positions"]


def test_missing_exit_quote_retains_obligation_and_fails_stale_marks(tmp_path):
    doc, start, calendar, config = fixture()
    once(tmp_path, doc, start, calendar, config)
    once(tmp_path, doc, start + 60, calendar, config)
    missing = copy.deepcopy(doc)
    missing["observations"] = [r for r in missing["observations"] if r["kind"] != "quote" or r["event_epoch"] <= start + 60]
    result = once(tmp_path, missing, start + 900, calendar, config)
    assert result["account"]["fills"] == 1 and result["account"]["positions"]
    assert result["account"]["unrealized_net"] is None
    assert result["account"]["pending_orders"][0]["side"] == "SELL"
    assert result["candidate"]["decision"] == "WAIT"


def test_explicit_early_close_blocks_new_entries_and_frozen_calendar_cannot_change(tmp_path):
    doc, start, _, config = fixture()
    early = runtime.PaperSession(start - 300, start + 600, "XNYS", "SYNTHETIC_EARLY_CLOSE", start - 3600)
    result = once(tmp_path, doc, start, early, config)
    assert result["status"] == "ENTRY_WINDOW_CLOSED_EXIT_SERVICE_ACTIVE"
    assert result["account"]["orders"] == 0
    result = once(tmp_path, doc, start + 660, early, config)
    assert result["status"] == "SESSION_CLOSED"
    changed = runtime.PaperSession(early.open_epoch, early.close_epoch + 60, "XNYS", early.calendar_source, early.known_at_epoch)
    with pytest.raises(Refused, match="IMMUTABLE_EVIDENCE"):
        once(tmp_path, doc, start + 720, changed, config)


def test_live_empty_denied_source_reports_noauth_and_rejects_synthetic_provenance(tmp_path, monkeypatch):
    doc, start, calendar, config = fixture()
    real_calendar = runtime.PaperSession(calendar.open_epoch, calendar.close_epoch, "XNYS", "SUPPLIED_EXCHANGE_CALENDAR_CAPTURE", calendar.known_at_epoch)
    monkeypatch.setattr(runtime.time, "time", lambda: start)
    denied = {"schema": "APEX_DATA_V1", "source": "READ_ONLY_PROVIDER_CAPTURE", "source_status": "NOT_ENTITLED", "observations": []}
    result = runtime.tick(tmp_path / "denied", denied, session=real_calendar, config=config)
    assert result["status"] == "BLOCKED_NO_AUTH"
    assert result["account"]["orders"] == result["account"]["fills"] == 0
    result = runtime.tick(tmp_path / "mismatch", doc, session=real_calendar, config=config)
    assert result["status"] == "BLOCKED_SYNTHETIC_DATA_IN_NON_SYNTHETIC_MODE"


def test_resource_budget_and_unknown_calendar_clock_refuse(tmp_path):
    doc, start, calendar, config = fixture()
    with pytest.raises(Refused, match="RESOURCE_BUDGET"):
        runtime.replay(doc, tmp_path / "oversized", start=start, end=start + 900, session=calendar,
                       config=Config(paths=100000, variance="ewma"))
    future = runtime.PaperSession(calendar.open_epoch, calendar.close_epoch, "XNYS", "SYNTHETIC_FIXTURE", start + 1)
    with pytest.raises(Refused, match="CALENDAR_NOT_KNOWN"):
        once(tmp_path / "unknown", doc, start, future, config)


@pytest.mark.parametrize("corruption", ["missing", "changed"])
def test_redundant_obligation_damage_cannot_postpone_primary_book_exit(tmp_path, corruption):
    doc, start, calendar, config = fixture()
    once(tmp_path, doc, start, calendar, config)
    once(tmp_path, doc, start + 60, calendar, config)
    sidecar = next((tmp_path / "obligations").glob("*.json"))
    if corruption == "missing":
        sidecar.unlink()
    else:
        value = json.loads(sidecar.read_bytes())
        value["due_epoch"] += 86400
        sidecar.write_text(json.dumps(value))
    prepared = once(tmp_path, doc, start + 899, calendar, config)
    assert prepared["activity"]["obligation_recoveries"][0]["due_epoch"] == start + 900
    exited = once(tmp_path, doc, start + 900, calendar, config)
    assert exited["account"]["fills"] == 2 and not exited["account"]["positions"]


def test_live_report_uses_current_clock_and_labels_stale_runtime(tmp_path, monkeypatch):
    _, start, calendar, config = fixture()
    real = runtime.PaperSession(calendar.open_epoch, calendar.close_epoch, "XNYS", "SUPPLIED_EXCHANGE_CALENDAR_CAPTURE", calendar.known_at_epoch)
    monkeypatch.setattr(runtime.time, "time", lambda: start)
    runtime.tick(tmp_path, {"schema": "APEX_DATA_V1", "source_status": "NOT_ENTITLED", "observations": []}, session=real, config=config)
    monkeypatch.setattr(runtime.time, "time", lambda: start + 61)
    result = runtime.report(tmp_path)
    assert result["status"] == "STALE_RUNTIME"
    assert result["runtime_health"]["latest_tick_age_seconds"] == 61
    assert result["latest_tick"]["status"] == "BLOCKED_NO_AUTH"


@pytest.mark.parametrize("late_quote_seconds", [60, 90])
def test_short_horizon_pending_entry_expires_before_exit_target(tmp_path, late_quote_seconds):
    doc, start, calendar, config = fixture(drift=.001)
    config = replace(config, horizon_minutes=1)
    decision_quote = next(r for r in doc["observations"] if r["kind"] == "quote" and r["event_epoch"] == start)
    doc["observations"] = [r for r in doc["observations"] if r["kind"] != "quote" or r["event_epoch"] <= start]
    # An otherwise executable later quote first appears at/after the fixed
    # forecast target. It must not open exposure after its exit is already due.
    doc["observations"].append({**decision_quote, "event_epoch": start + late_quote_seconds,
                                "available_epoch": start + late_quote_seconds})
    initial = once(tmp_path, doc, start, calendar, config)
    assert initial["account"]["pending_orders"][0]["expires_epoch"] == start + 59
    assert initial["account"]["fills"] == 0
    after = once(tmp_path, doc, start + late_quote_seconds, calendar, config)
    assert after["account"]["fills"] == 0 and not after["account"]["positions"]
    assert not after["account"]["pending_orders"]
    assert after["activity"]["expired_before_quote"]
