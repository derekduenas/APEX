"""Bind accounting, captured inputs, model paths and completion to one run."""
import hashlib
import json
from pathlib import Path

import numpy as np

from .core import Refused, digest
from .data import normalize
from .ledger import read_verified, reconstruct


def verify_run(root: Path) -> dict:
    manifest = json.loads((root / "manifest.json").read_text())
    captured = (root / "input.json").read_bytes()
    if hashlib.sha256(captured).hexdigest() != manifest["input_sha256"]:
        raise Refused("CAPTURE_DIGEST_MISMATCH")
    rows = read_verified(root / "ledger.jsonl")
    if rows[0]["payload"]["manifest_digest"] != digest(manifest):
        raise Refused("MANIFEST_NOT_LEDGER_BOUND")
    if rows[0]["payload"]["config"] != manifest["config"]:
        raise Refused("CONFIG_DISAGREES")
    observations, _ = normalize(json.loads(captured))
    by_id = {o["observation_id"]: o for o in observations}
    if rows[-1]["kind"] != "RUN_CLOSE" or (root / "COMPLETE").read_text() != rows[-1]["hash"]:
        raise Refused("INCOMPLETE_RUN_OR_CHANGED_HEAD")
    seen = {}
    forecasts = {}
    for row in rows:
        p = row["payload"]
        if row["kind"] == "QUOTE":
            if by_id.get(p.get("observation_id")) != p:
                raise Refused("QUOTE_NOT_CAPTURE_BOUND")
        elif row["kind"] == "TWIN":
            body = {k: v for k, v in p.items() if k != "snapshot_id"}
            if digest(body) != p["snapshot_id"] or any(i not in by_id or by_id[i]["available_epoch"] > row["epoch"] for i in p["bar_ids"]):
                raise Refused("TWIN_INPUT_BINDING_INVALID")
        elif row["kind"] == "FORECAST":
            identity = {k: v for k, v in p.items() if k not in ("forecast_id", "path_file")}
            if digest(identity) != p["forecast_id"] or digest(p["training_rows"]) != p["training_digest"]:
                raise Refused("FORECAST_IDENTITY_INVALID")
            if p["path_file"] != p["forecast_id"] + ".npy":
                raise Refused("INVALID_PATH_ARTIFACT_NAME")
            paths = np.load(root / p["path_file"], allow_pickle=False)
            if paths.shape != (p["path_count"], p["horizon_minutes"] + 1) or not np.all(np.isfinite(paths)) or digest(paths.tolist()) != p["paths_digest"]:
                raise Refused("SIMULATION_ARTIFACT_CHANGED")
            terminal = paths[:, -1]
            baseline = terminal - p["direction"]["per_minute_log_drift"] * p["horizon_minutes"]
            if p["p_up"] != float((terminal > 0).mean()) or p["baseline_p_up"] != float((baseline > 0).mean()):
                raise Refused("PATH_PROBABILITY_DISAGREES")
            for q, value in p["quantiles"].items():
                if value != float(np.quantile(terminal, float(q))):
                    raise Refused("PATH_QUANTILE_DISAGREES")
            for training in p["training_rows"]:
                if training["available"] > row["epoch"] or training["event_time"] > row["epoch"]:
                    raise Refused("FUTURE_TRAINING_INPUT")
                left, right = [by_id[i] for i in training["input_ids"]]
                expected = float(np.log(right["close"] / left["close"]))
                if not np.isclose(training["ret_1"], expected, rtol=0, atol=1e-14) or max(left["available_epoch"], right["available_epoch"]) != training["available"]:
                    raise Refused("TRAINING_INPUT_DISAGREES")
            forecasts[p["forecast_id"]] = p
        elif row["kind"] == "CANDIDATE":
            if seen.get(p["forecast_ref"], {}).get("kind") != "FORECAST" or seen.get(p["snapshot_ref"], {}).get("kind") != "TWIN":
                raise Refused("CANDIDATE_MISSING_INPUT")
        elif row["kind"] == "FORECAST_SCORE":
            f, bar = forecasts[p["forecast_id"]], by_id[p["bar_id"]]
            if f["target_epoch"] != bar["event_epoch"] + 60 or bar["available_epoch"] > row["epoch"]:
                raise Refused("LABEL_NOT_MATURE")
            realized = float(np.log(bar["close"] / f["spot"]))
            if not np.isclose(p["realized_log_return"], realized, rtol=0, atol=1e-14):
                raise Refused("LABEL_VALUE_DISAGREES")
            outcome = int(realized > 0)
            if p["brier"] != (f["p_up"] - outcome) ** 2 or p["baseline_brier"] != (f["baseline_p_up"] - outcome) ** 2:
                raise Refused("SCORE_DISAGREES")
        seen[row["hash"]] = row
    result = reconstruct(root / "ledger.jsonl")
    saved = json.loads((root / "summary.json").read_text())
    if saved["accounting"] != result:
        raise Refused("SAVED_ACCOUNTING_DISAGREES")
    actual = {"forecasts": sum(r["kind"] == "FORECAST" for r in rows), "entries": sum(r["kind"] == "ENTRY" for r in rows),
              "exits": sum(r["kind"] == "EXIT" for r in rows), "scored_forecasts": sum(r["kind"] == "FORECAST_SCORE" for r in rows)}
    if any(saved["counts"][k] != v for k, v in actual.items()):
        raise Refused("SAVED_COUNTS_DISAGREE")
    return {**result, "verified_forecasts": len(forecasts), "capture_and_path_bindings": "VERIFIED",
            "scope": "Internal artifact consistency, not authenticated source history or calibrated forecasting"}
