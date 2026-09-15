"""One ingestion gate for bars and quotes, with availability separate from event time."""
from __future__ import annotations

import csv
import io
import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .core import Refused, digest, finite

EASTERN = ZoneInfo("America/New_York")
BASES = {"SYNTHETIC_CLOCK", "MEASURED_RECEIPT", "BAR_COMPLETION_ASSUMPTION_V1"}


def session(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, EASTERN).date().isoformat()


def regular(epoch: float) -> bool:
    dt = datetime.fromtimestamp(epoch, EASTERN)
    minute = dt.hour * 60 + dt.minute
    return dt.weekday() < 5 and 570 <= minute < 960


def normalize(document: dict) -> tuple[list[dict], list[dict]]:
    """Reject malformed rows by name; validate provenance labels without authenticating them.

    Historical completion is an explicit research assumption. Actual retrieval time
    is kept at document level and is never passed off as historical receipt time.
    """
    if document.get("schema") != "APEX_DATA_V1" or not isinstance(document.get("observations"), list):
        raise Refused("INVALID_DATA_DOCUMENT")
    accepted, rejected = [], []
    for index, raw in enumerate(document["observations"]):
        try:
            if not isinstance(raw, dict):
                raise Refused("ROW_NOT_OBJECT")
            required = {"kind", "symbol", "event_epoch", "available_epoch", "availability_basis"}
            if not required <= raw.keys():
                raise Refused("MISSING_FIELDS:" + ",".join(sorted(required - raw.keys())))
            row = dict(raw)
            if row["availability_basis"] not in BASES or not isinstance(row["symbol"], str) or not row["symbol"]:
                raise Refused("INVALID_SOURCE_LABEL")
            if not all(finite(row[k]) for k in ("event_epoch", "available_epoch")):
                raise Refused("INVALID_TIME")
            if row["available_epoch"] < row["event_epoch"]:
                raise Refused("AVAILABLE_BEFORE_EVENT")
            # Also reject epochs that cannot be represented by the session clock.
            session(row["event_epoch"])
            if row["kind"] == "bar":
                if row["event_epoch"] % 60 != 0:
                    raise Refused("BAR_START_NOT_MINUTE_ALIGNED")
                if not all(finite(row.get(k)) for k in ("open", "high", "low", "close", "volume")):
                    raise Refused("INVALID_OHLCV")
                if not 0 < row["low"] <= min(row["open"], row["close"]) <= max(row["open"], row["close"]) <= row["high"] or row["volume"] < 0:
                    raise Refused("INCONSISTENT_OHLCV")
                if row["available_epoch"] < row["event_epoch"] + 60:
                    raise Refused("BAR_NOT_COMPLETE_AT_AVAILABILITY")
                if row["availability_basis"] == "BAR_COMPLETION_ASSUMPTION_V1" and row["available_epoch"] != row["event_epoch"] + 60:
                    raise Refused("COMPLETION_ASSUMPTION_DISAGREES")
            elif row["kind"] == "quote":
                if row["availability_basis"] == "BAR_COMPLETION_ASSUMPTION_V1":
                    raise Refused("BAR_ASSUMPTION_IS_NOT_QUOTE_AVAILABILITY")
                if not all(finite(row.get(k)) for k in ("bid", "ask", "bid_size", "ask_size")):
                    raise Refused("INVALID_QUOTE")
                if not 0 < row["bid"] <= row["ask"] or min(row["bid_size"], row["ask_size"]) < 0:
                    raise Refused("INVALID_QUOTE_MARKET")
            else:
                raise Refused("UNKNOWN_OBSERVATION_KIND")
            row.pop("observation_id", None)
            row["observation_id"] = digest(row)
            accepted.append(row)
        except (Refused, ValueError, OverflowError, OSError) as exc:
            rejected.append({"input_index": index, "reason": str(exc)})
    # Only exact duplicates collapse here. Conflicts must be adjudicated AS OF
    # the decision, so a future correction cannot poison an earlier observation.
    distinct = {r["observation_id"]: r for r in accepted}
    return sorted(distinct.values(), key=lambda r: (r["available_epoch"], r["event_epoch"], r["observation_id"])), rejected


