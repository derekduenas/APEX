"""One Linux shadow tick, persistent recovery evidence and a read-only report.

systemd owns the whole-process timeout and recurring schedule. This module
opens no broker connection and never treats repeated candidates as positions.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .capture import _write, capture_alpaca, http_transport, replay_capture
from .core import Config, Refused, code_manifest, digest
from .data import regular, session


@contextmanager
def runtime_lock(root):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / "runtime.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Refused("RUNTIME_ALREADY_ACTIVE") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _replace(path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    _write(temporary, value)
    os.replace(temporary, path)
    _sync_directory(path.parent)


def _publish(path, value):
    """Publish a complete immutable record without replacing prior evidence.

    A failed write retains its unique temporary file for inspection. Linking a
    fully flushed file makes the record visible atomically and refuses an
    existing destination, including a damaged one.
    """
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    _write(temporary, value)
    os.link(temporary, path)
    temporary.unlink()
    _sync_directory(path.parent)


def _sync_directory(directory):
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read(path):
    return json.loads(path.read_bytes())


def _runtime_record(path, kind):
    """Read report fields defensively; this is shape checking, not authentication."""
    try:
        value = _read(path)
        if not isinstance(value, dict):
            raise ValueError("RECORD_OBJECT_REQUIRED")
        # The report embeds the record. Reject non-finite numbers anywhere,
        # including otherwise unused fields, before computing its digest.
        digest(value)
        timestamp = value["started_ns" if kind == "STARTED" else "finished_ns"]
        if type(timestamp) is not int or timestamp <= 0:
            raise ValueError("RECORD_TIMESTAMP_INVALID")
        timestamp / 1e9  # also reject integers that overflow report conversion
        if kind == "FINISHED":
            if not isinstance(value["status"], str) or not value["status"]:
                raise ValueError("RECORD_STATUS_INVALID")
            reasons = value.get("reasons", [])
            if not isinstance(reasons, list) or any(not isinstance(reason, str) for reason in reasons):
                raise ValueError("RECORD_REASONS_INVALID")
            candidate = value.get("candidate")
            if candidate is not None:
                if (not isinstance(candidate, dict) or not isinstance(candidate["decision"], str)
                        or candidate["reason"] is not None and not isinstance(candidate["reason"], str)
                        or "quantity" not in candidate or "expected_net" not in candidate):
                    raise ValueError("RECORD_CANDIDATE_INVALID")
            forecast = value.get("forecast")
            if forecast is not None:
                if (not isinstance(forecast, dict) or not isinstance(forecast["models"], list)
                        or type(forecast["paths"]) is not int or type(forecast["training_returns"]) is not int):
                    raise ValueError("RECORD_FORECAST_INVALID")
                for model in forecast["models"]:
                    if (not isinstance(model, dict) or not isinstance(model["model"], str)
                            or not isinstance(model["status"], str)):
                        raise ValueError("RECORD_MODEL_INVALID")
        return value, None
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
        # Do not include arbitrary corrupt bytes or exception text in reports.
        return None, {"record": path.name, "reason": kind + "_RECORD_UNAVAILABLE",
                      "error_type": type(exc).__name__}


def load_settings(path):
    settings = _read(path)
    required = {"symbol", "feed", "round_lot_shares", "history_days", "max_pages", "request_timeout", "variance", "min_free_bytes"}
    if set(settings) != required:
        raise Refused("RUNTIME_CONFIG_KEYS_INVALID")
    if not isinstance(settings["symbol"], str) or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", settings["symbol"]):
        raise Refused("RUNTIME_SYMBOL_INVALID")
    if settings["feed"] != "sip" or type(settings["history_days"]) is not int or not 1 <= settings["history_days"] <= 9:
        raise Refused("RUNTIME_REQUIRES_SIP_AND_BOUNDED_HISTORY")
    if type(settings["round_lot_shares"]) is not int or not 1 <= settings["round_lot_shares"] <= 10000:
        raise Refused("RUNTIME_REQUIRES_OPERATOR_VERIFIED_LOT_SIZE")
    if type(settings["max_pages"]) is not int or not 1 <= settings["max_pages"] <= 10:
        raise Refused("RUNTIME_PAGE_BUDGET_INVALID")
    if type(settings["request_timeout"]) not in (float, int) or not 0 < settings["request_timeout"] <= 30:
        raise Refused("RUNTIME_REQUEST_DEADLINE_INVALID")
    if type(settings["min_free_bytes"]) is not int or settings["min_free_bytes"] < 64 * 1024**2:
        raise Refused("RUNTIME_DISK_RESERVE_INVALID")
    Config(symbol=settings["symbol"], variance=settings["variance"])
    return settings


def tick(root: Path, settings: dict, *, transport=http_transport, clock_ns=time.time_ns):
    """One capture and replay verification, with an exclusive process lock."""
    with runtime_lock(root):
        now_ns = clock_ns()
        active = root / "active.json"
        recovered = None
        recovered_unavailable = None
        if active.exists():
            try:
                previous = _read(active)
                if not isinstance(previous, dict) or not isinstance(previous["run"], str) or not previous["run"]:
                    raise ValueError("ACTIVE_RUN_INVALID")
            except (OSError, ValueError, TypeError, KeyError) as exc:
                raise Refused("ACTIVE_RUN_RECORD_UNAVAILABLE") from exc
            relative = Path(previous["run"])
            prior = root / relative
            if (relative.is_absolute() or ".." in relative.parts or len(relative.parts) != 3
                    or relative.parts[0] != "ticks" or not prior.is_dir()
                    or not prior.resolve().is_relative_to(root.resolve())):
                raise Refused("ACTIVE_RUN_PATH_INVALID")
            if not (prior / "finished.json").exists():
                _publish(prior / "finished.json", {"status": "INTERRUPTED", "finished_ns": now_ns,
                                                 "reason": "Prior process released its lock without a terminal record; original capture retained"})
                recovered = previous["run"]
            else:
                _, issue = _runtime_record(prior / "finished.json", "FINISHED")
                if issue:
                    recovered_unavailable = {"run": previous["run"], **issue}
                    if not (prior / "recovery.json").exists():
                        _publish(prior / "recovery.json", {"status": "UNAVAILABLE_RUNTIME_EVIDENCE",
                                                          "detected_ns": now_ns, **issue})
        day = session(now_ns / 1e9)
        run = root / "ticks" / day / (str(now_ns) + "-" + uuid.uuid4().hex[:12])
        run.mkdir(parents=True, exist_ok=False)
        opening = {"started_ns": now_ns, "settings": settings, "code": code_manifest(),
                   "mode": "SHADOW_ONLY", "recovered_interruption": recovered,
                   "recovered_unavailable_evidence": recovered_unavailable,
                   "capture_class": "HOST_CAPTURE_ATTEMPT" if transport is http_transport and clock_ns is time.time_ns else "SYNTHETIC_ACCEPTANCE"}
        _publish(run / "started.json", opening)
        _replace(active, {"run": str(run.relative_to(root))})
        try:
            if shutil.disk_usage(root).free < settings["min_free_bytes"]:
                result = {"status": "BLOCKED_DISK_RESERVE", "reason": "Evidence is retained; free space or archive verified captures before resuming"}
            elif not regular(now_ns / 1e9):
                result = {"status": "IDLE_OUTSIDE_REGULAR_CLOCK_WINDOW", "reason": "Weekday clock only; exchange holidays and early closes are not yet modeled"}
            else:
                start = datetime.fromtimestamp(now_ns / 1e9, timezone.utc) - timedelta(days=settings["history_days"])
                summary = capture_alpaca(run / "capture", history_start=start.isoformat(),
                                        config=Config(symbol=settings["symbol"], variance=settings["variance"]),
                                        feed=settings["feed"], round_lot_shares=settings["round_lot_shares"],
                                        max_pages=settings["max_pages"], timeout=settings["request_timeout"],
                                        transport=transport, clock_ns=clock_ns)
                comparison = replay_capture(run / "capture")
                _write(run / "comparison.json", comparison)
                shadows = sorted((run / "capture").glob("shadow-*.json"))
                shadow = _read(shadows[-1])["result"]
                forecast, candidate = shadow.get("forecast"), shadow.get("candidate")
                health = shadow["health"]
                reasons = []
                if summary["request_errors"]:
                    reasons.append("PROVIDER_REQUEST_ERRORS")
                if not summary["history_pagination_exhausted"]:
                    reasons.append("PARTIAL_HISTORY")
                if summary["rejected"] or health["bar_conflicts"] or health["quote_conflicts"]:
                    reasons.append("INPUT_REJECTIONS_OR_CONFLICTS")
                if health["internal_regular_session_gaps"]:
                    reasons.append("INTERNAL_BAR_GAPS")
                if shadow["status"] != "OBSERVED":
                    reasons.append(shadow["reason"])
                if health["latest_quote_age_from_event_s"] is None or health["latest_quote_age_from_event_s"] > 15:
                    reasons.append("QUOTE_MISSING_OR_STALE")
                result = {"status": "SHADOW_OBSERVED" if not reasons else "DEGRADED", "reasons": reasons,
                          "capture_class": summary["capture_class"], "comparison": comparison["status"],
                          "health": health, "decision_epoch": shadow["now"],
                          "forecast": {"id": forecast["forecast_id"], "models": forecast["fit_attempts"],
                                       "paths": forecast["path_count"], "training_returns": len(forecast["training_rows"])} if forecast else None,
                          "candidate": candidate, "capture_summary": summary}
            result.update(finished_ns=clock_ns(), orders=0, fills=0, pnl=None,
                          accounting_basis="SHADOW_CANDIDATES_ONLY_NO_TRADING_LEDGER", run=str(run.relative_to(root)))
            _publish(run / "finished.json", result)
        except BaseException as exc:
            # Arbitrary provider/OS exception text can contain a secret. Store
            # its class only; detailed credential-free capture markers remain.
            if not (run / "finished.json").exists():
                _publish(run / "finished.json", {"status": "FAILED", "error_type": type(exc).__name__, "finished_ns": clock_ns()})
            raise
        _replace(root / "latest.json", result)
        active.unlink()
        _sync_directory(root)
        return result


def report(root: Path, *, day=None, now=None):
    """Rebuild the daily view from durable ticks, never infer trades from signals."""
    now = time.time() if now is None else now
    day = day or session(now)
    if datetime.strptime(day, "%Y-%m-%d").date().isoformat() != day:
        raise Refused("REPORT_DATE_INVALID")
    statuses, decisions, reasons, models = Counter(), Counter(), Counter(), Counter()
    latest, latest_started = None, None
    incomplete, unavailable = [], []
    for run in sorted((root / "ticks" / day).glob("*")):
        start, start_issue = _runtime_record(run / "started.json", "STARTED")
        latest_started = None if start_issue else start["started_ns"] / 1e9
        latest = None
        item, finish_issue = (None, None)
        if (run / "finished.json").exists():
            item, finish_issue = _runtime_record(run / "finished.json", "FINISHED")
        issues = [issue for issue in (start_issue, finish_issue) if issue]
        if issues:
            unavailable.append({"run": str(run.relative_to(root)), "issues": issues})
            statuses["UNAVAILABLE_RUNTIME_EVIDENCE"] += 1
            reasons.update(issue["reason"] for issue in issues)
            continue
        if item is None:
            incomplete.append(str(run.relative_to(root)))
            statuses["INCOMPLETE"] += 1
            latest = None
            continue
        latest = item
        statuses[item["status"]] += 1
        for reason in item.get("reasons", []):
            reasons[reason] += 1
        candidate = item.get("candidate")
        if candidate:
            decisions[candidate["decision"]] += 1
            if candidate["reason"]:
                reasons[candidate["reason"]] += 1
        for model in (item.get("forecast") or {}).get("models", []):
            models[model["model"] + ":" + model["status"]] += 1
    age = None if latest_started is None else now - latest_started
    status = "UNAVAILABLE_RUNTIME_EVIDENCE" if unavailable else "NO_RUNS" if age is None else "CLOCK_REWIND" if age < 0 else "STALE_RUNTIME" if age > 300 else "INCOMPLETE" if latest is None else latest["status"]
    result = {"status": status, "market_date": day, "generated_epoch": now, "last_tick_age_s": age,
              "ticks": dict(statuses), "candidate_observations": dict(decisions), "models": dict(models),
              "reasons": dict(reasons), "incomplete_runs": incomplete, "latest": latest,
              "unavailable_runs": unavailable,
              "orders": 0, "fills": 0, "pnl": None,
              "scope": "Local shadow runtime only; no broker/account inventory; candidate observations are not unique trades"}
    result["report_digest"] = digest(result)
    return result


def report_text(value):
    latest = value["latest"] or {}
    candidate, forecast = latest.get("candidate"), latest.get("forecast")
    lines = [f"APEX shadow — {value['market_date']}", f"Status: {value['status']}",
             f"Input class: {latest.get('capture_class', 'NO_MARKET_CAPTURE_IN_LATEST_TICK')}",
             f"Last tick age: {value['last_tick_age_s']} seconds", f"Cycles: {value['ticks']}",
             f"Candidate observations: {value['candidate_observations']}", f"Models: {value['models']}",
             f"Reasons: {value['reasons']}"]
    if forecast:
        lines.append(f"Latest forecast: {forecast['paths']} paths from {forecast['training_returns']} returns")
    if candidate:
        lines.append(f"Latest candidate: {candidate['decision']} | shares {candidate['quantity']} | reason {candidate['reason']}")
        lines.append(f"Model expected net: {candidate['expected_net']} (uncalibrated estimate)")
    lines.extend(["Orders: 0 | fills: 0 | P&L: unavailable — shadow only", value["scope"]])
    return "\n".join(lines)
