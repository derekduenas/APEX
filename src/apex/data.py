"""One ingestion gate for bars and quotes, with availability separate from event time."""
from __future__ import annotations

import csv
import io
import math
import re
from decimal import Decimal, ROUND_FLOOR
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .core import Refused, digest, finite

EASTERN = ZoneInfo("America/New_York")
# A historical quote has no measured receipt. Calling it MEASURED_RECEIPT would be a lie, and calling it
# SYNTHETIC_CLOCK would mark real market data as a synthetic control (see input_class_of). So the assumption gets
# its own name and its own exact rule, the same way bar completion does.
QUOTE_LATENCY_NS = 1_000_000_000
QUOTE_LATENCY_SECONDS = QUOTE_LATENCY_NS / 1e9
BASES = {"SYNTHETIC_CLOCK", "MEASURED_RECEIPT", "BAR_COMPLETION_ASSUMPTION_V1", "QUOTE_LATENCY_ASSUMPTION_V1"}


def epoch_ns(epoch):
    """Floor the declared decimal decision clock to an integer-nanosecond cutoff."""
    if isinstance(epoch, (int, float)) and epoch == int(epoch):
        return int(epoch) * 1_000_000_000
    return int((Decimal(str(epoch)) * 1_000_000_000).to_integral_value(rounding=ROUND_FLOOR))


def duration_ns(seconds):
    """Floor a declared duration to nanoseconds; never extend an age allowance."""
    return int((Decimal(str(seconds)) * 1_000_000_000).to_integral_value(rounding=ROUND_FLOOR))


def exact_ns(seconds, field: str) -> int:
    """Nanoseconds from declared seconds, or a refusal. NEVER a reconstruction.

    Recovering a provider's nanoseconds from a float cannot be done soundly, and two successive attempts here
    were wrong in ways that took measurement to see. The second attempt failed for a reason worth recording: a
    float produced as `n / 1e9` is NOT the nearest float to n/10**9, because n exceeds 2**53 and the int-to-float
    conversion rounds before the division ever happens. So any bound depends on HOW the float was made, and this
    function is not told that. Provenance is the missing information, not precision.

    An integer-VALUED float is accepted here because a whole-second clock is a legitimate declaration, but that
    is a statement about the DECLARATION, not a proof that nothing was lost: a true stamp of ...200_000000123
    nanoseconds also rounds to exactly 1787578200.0. Anything whose nanoseconds actually matter must carry them,
    and the measured publisher path requires the original integers outright rather than relying on this.
    """
    if isinstance(seconds, bool):
        raise Refused("INVALID_TIME")
    if isinstance(seconds, int) or (isinstance(seconds, float) and seconds.is_integer()):
        return int(seconds) * 1_000_000_000
    raise Refused("FRACTIONAL_SECONDS_REQUIRE_NANOSECOND_STAMPS:" + field)


def event_ns(row):
    return row["event_ns"] if "event_ns" in row else exact_ns(row["event_epoch"], "event_epoch")


def available_ns(row):
    return row["available_ns"] if "available_ns" in row else exact_ns(row["available_epoch"], "available_epoch")


def decision_only(document):
    return (document.get("evidence_scope") == "DECISION_SNAPSHOTS_ONLY"
            or document.get("source") == "ALPACA_HISTORICAL_QUOTES_AS_OF_DECISION_INSTANTS"
            or any(r.get("evidence_scope") == "DECISION_SNAPSHOTS_ONLY" for r in document.get("observations", []) if isinstance(r, dict))
            or any(decision_only(c) for c in document.get("components", [])))


