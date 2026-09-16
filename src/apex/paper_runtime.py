"""File-fed paper controller with durable decisions and independent exit service.

The numerical policy is an unvalidated experiment. A PAPER fill is a quote-based
simulation in a persistent account, never a broker fill or proof of market edge.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
import json
from pathlib import Path
import time

from .core import Config, Refused, code_manifest, digest, finite, money
from .data import normalize, session as market_day, twin
from .decision import evaluate, quote_at
from .forecast import predict
from .paper_book import PaperAccountConfig, PaperBook
from .paper_outlook import path_outlook
from .regimes import classify_regime
from .runtime import _publish, runtime_lock
from .shadow import _strategy_preview


MODES = {"SYNTHETIC_PAPER", "RECORDED_PAPER", "LIVE_PAPER"}
POLICY = {
    "schema": "PAPER_RUNTIME_POLICY_V1",
    "strategy": "SHRUNK_EMPIRICAL_MEAN_EXPERIMENT_V1",
    "strategy_status": "UNVALIDATED_PAPER_EXPERIMENT_NO_PEER_STRATEGY_PROMOTION",
    "execution": "LATER_FRESH_QUOTE_FULL_DISPLAYED_SIZE_OR_NO_FILL",
    "entry_limit_fraction": .001,
    "entry_window_seconds": 120,
    "entry_expiry_rule": "MIN_DECISION_PLUS_ENTRY_WINDOW_OR_EXIT_DUE_MINUS_MINIMUM_LATENCY; EXPIRE_BEFORE_QUOTE_PROCESSING",
    "maximum_paths": 2000,
    "maximum_training_returns": 2000,
    "maximum_horizon_minutes": 60,
    "maximum_observations": 100000,
    "maximum_document_bytes": 128 * 1024 * 1024,
    "live_heartbeat_stale_seconds": 60,
    "authority": "LOCAL_PAPER_ACCOUNT_ONLY_NO_BROKER_OR_REAL_MONEY",
    "exit_priority": "SERVICE_DUE_EXITS_BEFORE_MODELS_OR_NEW_ENTRY_DECISIONS",
}


@dataclass(frozen=True)
class PaperSession:
    open_epoch: float
    close_epoch: float
    calendar_id: str
    calendar_source: str
    known_at_epoch: float

    def __post_init__(self):
        if not all(finite(v) for v in (self.open_epoch, self.close_epoch, self.known_at_epoch)):
            raise Refused("PAPER_CALENDAR_INVALID_CLOCK")
        if not self.open_epoch < self.close_epoch or self.close_epoch - self.open_epoch > 8 * 3600:
            raise Refused("PAPER_CALENDAR_INVALID_SESSION")
        if any(not isinstance(v, str) or not v.strip() for v in (self.calendar_id, self.calendar_source)):
            raise Refused("PAPER_CALENDAR_SOURCE_REQUIRED")
        if self.open_epoch % 60 or self.close_epoch % 60:
            raise Refused("PAPER_CALENDAR_MINUTE_ALIGNMENT_REQUIRED")
        try:
            if market_day(self.open_epoch) != market_day(self.close_epoch):
                raise Refused("PAPER_CALENDAR_ONE_LOCAL_SESSION_REQUIRED")
        except (ValueError, OverflowError, OSError) as exc:
            raise Refused("PAPER_CALENDAR_INVALID_SESSION_DATE") from exc

    def record(self):
        return {**asdict(self), "session_date": market_day(self.open_epoch),
                "calendar_contract_id": digest(asdict(self)),
                "scope": "Explicit supplied session calendar; no weekday inference or independent source attestation."}


def _calendar(value):
    if isinstance(value, PaperSession):
        return value
    if isinstance(value, (str, Path)):
        value = json.loads(Path(value).read_bytes())
    if not isinstance(value, dict):
        raise Refused("PAPER_CALENDAR_DOCUMENT_REQUIRED")
    try:
        return PaperSession(**value)
    except TypeError as exc:
        raise Refused("PAPER_CALENDAR_KEYS_INVALID") from exc


def _config(value):
    config = value if isinstance(value, Config) else Config(**value)
    if (config.paths > POLICY["maximum_paths"] or config.training_returns > POLICY["maximum_training_returns"]
            or config.horizon_minutes > POLICY["maximum_horizon_minutes"] or config.scan_minutes < 1):
        raise Refused("PAPER_FORECAST_RESOURCE_BUDGET_EXCEEDED")
    return config


def _document(value):
    if isinstance(value, (str, Path)):
        path = Path(value)
        if path.stat().st_size > POLICY["maximum_document_bytes"]:
            raise Refused("PAPER_SOURCE_DOCUMENT_TOO_LARGE")
        value = json.loads(path.read_bytes())
    if not isinstance(value, dict) or not isinstance(value.get("observations"), list):
        raise Refused("PAPER_SOURCE_DOCUMENT_INVALID")
    if len(value["observations"]) > POLICY["maximum_observations"]:
        raise Refused("PAPER_SOURCE_OBSERVATION_BUDGET_EXCEEDED")
    # Strict canonical JSON also refuses non-finite document metadata.
    digest(value)
    return value


def _immutable(path, value):
    if path.exists():
        if json.loads(path.read_bytes()) != value:
            raise Refused("PAPER_IMMUTABLE_EVIDENCE_DISAGREES:" + path.name)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    _publish(path, value)


def _event(root, now, kind, payload):
    content = {"epoch": now, "kind": kind, "payload": payload}
    record = {**content, "event_id": digest(content)}
    _immutable(root / "events" / market_day(now) / (str(int(now * 1e6)).zfill(20) + "-" + kind + "-" + record["event_id"] + ".json"), record)
    return record


def _events(root, *, day=None, kinds=None):
    records = []
    for path in sorted((root / "events").glob((day or "*") + "/*.json")):
        if kinds is not None and not any("-" + kind + "-" in path.name for kind in kinds):
            continue
        value = json.loads(path.read_bytes())
        if (set(value) != {"epoch", "kind", "payload", "event_id"}
                or value["event_id"] != digest({k: v for k, v in value.items() if k != "event_id"})):
            raise Refused("PAPER_RUNTIME_EVIDENCE_CORRUPT")
        records.append(value)
    return records


def _source_problem(document, rows, rejected, mode):
    from .admissibility import admissibility
    from .data import collection_problem, decision_only
    if decision_only(document):
        return "BLOCKED_DECISION_SNAPSHOTS_NOT_EXECUTION_EVIDENCE"
    if collection_problem(document):
        return "BLOCKED_INCOMPLETE_QUOTE_COLLECTION"
    if admissibility(document, rows)["input_class"] == "MIXED_SYNTHETIC_AND_RECORDED_INPUT":
        return "BLOCKED_MIXED_SYNTHETIC_AND_RECORDED_INPUT"
    status = document.get("source_status", document.get("status", ""))
    if status in ("NO_AUTH", "UNAUTHORIZED", "NOT_ENTITLED", "SUBSCRIPTION_DENIED", "BLOCKED_NO_AUTH"):
        return "BLOCKED_NO_AUTH"
    if status in ("FAILED", "BLOCKED", "BLOCKED_SOURCE", "BLOCKED_NO_MARKET_DATA"):
        return "BLOCKED_SOURCE"
    if not rows:
        return "BLOCKED_NO_MARKET_DATA"
    bases = {row["availability_basis"] for row in rows}
    synthetic = "SYNTHETIC_CLOCK" in bases or "SYNTHETIC" in str(document.get("source", "")).upper()
    if mode == "SYNTHETIC_PAPER" and bases != {"SYNTHETIC_CLOCK"}:
        return "BLOCKED_SYNTHETIC_SOURCE_MODE_MISMATCH"
    if mode != "SYNTHETIC_PAPER" and synthetic:
        return "BLOCKED_SYNTHETIC_DATA_IN_NON_SYNTHETIC_MODE"
    if mode == "LIVE_PAPER" and bases != {"MEASURED_RECEIPT"}:
        return "BLOCKED_LIVE_REQUIRES_MEASURED_RECEIPT"
    if rejected:
        return "BLOCKED_INPUT_ROWS_REJECTED"
    return None


def _read_contract(root):
    try:
        return json.loads((root / "contract.json").read_bytes())
    except (OSError, ValueError, TypeError) as exc:
        raise Refused("PAPER_RUNTIME_CONTRACT_UNAVAILABLE") from exc


def _open_book(root, contract):
    return PaperBook(root / "paper.sqlite", config=PaperAccountConfig(**contract["account_config"]), mode=contract["mode"])


def _service(book, root, *, now, calendar, quote):
    """Persist and service obligations without invoking any forecast or director."""
    expired = book.expire_orders(now=now)
    recoveries = []
    before = book.snapshot(now=now)
    is_open = calendar.open_epoch <= now <= calendar.close_epoch
    for order in before["pending_orders"]:
        if order["side"] == "BUY" and not is_open:
            book.cancel_order(order["order_id"], now=now, reason="SESSION_CLOSED")
    if is_open:
        before = book.snapshot(now=now)
        for position in before["positions"]:
            obligation = position["entry_evidence"]
            if not finite(obligation.get("due_epoch")):
                raise Refused("PAPER_PERSISTED_EXIT_OBLIGATION_INVALID")
            expected_copy = {"entry_order_id": position["entry_order_id"], "due_epoch": obligation["due_epoch"],
                             "decision_id": position["entry_decision_id"]}
            try:
                copy = json.loads((root / "obligations" / (position["entry_order_id"] + ".json")).read_bytes())
                copy_status = "REDUNDANT_COPY_DISAGREES" if copy != expected_copy else None
            except (OSError, ValueError, TypeError):
                copy_status = "REDUNDANT_COPY_UNAVAILABLE"
            if copy_status:
                recoveries.append({"entry_order_id": position["entry_order_id"], "status": copy_status,
                                   "action": "EXIT_DUE_RECOVERED_FROM_PRIMARY_BOOK_EVIDENCE", "due_epoch": obligation["due_epoch"]})
            if now < obligation["due_epoch"] - book.config.min_latency_seconds:
                continue
            if any(order["symbol"] == position["symbol"] for order in before["pending_orders"]):
                continue
            # The full exit is renewed if an earlier attempt expired; exposure
            # remains real in the paper account until an actual simulated fill.
            exit_id = digest({"entry_order_id": position["entry_order_id"], "attempt_epoch": now})
            evidence = {"reason": "PERSISTED_TIME_EXIT_OBLIGATION", "entry_order_id": position["entry_order_id"],
                        "due_epoch": obligation["due_epoch"], "calendar_contract_id": calendar.record()["calendar_contract_id"]}
            book.submit_order("exit-" + exit_id, symbol=position["symbol"], side="SELL", quantity=position["quantity"],
                              decision_epoch=now, decision_id=exit_id, evidence=evidence)
        if quote:
            result = book.process_quote(quote, now=now)
            return {**result, "expired_before_quote": expired, "obligation_recoveries": recoveries}
    return {"fills": [], "waits": [], "expired_before_quote": expired, "obligation_recoveries": recoveries,
            "quote_service": "SESSION_CLOSED" if not is_open else "QUOTE_UNAVAILABLE"}


def _tick(root, document, *, calendar, config, mode, now, entry_cutoff=None, account_config=None):
    root = Path(root)
    if mode not in MODES or not finite(now) or now < 0:
        raise Refused("PAPER_TICK_MODE_OR_CLOCK_INVALID")
    if calendar.known_at_epoch > now:
        raise Refused("PAPER_CALENDAR_NOT_KNOWN_AT_TICK")
    if mode != "SYNTHETIC_PAPER" and "SYNTHETIC" in calendar.calendar_source.upper():
        raise Refused("PAPER_REAL_MODE_REQUIRES_NON_SYNTHETIC_CALENDAR")
    account = account_config or PaperAccountConfig(starting_cash=config.starting_cash, max_notional=config.max_notional,
                                 commission_per_share=config.commission_per_share, minimum_commission=config.minimum_commission,
                                 max_quote_age_seconds=config.max_quote_age, feed="SYNTHETIC" if mode == "SYNTHETIC_PAPER" else "sip")
    if not isinstance(account, PaperAccountConfig) or any(getattr(account, key) != getattr(config, key)
            for key in ("starting_cash", "max_notional", "commission_per_share", "minimum_commission")):
        raise Refused("PAPER_MODEL_ACCOUNT_CONFIG_DISAGREES")
    if account.order_ttl_seconds != POLICY["entry_window_seconds"] or account.min_latency_seconds != 1:
        raise Refused("PAPER_FROZEN_EXECUTION_CLOCK_DISAGREES")
    contract = {"schema": "APEX_PAPER_RUNTIME_V1", "mode": mode, "config": config.record(),
                "account_config": asdict(account), "policy": POLICY,
                "source_adapter": "READ_ONLY_APEX_DATA_V1_FILE", "authority": POLICY["authority"]}
    with runtime_lock(root / "controller"):
        _immutable(root / "contract.json", contract)
        _immutable(root / "sessions" / (market_day(calendar.open_epoch) + ".json"), calendar.record())
        input_id = digest(document)
        # Live exit servicing does not reread historical forecast arrays. Only
        # today's small scheduler records precede account service.
        records = _events(root, day=market_day(now), kinds={"SCAN_STARTED", "TICK_STARTED", "TICK_COMPLETE"})
        completed = [r for r in records if r["kind"] == "TICK_COMPLETE" and r["epoch"] == now]
        if completed:
            if completed[-1]["payload"]["input_id"] != input_id:
                raise Refused("PAPER_TICK_CLOCK_REUSED_WITH_DIFFERENT_INPUT")
            return completed[-1]["payload"]
        if records and now < max(r["epoch"] for r in records):
            raise Refused("PAPER_RUNTIME_CLOCK_REWIND")
        _immutable(root / "inputs" / (input_id + ".json"), document)
        _event(root, now, "TICK_STARTED", {"input_id": input_id, "calendar_contract_id": calendar.record()["calendar_contract_id"],
                                          "clock_basis": "SYSTEM_TIME" if mode == "LIVE_PAPER" else "EXPLICIT_CHRONOLOGICAL_REPLAY", "code": code_manifest()})
        try:
            rows, rejected = normalize(document)
            source_problem = _source_problem(document, rows, rejected, mode)
        except Refused:
            rows, rejected = [], []
            source_problem = "BLOCKED_INVALID_INPUT_DOCUMENT"
        quote, quote_problem = quote_at(rows, now, config)
        # Invalid bars block entries but a separately valid quote may still
        # service an existing exit. Wrong provenance never enters the book.
        service_quote = quote if source_problem in (None, "BLOCKED_INPUT_ROWS_REJECTED") else None
        with _open_book(root, contract) as book:
            try:
                activity = _service(book, root, now=now, calendar=calendar, quote=service_quote)
            except Refused as exc:
                activity = {"fills": [], "reason": str(exc)}
                source_problem = "BLOCKED_QUOTE_ACCOUNT_CONTRACT"
            status, candidate, evidence_id = "WAIT", None, None
            last_scan = max((r["epoch"] for r in records if r["kind"] == "SCAN_STARTED"), default=float("-inf"))
            cutoff = min(calendar.close_epoch - config.horizon_minutes * 60 - POLICY["entry_window_seconds"],
                         entry_cutoff if entry_cutoff is not None else float("inf"))
            if source_problem:
                status = source_problem
            elif not calendar.open_epoch <= now < calendar.close_epoch:
                status = "SESSION_CLOSED"
            elif now > cutoff:
                status = "ENTRY_WINDOW_CLOSED_EXIT_SERVICE_ACTIVE"
            elif now < last_scan + config.scan_minutes * 60:
                status = "MONITORING_BETWEEN_FORECASTS"
            else:
                _event(root, now, "SCAN_STARTED", {"input_id": input_id, "cadence_minutes": config.scan_minutes})
                try:
                    state, returns = twin(rows, now, config.symbol)
                    forecast, paths = predict(returns, state, config)
                    intelligence = classify_regime(rows, now=now, symbol=config.symbol)
                    preview = _strategy_preview(intelligence, state, forecast, paths, now=now, config=config)
                    account_state = book.snapshot(now=now)
                    candidate = evaluate(forecast, paths, quote, quote_problem, cash=money(account_state["cash"]),
                                         position_open=bool(account_state["positions"] or account_state["pending_orders"]), config=config)
                    candidate.update(authority=POLICY["authority"], policy_status=POLICY["strategy_status"])
                    outlook = path_outlook(forecast, paths, quote, quantity=candidate["quantity"], config=config)
                    decision = {"snapshot": state, "forecast": forecast, "paths": paths.tolist(), "candidate": candidate,
                                "intelligence": intelligence, "strategy_preview": preview, "outlook": outlook, "input_id": input_id,
                                "policy_id": digest(POLICY), "calendar_contract_id": calendar.record()["calendar_contract_id"],
                                "director_role": "NO_LLM_POLICY_SELECTION_CONSUMED_BY_THIS_FIXED_NUMERICAL_EXPERIMENT"}
                    recorded = _event(root, now, "DECISION", decision)
                    evidence_id = recorded["event_id"]
                    evidence = {"runtime_event_id": evidence_id, "candidate": candidate, "forecast_id": forecast["forecast_id"],
                                "due_epoch": now + config.horizon_minutes * 60, "policy_id": digest(POLICY)}
                    book.record_decision(evidence_id, now=now, payload=evidence)
                    if candidate["decision"] == "EXPERIMENTAL_LONG":
                        order_id = "entry-" + evidence_id
                        _immutable(root / "obligations" / (order_id + ".json"),
                                   {"entry_order_id": order_id, "due_epoch": evidence["due_epoch"], "decision_id": evidence_id})
                        book.submit_order(order_id, symbol=config.symbol, side="BUY", quantity=candidate["quantity"],
                                          decision_epoch=now, decision_id=evidence_id, evidence=evidence,
                                          expires_epoch=min(now + POLICY["entry_window_seconds"],
                                                            evidence["due_epoch"] - account.min_latency_seconds),
                                          limit_price=str(Decimal(str(quote["ask"])) * Decimal("1.001")))
                        status = "PAPER_ORDER_PENDING_LATER_QUOTE"
                    else:
                        status = "WAIT"
                except Refused as exc:
                    status = "FORECAST_OR_ADMISSION_REFUSED"
                    _event(root, now, "SCAN_REFUSED", {"reason": str(exc)})
                except Exception as exc:
                    # Exit service has already committed. Do not expose arbitrary
                    # provider/error text or let model failure undo that service.
                    status = "MODEL_FAILED_EXIT_SERVICE_RETAINED"
                    _event(root, now, "SCAN_FAILED", {"error_type": type(exc).__name__})
            result = {"schema": "APEX_PAPER_TICK_V1", "mode": mode, "now": now, "status": status, "input_id": input_id,
                      "candidate": candidate, "decision_evidence_id": evidence_id, "activity": activity,
                      "source_problem": source_problem, "quote_problem": quote_problem, "rejected_rows": rejected,
                      "account": book.snapshot(now=now), "authority": POLICY["authority"],
                      "readiness": "FILE_FED_PAPER_EXPERIMENT_NOT_BROKER_EXECUTION_OR_VALIDATED_EDGE"}
            _event(root, now, "TICK_COMPLETE", result)
            return result


def tick(root: Path, document, *, session, config: Config | None = None, account_config=None, mode="LIVE_PAPER"):
    """Consume one read-only capture using the system clock; no network or broker."""
    root = Path(root)
    if config is None:
        config = Config(**_read_contract(root)["config"]) if (root / "contract.json").exists() else Config()
    if mode != "LIVE_PAPER":
        raise Refused("PAPER_TICK_REQUIRES_LIVE_MODE_USE_REPLAY_FOR_RECORDED_CLOCKS")
    return _tick(root, _document(document), calendar=_calendar(session), config=_config(config), mode=mode, now=time.time(), account_config=account_config)


def replay(document, out: Path, *, start: float, end: float, session, config: Config | None = None):
    """Replay one supplied calendar session into a new persistent PAPER account."""
    calendar, config, document = _calendar(session), _config(config or Config()), _document(document)
    if not all(finite(v) for v in (start, end)) or not calendar.open_epoch <= start < end <= calendar.close_epoch:
        raise Refused("PAPER_REPLAY_RANGE_OUTSIDE_SUPPLIED_CALENDAR")
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    rows, _ = normalize(document)
    mode = "SYNTHETIC_PAPER" if any(r["availability_basis"] == "SYNTHETIC_CLOCK" for r in rows) else "RECORDED_PAPER"
    scans = []
    scan = start
    while scan <= end - config.horizon_minutes * 60:
        scans.append(scan)
        scan += config.scan_minutes * 60
    times = {start, end, *scans}
    times.update(r["available_epoch"] for r in rows if start <= r["available_epoch"] <= end)
    for scan in scans:
        times.update(t for t in (scan + POLICY["entry_window_seconds"], scan + config.horizon_minutes * 60 - 1,
                                scan + config.horizon_minutes * 60) if start <= t <= end)
    for now in sorted(times):
        _tick(out, document, calendar=calendar, config=config, mode=mode, now=now,
              entry_cutoff=end - config.horizon_minutes * 60)
    result = report(out, now=end)
    _immutable(out / "summary.json", result)
    return result


def report(root: Path, *, now: float | None = None):
    """Reconstruct PAPER P&L and orders from the persistent accounting reader."""
    root, explicit_now = Path(root), now is not None
    now = time.time() if now is None else now
    contract = _read_contract(root)
    records = _events(root)
    completed = [r for r in records if r["kind"] == "TICK_COMPLETE"]
    latest = max(completed, key=lambda r: r["epoch"])["payload"] if completed else None
    if contract["mode"] != "LIVE_PAPER" and latest and not explicit_now:
        now = latest["now"]
    with _open_book(root, contract) as book:
        verification = book.verify()
        if verification["status"] != "VALID":
            raise Refused("PAPER_BOOK_RECONSTRUCTION_FAILED")
        age = now - latest["now"] if latest else None
        stale = contract["mode"] == "LIVE_PAPER" and (age is None or age > POLICY["live_heartbeat_stale_seconds"])
        return {"schema": "APEX_PAPER_RUNTIME_REPORT_V1", "mode": contract["mode"], "as_of_epoch": now,
                "status": "STALE_RUNTIME" if stale else latest["status"] if latest else "NO_COMPLETED_TICKS", "account": book.snapshot(now=now),
                "runtime_health": {"latest_tick_age_seconds": age, "stale": stale,
                                   "basis": "COMPLETED_TICK_HEARTBEAT_ONLY_NOT_SERVICE_OR_PROVIDER_ATTESTATION"},
                "verification": verification, "latest_tick": latest, "completed_ticks": len(completed),
                "decisions": sum(r["kind"] == "DECISION" for r in records),
                "orders_and_fills": "SIMULATED_PAPER_ACCOUNT_RECORDS_ONLY", "authority": POLICY["authority"],
                "limitations": [POLICY["strategy_status"], "No authenticated broker fills or independently validated market edge",
                                "Immutable file and database hashes establish internal consistency, not external authentication"]}
