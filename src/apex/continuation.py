"""Frozen opening-to-closing diagnostic. Never an execution or admission engine."""
import csv
import hashlib
import io
import json
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from .core import Refused

ET = ZoneInfo("America/New_York")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def opening_signal(bar):
    """Only opening-window values may enter the decision."""
    x = math.log(bar["c"] / bar["o"])
    return x, (1 if x > 0 else -1 if x < 0 else 0)


def evaluate(raw, plan):
    if digest(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()) != "5a7011aa1479ed17b5030c1c85fa46231f35c8f3a3a9393642d9ac727d1b5f1c":
        raise Refused("CONTINUATION_PLAN_NOT_FROZEN")
    by_day, seen = {}, set()
    reader = csv.DictReader(io.StringIO(raw.decode()))
    if set(reader.fieldnames or []) != {"ticker", "t", "o", "h", "l", "c", "v"}:
        raise Refused("CONTINUATION_CSV_SCHEMA")
    count = 0
    for row in reader:
        count += 1
        if row["ticker"] != plan["symbol"]:
            raise Refused("CONTINUATION_SYMBOL")
        try:
            stamp = int(row["t"])
            local = datetime.fromtimestamp(stamp / 1000, ET)
            values = {k: float(row[k]) for k in ("o", "h", "l", "c", "v")}
        except (ValueError, TypeError, OverflowError):
            raise Refused("CONTINUATION_INVALID_ROW") from None
        if stamp in seen:
            raise Refused("CONTINUATION_DUPLICATE_TIMESTAMP")
        seen.add(stamp)
        if stamp % 1800000 or not plan["from"] <= local.date().isoformat() <= plan["to"]:
            raise Refused("CONTINUATION_TIMESTAMP_RANGE")
        valid = (all(math.isfinite(v) for v in values.values())
                 and values["l"] > 0 and values["v"] > 0
                 and values["l"] <= min(values["o"], values["c"])
                 and values["h"] >= max(values["o"], values["c"]))
        by_day.setdefault(local.date().isoformat(), {})[local.strftime("%H:%M")] = (
            values if valid else None, stamp / 1000)
    rows, excluded = [], []
    day, end = date.fromisoformat(plan["from"]), date.fromisoformat(plan["to"])
    while day <= end:
        key = day.isoformat()
        if day.weekday() < 5:
            windows = by_day.get(key, {})
            opening, closing = windows.get("09:30"), windows.get("15:30")
            if not opening or not closing:
                excluded.append({"day": key, "reason": "MISSING_WINDOW_OR_CLOSED_SESSION"})
            elif opening[0] is None or closing[0] is None:
                excluded.append({"day": key, "reason": "INVALID_OHLCV"})
            else:
                feature, sign = opening_signal(opening[0])
                target = math.log(closing[0]["c"] / closing[0]["o"]) * 10000
                rows.append({"day": key, "feature": feature, "signal": sign,
                             "feature_available_assumed": opening[1] + 1800,
                             "decision_at": opening[1] + 1805,
                             "target_start": closing[1], "target_complete": closing[1] + 1800,
                             "target_bps": target, "signed_target_bps": sign * target})
        day += timedelta(days=1)
    if len(rows) < 10:
        raise Refused("CONTINUATION_INSUFFICIENT_PAIRED_SESSIONS")
    policy = np.array([r["signed_target_bps"] for r in rows])
    target = np.array([r["target_bps"] for r in rows])
    difference = policy - target
    n = len(rows)
    rng = np.random.default_rng(74001)
    starts = rng.integers(0, n, size=(10000, math.ceil(n / 5)))
    indices = ((starts[:, :, None] + np.arange(5)) % n).reshape(10000, -1)[:, :n]
    interval = np.quantile(difference[indices].mean(axis=1), [.025, .975]).tolist()
    active = np.array([abs(r["signal"]) for r in rows])
    return {"status": "DIAGNOSTIC_ONLY_NO_PROMOTION", "input_rows": count,
            "eligible_sessions": n, "excluded_weekdays": excluded,
            "mean_signed_target_bps": float(policy.mean()),
            "comparators_mean_bps": {"always_long": float(target.mean()),
                                     "always_short": float(-target.mean()), "always_flat": 0.0},
            "primary_difference_vs_long_bps": float(difference.mean()),
            "primary_95pct_block_bootstrap_interval_bps": interval,
            "directional_hit_rate_active": float((policy[active > 0] > 0).mean()) if active.sum() else None,
            "illustrative_cost_sensitivity_bps": {str(c): float((policy - active * c).mean())
                                                  for c in plan["cost_sensitivity_bps"]},
            "actual_trades": 0, "account_pnl": None,
            "scope": "Revised historical bar diagnostic; assumed availability, no quotes/fills/borrow; bootstrap is not calibrated future probability.",
            "rows": rows}


def run_continuation(input_path, plan_path, out):
    raw, plan_raw = input_path.read_bytes(), plan_path.read_bytes()
    result = evaluate(raw, json.loads(plan_raw))
    out.mkdir(parents=True, exist_ok=False)
    (out / "bars.csv").write_bytes(raw)
    (out / "plan.json").write_bytes(plan_raw)
    result.update(input_sha256=digest(raw), plan_sha256=digest(plan_raw),
                  evaluated_at=datetime.now(timezone.utc).isoformat(),
                  implementation_sha256=digest(Path(__file__).read_bytes()))
    (out / "result.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    return {k: v for k, v in result.items() if k != "rows"}


def verify_continuation(root):
    raw, plan_raw = (root / "bars.csv").read_bytes(), (root / "plan.json").read_bytes()
    recorded = json.loads((root / "result.json").read_text())
    expected = evaluate(raw, json.loads(plan_raw))
    valid = (all(recorded.get(k) == v for k, v in expected.items())
             and recorded.get("input_sha256") == digest(raw)
             and recorded.get("plan_sha256") == digest(plan_raw)
             and recorded.get("implementation_sha256") == digest(Path(__file__).read_bytes()))
    return {"status": "VALID" if valid else "INVALID", "scope": "Complete deterministic reconstruction; not provider authentication or trading validation"}