def collection_problem(document):
    if document.get("collection_status") not in (None, "COMPLETE"):
        return "INCOMPLETE_QUOTE_COLLECTION"
    if document.get("reconciliation", {}).get("status") not in (None, "COMPLETE"):
        return "INCOMPLETE_QUOTE_COLLECTION"
    for component in document.get("components", []):
        problem = collection_problem(component)
        if problem:
            return problem
    return None


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
    problem = collection_problem(document)
    if problem:
        raise Refused(problem)
    accepted, rejected = [], []
    for index, raw in enumerate(document["observations"]):
        try:
            if not isinstance(raw, dict):
                raise Refused("ROW_NOT_OBJECT")
            required = {"kind", "symbol", "event_epoch", "available_epoch", "availability_basis"}
            if not required <= raw.keys():
                raise Refused("MISSING_FIELDS:" + ",".join(sorted(required - raw.keys())))
            row = dict(raw)
            if not isinstance(row["availability_basis"], str) or row["availability_basis"] not in BASES or not isinstance(row["symbol"], str) or not row["symbol"]:
                raise Refused("INVALID_SOURCE_LABEL")
            if not all(finite(row[k]) for k in ("event_epoch", "available_epoch")):
                raise Refused("INVALID_TIME")
            if row["available_epoch"] < row["event_epoch"]:
                raise Refused("AVAILABLE_BEFORE_EVENT")
            for field in ("event_epoch", "available_epoch"):
                if field.replace("_epoch", "_ns") not in row:
                    exact_ns(row[field], field)
            if "event_ns" in row or "available_ns" in row:
                if not all(type(row.get(k)) is int for k in ("event_ns", "available_ns")):
                    raise Refused("QUOTE_LATENCY_ASSUMPTION_REQUIRES_NANOSECOND_STAMPS" if row["availability_basis"] == "QUOTE_LATENCY_ASSUMPTION_V1" else "INVALID_NANOSECOND_STAMPS")
                if row["available_ns"] < row["event_ns"]:
                    raise Refused("AVAILABLE_BEFORE_EVENT")
                if row["event_epoch"] != row["event_ns"] / 1e9 or row["available_epoch"] != row["available_ns"] / 1e9:
                    raise Refused("SECONDS_DISAGREE_WITH_NANOSECOND_STAMPS")
            # Also reject epochs that cannot be represented by the session clock.
            session(row["event_epoch"])
            if row["kind"] == "bar":
                if row["availability_basis"] == "QUOTE_LATENCY_ASSUMPTION_V1":
                    raise Refused("QUOTE_ASSUMPTION_IS_NOT_BAR_AVAILABILITY")
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
                if row["availability_basis"] == "QUOTE_LATENCY_ASSUMPTION_V1":
                    # Exact, like the bar rule: a declared assumption that can be stretched is not an assumption.
                    # Checked in INTEGER NANOSECONDS. A float epoch near 1.79e9 resolves to about 240ns, so a
                    # one-nanosecond violation compared in seconds silently comes out equal; the adapter carries
                    # event_ns and available_ns for exactly this check, and the float seconds are derived.
                    if not all(type(row.get(k)) is int for k in ("event_ns", "available_ns")):
                        raise Refused("QUOTE_LATENCY_ASSUMPTION_REQUIRES_NANOSECOND_STAMPS")
                    if row["available_ns"] - row["event_ns"] != QUOTE_LATENCY_NS:
                        raise Refused("QUOTE_LATENCY_ASSUMPTION_DISAGREES")
                    if row["event_epoch"] != row["event_ns"] / 1e9 or row["available_epoch"] != row["available_ns"] / 1e9:
                        raise Refused("SECONDS_DISAGREE_WITH_NANOSECOND_STAMPS")
                if not all(finite(row.get(k)) for k in ("bid", "ask", "bid_size", "ask_size")):
                    raise Refused("INVALID_QUOTE")
                if any(k in row for k in ("provider_bid_size", "provider_ask_size", "provider_bid_lots", "provider_ask_lots")):
                    for side in ("bid", "ask"):
                        raw_size = row.get("provider_" + side + "_size", row.get("provider_" + side + "_lots"))
                        if not finite(raw_size) or raw_size < 0:
                            raise Refused("INVALID_RAW_PROVIDER_SIZE")
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
    groups: dict[int, list] = {}
    cutoff_ns = epoch_ns(now)
    for row in rows:
        if row["symbol"] == symbol and row["kind"] == kind and available_ns(row) <= cutoff_ns:
            groups.setdefault(event_ns(row), []).append(row)
    out, conflicts = [], []
    for instant, group in sorted(groups.items()):
        keys = ("open", "high", "low", "close", "volume") if kind == "bar" else ("bid", "ask", "bid_size", "ask_size")
        if len({tuple(r[k] for k in keys) for r in group}) > 1:
            conflicts.append({"event_epoch": instant / 1e9, "event_ns": instant, "observation_ids": sorted(r["observation_id"] for r in group)})
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


STAMP = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)")


