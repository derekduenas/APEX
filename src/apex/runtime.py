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
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read(path):
    return json.loads(path.read_bytes())


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
        if active.exists():
            previous = _read(active)
            relative = Path(previous["run"])
            if relative.is_absolute() or ".." in relative.parts:
                raise Refused("ACTIVE_RUN_PATH_INVALID")
            prior = root / relative
            if not (prior / "finished.json").exists():
                _write(prior / "finished.json", {"status": "INTERRUPTED", "finished_ns": now_ns,
                                               "reason": "Prior process released its lock without a terminal record; original capture retained"})
                recovered = previous["run"]
        day = session(now_ns / 1e9)
        run = root / "ticks" / day / (str(now_ns) + "-" + uuid.uuid4().hex[:12])
        run.mkdir(parents=True, exist_ok=False)
        opening = {"started_ns": now_ns, "settings": settings, "code": code_manifest(),
                   "mode": "SHADOW_ONLY", "recovered_interruption": recovered,
                   "capture_class": "HOST_CAPTURE_ATTEMPT" if transport is http_transport and clock_ns is time.time_ns else "SYNTHETIC_ACCEPTANCE"}
        _write(run / "started.json", opening)
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
            _write(run / "finished.json", result)
        except BaseException as exc:
            # Arbitrary provider/OS exception text can contain a secret. Store
            # its class only; detailed credential-free capture markers remain.
            _write(run / "finished.json", {"status": "FAILED", "error_type": type(exc).__name__, "finished_ns": clock_ns()})
            raise
        _replace(root / "latest.json", result)
        active.unlink()
        return result


def report(root: Path, *, day=None, now=None):
    """Rebuild the daily view from durable ticks, never infer trades from signals."""
    now = time.time() if now is None else now
    day = day or session(now)
    if datetime.strptime(day, "%Y-%m-%d").date().isoformat() != day:
        raise Refused("REPORT_DATE_INVALID")
    statuses, decisions, reasons, models = Counter(), Counter(), Counter(), Counter()
    latest, latest_started = None, None
    incomplete = []
    for run in sorted((root / "ticks" / day).glob("*")):
        start = _read(run / "started.json")
        latest_started = start["started_ns"] / 1e9
        if not (run / "finished.json").exists():
            incomplete.append(str(run.relative_to(root)))
            statuses["INCOMPLETE"] += 1
            latest = None
            continue
        item = _read(run / "finished.json")
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
    status = "NO_RUNS" if age is None else "CLOCK_REWIND" if age < 0 else "STALE_RUNTIME" if age > 300 else "INCOMPLETE" if latest is None else latest["status"]
    result = {"status": status, "market_date": day, "generated_epoch": now, "last_tick_age_s": age,
              "ticks": dict(statuses), "candidate_observations": dict(decisions), "models": dict(models),
              "reasons": dict(reasons), "incomplete_runs": incomplete, "latest": latest,
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