def visible(rows: list[dict], *, now: float, symbol: str, kind: str) -> tuple[list[dict], list[dict]]:
    groups: dict[float, list] = {}
    for row in rows:
        if row["symbol"] == symbol and row["kind"] == kind and row["available_epoch"] <= now:
            groups.setdefault(row["event_epoch"], []).append(row)
    out, conflicts = [], []
    for instant, group in sorted(groups.items()):
        keys = ("open", "high", "low", "close", "volume") if kind == "bar" else ("bid", "ask", "bid_size", "ask_size")
        if len({tuple(r[k] for k in keys) for r in group}) > 1:
            conflicts.append({"event_epoch": instant, "observation_ids": sorted(r["observation_id"] for r in group)})
        else:
            # Market values agree: preserve earliest knowable evidence, with all
            # raw provenance retained in the captured input document.
            out.append(min(group, key=lambda r: (r["available_epoch"], r["observation_id"])))
    return out, conflicts


def twin(rows: list[dict], now: float, symbol: str) -> tuple[dict, list[dict]]:
    bars, conflicts = visible(rows, now=now, symbol=symbol, kind="bar")
    bars = [b for b in bars if regular(b["event_epoch"])]
    today = [b for b in bars if session(b["event_epoch"]) == session(now)]
    if not today or now - (today[-1]["event_epoch"] + 60) > 120:
        raise Refused("CURRENT_COMPLETED_BAR_UNAVAILABLE_OR_STALE")
    returns = []
    for left, right in zip(bars, bars[1:]):
        if right["event_epoch"] - left["event_epoch"] == 60 and session(left["event_epoch"]) == session(right["event_epoch"]):
            returns.append({"event_time": right["event_epoch"] + 60,
                            "available": max(left["available_epoch"], right["available_epoch"]),
                            "ret_1": math.log(right["close"] / left["close"]),
                            "input_ids": [left["observation_id"], right["observation_id"]]})
    fields = {"spot": today[-1]["close"], "last_bar_event": today[-1]["event_epoch"],
              "volume": sum(b["volume"] for b in today), "completed_bars_today": len(today)}
    fields["close_weighted_vwap_proxy"] = (sum(b["close"] * b["volume"] for b in today) / fields["volume"] if fields["volume"] else None)
    for n in (1, 5, 15):
        block = today[-n - 1:]
        contiguous = len(block) == n + 1 and block[-1]["event_epoch"] - block[0]["event_epoch"] == n * 60
        fields[f"ret_{n}"] = math.log(block[-1]["close"] / block[0]["close"]) if contiguous else None
    state = {"symbol": symbol, "now": now, "fields": fields, "bar_ids": [b["observation_id"] for b in bars], "conflicts": conflicts}
    return {**state, "snapshot_id": digest(state)}, returns


def from_massive_csv(text: str, symbol: str, retrieved_utc: str) -> dict:
    observations = []
    for row in csv.DictReader(io.StringIO(text)):
        event = int(row["t"]) / 1000
        observations.append({"kind": "bar", "symbol": symbol, "event_epoch": event, "available_epoch": event + 60,
                             "availability_basis": "BAR_COMPLETION_ASSUMPTION_V1",
                             **{long: float(row[short]) for short, long in (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close"), ("v", "volume"))}})
    return {"schema": "APEX_DATA_V1", "source": "MASSIVE_CONNECTOR_TRANSFORMED_CSV", "retrieved_utc": retrieved_utc,
            "retrieved_utc_basis": "Operator-supplied capture timestamp; not measured network receipt",
            "coverage": "NOT_INDEPENDENTLY_ESTABLISHED; connector export may omit requested intervals",
            "source_authenticity": "CONNECTOR_RESPONSE_NOT_INDEPENDENTLY_ATTESTED",
            "limitation": "Retrospective finalized bars; event+60 availability assumed, revisions and original latency unknown. No quotes supplied.",
            "observations": observations}