def timestamp_ns(text) -> int:
    """The one provider-timestamp parser. Capture imports it from here rather than keeping a second copy.

    datetime.fromisoformat truncates fractions beyond microseconds, and Alpaca quotes carry one, so the fraction is
    read separately. An absent offset is refused instead of being silently read as local time.
    """
    match = STAMP.fullmatch(text) if isinstance(text, str) else None
    if not match:
        raise Refused("TIMESTAMP_NOT_RFC3339")
    base, fraction, offset = match.groups()
    stamp = datetime.fromisoformat(base + offset.replace("Z", "+00:00"))
    return int(stamp.timestamp()) * 1_000_000_000 + int((fraction or "").ljust(9, "0"))


def from_alpaca_quotes(quotes: list, symbol: str, retrieved_utc: str, *, round_lot_shares: int = 1) -> list:
    """Alpaca v2 historical quotes -> observations, with availability DECLARED rather than measured.

    Sizes are caller-declared provider units times a multiplier. Similar quote/trade size distributions
    do not verify provider units. Raw values remain available for a conservative quantity ceiling.
    """
    if type(round_lot_shares) is not int or not 1 <= round_lot_shares <= 10000:
        raise Refused("EXPLICIT_QUOTE_LOT_SIZE_REQUIRED")
    out = []
    for raw in quotes:
        # The integer nanoseconds are retained alongside the float seconds the rest of the system uses, so the
        # availability assumption can be checked exactly rather than at float resolution.
        event_ns = timestamp_ns(raw["t"])
        available_ns = event_ns + QUOTE_LATENCY_NS
        out.append({"kind": "quote", "symbol": symbol, "event_epoch": event_ns / 1e9,
                    "available_epoch": available_ns / 1e9, "event_ns": event_ns, "available_ns": available_ns,
                    "assumed_latency_ns": QUOTE_LATENCY_NS,
                    "availability_basis": "QUOTE_LATENCY_ASSUMPTION_V1",
                    "bid": float(raw["bp"]), "ask": float(raw["ap"]),
                    "bid_size": float(raw["bs"]) * round_lot_shares,
                    "ask_size": float(raw["as"]) * round_lot_shares,
                    "bid_exchange": raw.get("bx"), "ask_exchange": raw.get("ax"),
                    "tape": raw.get("z"), "conditions": raw.get("c"),
                    "provider_bid_size": raw["bs"], "provider_ask_size": raw["as"],
                    "round_lot_shares": round_lot_shares,
                    "size_conversion_basis": "OPERATOR_DECLARED_SIZE_MULTIPLIER_UNVERIFIED",
                    "retrieved_utc": retrieved_utc})
    return out


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


def merged_document(documents: list, *, retrieved_utc: str) -> dict:
    """Combine input documents into one research input WITHOUT flattening their provenance.

    A merged document that reported a single `source` would be the more convenient object and the less honest
    one: bars and quotes here come from different providers under different availability rules, and a reader of
    the study needs to see which claim attaches to which half. So every component record is retained verbatim
    under `components`, and the top-level scope fields say only what is true of the union.
    """
    if not documents or any(d.get("schema") != "APEX_DATA_V1" for d in documents):
        raise Refused("INVALID_DATA_DOCUMENT")
    from .research import input_class_of
    from .admissibility import require_mode
    for d in documents:
        rows, rejected = normalize(d)
        if rejected:
            raise Refused("MERGE_INPUT_ROWS_REJECTED")
        mode = "SYNTHETIC_CONTROL" if input_class_of(d, rows) == "SYNTHETIC_RESEARCH_CONTROL" else "OFFLINE_RESEARCH"
        if rows:
            require_mode(d, rows, mode)
    classes = {input_class_of(d, d["observations"]) == "SYNTHETIC_RESEARCH_CONTROL" for d in documents}
    if len(classes) > 1:
        # Refused here as well as named downstream: the study should never have to explain a mixed input.
        raise Refused("REFUSING_TO_MERGE_SYNTHETIC_AND_RECORDED_INPUTS")
    observations, components = [], []
    for document in documents:
        observations.extend(document["observations"])
        components.append({k: v for k, v in document.items() if k != "observations"}
                          | {"observation_count": len(document["observations"])})
    return {"schema": "APEX_DATA_V1", "source": "MERGED_INPUT_DOCUMENTS", "retrieved_utc": retrieved_utc,
            "retrieved_utc_basis": "Merge time; each component keeps its own retrieval claim",
            "coverage": "The union of the component coverages below and no wider.",
            "source_authenticity": "COMPONENT_RESPONSES_NOT_INDEPENDENTLY_ATTESTED",
            "limitation": "Each component's limitations apply unchanged to its own observations.",
            "components": components, "observations": observations}

