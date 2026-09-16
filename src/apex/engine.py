"""One offline event loop. Due exits run before expensive models and new entries.

No broker imports, orders, account permissions, or live capital authorization.
The experimental policy is explicitly allowed only inside this replay engine.
"""
from __future__ import annotations

import hashlib
import json
import platform
from decimal import Decimal
from pathlib import Path

import numpy as np
import scipy

from .admissibility import admissibility
from .core import Config, Refused, canonical, code_manifest, digest, fee, money
from .data import normalize, regular, session, twin, visible
from .decision import evaluate, quote_at
from .forecast import predict
from .ledger import Ledger, reconstruct


def run(input_bytes: bytes | Path, out: Path, *, start: float, end: float, config: Config) -> dict:
    if end <= start or end - start > 86400 or session(start) != session(end):
        raise Refused("ONE_SESSION_RUN_RANGE_REQUIRED")
    out.mkdir(parents=True, exist_ok=False)
    ledger = None
    try:
        if isinstance(input_bytes, Path):
            input_bytes = input_bytes.read_bytes()
        (out / "input.json").write_bytes(input_bytes)
        document = json.loads(input_bytes)
        observations, rejected = normalize(document)
        manifest = {"schema": "APEX_RUN_V1", "mode": "OFFLINE_EXPERIMENTAL_REPLAY", "start": start, "end": end,
                    "input_sha256": hashlib.sha256(input_bytes).hexdigest(), "source": document.get("source"),
                    "config": config.record(), "code": code_manifest(),
                    "runtime": {"python": platform.python_version(), "numpy": np.__version__, "scipy": scipy.__version__},
                    "authority": "NO_BROKER_OR_PRODUCTION_CAPITAL_AUTHORITY", "rejected": rejected,
                    "availability_bases": sorted({r["availability_basis"] for r in observations}),
                    "admissibility": admissibility(document, observations)}
        (out / "manifest.json").write_text(canonical(manifest))
        ledger = Ledger(out / "ledger.jsonl")
        ledger.append("RUN_OPEN", start, {"config": config.record(), "manifest_digest": digest(manifest)})
        # Entry cutoff is frozen before execution; it is not selected from outcomes.
        scans = set(np.arange(start, end - config.horizon_minutes * 60 + 0.001, config.scan_minutes * 60).tolist())
        times = {start, end, *scans}
        times.update(r["available_epoch"] for r in observations if start <= r["available_epoch"] <= end)
        for scan in scans:
            times.add(scan + config.horizon_minutes * 60)
            close = scan + config.horizon_minutes * 60 + config.exit_window_seconds
            if close <= end:
                times.add(close)
        cash = money(config.starting_cash)
        position, pending = None, {}
        counts = {"scans": 0, "forecasts": 0, "entries": 0, "exits": 0, "scored_forecasts": 0}
        variances, decisions = {}, {}
        scores = []

        for now in sorted(times):
            q, quote_problem = quote_at(observations, now, config)
            if position and position["due"] <= now <= position["window_end"]:
                observation_id = q["observation_id"] if q else None
                # Retry only a new usable quote. Refusal isn't counted as an
                # attempt to fill; the declared contract is a time window.
                if q and observation_id != position.get("last_quote"):
                    position["last_quote"] = observation_id
                    if q["bid_size"] >= position["quantity"]:
                        qr = ledger.append("QUOTE", now, q)
                        qty = position["quantity"]
                        credit, charge = money(Decimal(str(q["bid"])) * qty), fee(qty, config)
                        gross = credit - position["debit"]
                        net = gross - position["fee"] - charge
                        cash += credit - charge
                        ledger.append("EXIT", now, {"entry_ref": position["entry_ref"], "quote_ref": qr["hash"], "quantity": qty,
                                                    "credit": str(credit), "fee": str(charge), "gross_pnl": str(gross),
                                                    "net_pnl": str(net), "cash_after": str(cash)})
                        counts["exits"] += 1
                        position = None
                elif now == position["due"]:
                    ledger.append("EXIT_WAIT", now, {"entry_ref": position["entry_ref"], "reason": quote_problem or "SIZE_UNAVAILABLE"})
            if position and now >= position["window_end"] and not position.get("exhausted"):
                position["exhausted"] = True
                ledger.append("EXIT_WINDOW_CLOSED", now, {"entry_ref": position["entry_ref"], "exposure_retained": True})

            # Outcomes are joined only after their target bar is completed and
            # available. No realized label is passed to an earlier forecast.
            bars, _ = visible(observations, now=now, symbol=config.symbol, kind="bar")
            by_close = {b["event_epoch"] + 60: b for b in bars}
            for fid, f in list(pending.items()):
                bar = by_close.get(f["target_epoch"])
                if now < f["target_epoch"] or bar is None:
                    continue
                realized = float(np.log(bar["close"] / f["spot"]))
                outcome = int(realized > 0)
                score = {"forecast_id": fid, "bar_id": bar["observation_id"], "realized_log_return": realized,
                         "target_epoch": f["target_epoch"], "label_available_epoch": bar["available_epoch"],
                         "brier": (f["p_up"] - outcome) ** 2, "baseline_brier": (f["baseline_p_up"] - outcome) ** 2,
                         "interval_covered": f["quantiles"]["0.05"] <= realized <= f["quantiles"]["0.95"]}
                ledger.append("FORECAST_SCORE", now, score)
                scores.append(score)
                counts["scored_forecasts"] += 1
                del pending[fid]

            if now not in scans:
                continue
            counts["scans"] += 1
            if not regular(now) or not regular(now + config.horizon_minutes * 60 - 60):
                ledger.append("SCAN_REFUSED", now, {"reason": "REGULAR_SESSION_HORIZON_REQUIRED"})
                continue
            try:
                state, returns = twin(observations, now, config.symbol)
                sr = ledger.append("TWIN", now, state)
                prediction, paths = predict(returns, state, config)
                path_file = prediction["forecast_id"] + ".npy"
                np.save(out / path_file, paths, allow_pickle=False)
                prediction["path_file"] = path_file
                fr = ledger.append("FORECAST", now, prediction)
                pending[prediction["forecast_id"]] = prediction
                counts["forecasts"] += 1
                model_name = prediction["variance"]["model_id"]
                variances[model_name] = variances.get(model_name, 0) + 1
                ledger.append("LAYER_RECEIPT", now, {"layer": "forecast", "input_ref": sr["hash"], "output_ref": fr["hash"],
                                                    "read_fields": ["spot", "snapshot_id", "now", "adjacent_completed_returns"],
                                                    "scope": "Instrumented call arguments; behavior tested separately"})
            except Refused as exc:
                ledger.append("SCAN_REFUSED", now, {"reason": str(exc)})
                continue

            candidate = evaluate(prediction, paths, q, quote_problem, cash=cash, position_open=bool(position), config=config)
            qty, reason, decision = candidate["quantity"], candidate["reason"], candidate["decision"]
            decisions[reason or decision] = decisions.get(reason or decision, 0) + 1
            qr = ledger.append("QUOTE", now, q) if q else None
            candidate.update(forecast_ref=fr["hash"], snapshot_ref=sr["hash"], quote_ref=qr["hash"] if qr else None)
            cr = ledger.append("CANDIDATE", now, candidate)
            if decision == "EXPERIMENTAL_LONG":
                debit, charge = money(Decimal(str(q["ask"])) * qty), fee(qty, config)
                cash -= debit + charge
                er = ledger.append("ENTRY", now, {"candidate_ref": cr["hash"], "quote_ref": qr["hash"], "quantity": qty,
                                                   "debit": str(debit), "fee": str(charge), "cash_after": str(cash)})
                position = {"entry_ref": er["hash"], "quantity": qty, "debit": debit, "fee": charge,
                            "due": now + config.horizon_minutes * 60,
                            "window_end": now + config.horizon_minutes * 60 + config.exit_window_seconds}
                counts["entries"] += 1
        ledger.append("RUN_CLOSE", end, {"status": "CLOSED_WITH_OUTSTANDING_OBLIGATIONS" if position else "CLOSED",
                                        "pending_forecast_ids": sorted(pending), "counts": counts})
        ledger.close()
        ledger = None
        accounting = reconstruct(out / "ledger.jsonl")
        if accounting["status"] != "VALID":
            raise Refused("ACCOUNTING_RECONSTRUCTION_FAILED")
        summary = {"mode": "OFFLINE_EXPERIMENTAL_REPLAY", "counts": counts, "variance_models": variances, "decisions": decisions,
                   "accounting": accounting, "forecast_feedback": {"count": len(scores),
                   "mean_brier": float(np.mean([s["brier"] for s in scores])) if scores else None,
                   "baseline_mean_brier": float(np.mean([s["baseline_brier"] for s in scores])) if scores else None,
                   "status": "DESCRIPTIVE_SCORES_NOT_CALIBRATION_OR_PROMOTION"},
                   "readiness": "REPLAY_PROTOTYPE; NOT_LIVE_PAPER_OR_REAL_MONEY_READY"}
        (out / "summary.json").write_text(canonical(summary))
        (out / "COMPLETE").write_text(accounting["head"])
        return summary
    except BaseException as exc:
        (out / "FAILED.json").write_text(canonical({"type": type(exc).__name__, "reason": str(exc)}))
        raise
    finally:
        if ledger:
            ledger.close()
