"""Bounded read-only capture and verification through the actual shadow reader.

HTTP authentication stays in a killable child process. Receipt is measured when
the parent obtains its result, an upper bound on body arrival, not exchange time.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .core import Config, Refused, canonical, code_manifest, digest
from .data import normalize
from .shadow import observe

ENDPOINTS = {"history": "/v2/stocks/bars", "bar": "/v2/stocks/bars/latest", "quote": "/v2/stocks/quotes/latest"}
STAMP = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)")


def timestamp_ns(text):
    match = STAMP.fullmatch(text) if isinstance(text, str) else None
    if not match:
        raise Refused("TIMESTAMP_NOT_RFC3339")
    base, fraction, offset = match.groups()
    stamp = datetime.fromisoformat(base + offset.replace("Z", "+00:00"))
    return int(stamp.timestamp()) * 1_000_000_000 + int((fraction or "").ljust(9, "0"))


def http_transport(path, params, timeout):
    """Timeout kills and reaps the whole worker, including DNS/slow response reads."""
    try:
        result = subprocess.run([sys.executable, "-m", "apex.http_worker"],
                                input=canonical({"path": path, "params": params, "timeout": timeout}),
                                capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return {"status": 0, "error": "WALL_CLOCK_DEADLINE_EXCEEDED"}
    if result.returncode:
        return {"status": 0, "error": "TRANSPORT_WORKER_FAILED"}
    try:
        response = json.loads(result.stdout)
        if not isinstance(response, dict) or type(response.get("status")) is not int:
            raise ValueError()
        return response
    except ValueError:
        return {"status": 0, "error": "TRANSPORT_WORKER_INVALID_OUTPUT"}


def decode(body: bytes, *, kind: str, symbol: str, feed: str, received_ns: int, round_lot_shares: int,
           availability_basis="MEASURED_RECEIPT"):
    if availability_basis not in ("MEASURED_RECEIPT", "SYNTHETIC_CLOCK"):
        raise Refused("INVALID_CAPTURE_AVAILABILITY_BASIS")
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise Refused("PROVIDER_PAYLOAD_NOT_OBJECT")
    family = "quotes" if kind == "quote" else "bars"
    if not isinstance(payload.get(family), dict):
        raise Refused("PROVIDER_FAMILY_MISSING:" + family)
    returned = payload[family].get(symbol)
    raw_rows = returned if kind == "history" else ([returned] if returned is not None else [])
    if raw_rows is None:
        raw_rows = []
    if not isinstance(raw_rows, list):
        raise Refused("PROVIDER_ROWS_NOT_LIST")
    records, rejected = [], []
    raw_digest = hashlib.sha256(body).hexdigest()
    for index, raw in enumerate(raw_rows):
        try:
            if not isinstance(raw, dict):
                raise Refused("PROVIDER_ROW_NOT_OBJECT")
            event_ns = timestamp_ns(raw.get("t"))
            if event_ns > received_ns:
                raise Refused("PROVIDER_EVENT_AFTER_RECEIPT")
            if kind != "quote" and event_ns + 60_000_000_000 > received_ns:
                raise Refused("PROVIDER_BAR_NOT_COMPLETE")
            row = {"kind": "quote" if kind == "quote" else "bar", "symbol": symbol,
                   "event_epoch": event_ns / 1e9, "available_epoch": received_ns / 1e9,
                   "availability_basis": availability_basis, "available_epoch_ns": received_ns,
                   "provider_timestamp": raw["t"], "provider_timestamp_ns": event_ns,
                   "receipt_basis": "PARENT_PROCESS_RECEIPT_UPPER_BOUND" if availability_basis == "MEASURED_RECEIPT" else "SYNTHETIC_RECEIPT_FIXTURE", "feed": feed,
                   "market_coverage": "SIP" if feed == "sip" else "IEX_ONLY_NOT_NBBO",
                   "raw_response_sha256": raw_digest, "raw_row_index": index}
            if kind == "quote":
                if any(type(raw.get(k)) is not int or raw[k] < 0 for k in ("as", "bs")):
                    raise Refused("QUOTE_SIZE_NOT_NONNEGATIVE_INTEGER_LOTS")
                row.update(bid=raw["bp"], ask=raw["ap"], bid_size=raw["bs"] * round_lot_shares,
                           ask_size=raw["as"] * round_lot_shares, provider_bid_lots=raw["bs"], provider_ask_lots=raw["as"],
                           round_lot_shares=round_lot_shares, size_conversion_basis="OPERATOR_DECLARED_LOT_SIZE_NOT_INDEPENDENTLY_VERIFIED")
            else:
                row.update({long: raw[short] for short, long in (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close"), ("v", "volume"))})
            accepted, problems = normalize({"schema": "APEX_DATA_V1", "observations": [row]})
            if problems:
                raise Refused(problems[0]["reason"])
            records.extend(accepted)
        except (KeyError, Refused, ValueError, OverflowError, OSError) as exc:
            rejected.append({"input_index": index, "reason": str(exc)})
    token = payload.get("next_page_token")
    if token is not None and (not isinstance(token, str) or len(token) > 4096):
        raise Refused("INVALID_PAGE_TOKEN")
    return records, rejected, token


def _write(path, value):
    with path.open("x", encoding="utf-8") as handle:
        handle.write(canonical(value))
        handle.flush()
        os.fsync(handle.fileno())


def capture_alpaca(out: Path, *, history_start: str, config: Config, feed: str, round_lot_shares: int,
                   max_pages=3, timeout=5.0, transport=http_transport, clock_ns=time.time_ns):
    """One bounded capture: up to max_pages history requests + one bar + one quote.

    History has actual retrieval availability, never backdated completion. This
    operation is a shadow observer and cannot write execution entries or exits.
    """
    out.mkdir(parents=True, exist_ok=False)
    packets, shadows, observations = [], [], []
    try:
        if feed not in ("sip", "iex") or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", config.symbol):
            raise Refused("INVALID_CAPTURE_FEED_OR_SYMBOL")
        if type(round_lot_shares) is not int or not 1 <= round_lot_shares <= 10000:
            raise Refused("EXPLICIT_QUOTE_LOT_SIZE_REQUIRED")
        if type(max_pages) is not int or not 1 <= max_pages <= 10 or not 0 < timeout <= 30:
            raise Refused("INVALID_CAPTURE_BUDGET")
        start_ns, opened_ns = timestamp_ns(history_start), clock_ns()
        if not 0 < opened_ns - start_ns <= 10 * 86400 * 1_000_000_000:
            raise Refused("HISTORY_WINDOW_MUST_PRECEDE_CAPTURE_AND_BE_AT_MOST_TEN_DAYS")
        synthetic = transport is not http_transport or clock_ns is not time.time_ns
        availability_basis = "SYNTHETIC_CLOCK" if synthetic else "MEASURED_RECEIPT"
        opening = {"schema": "APEX_CAPTURE_V1", "config": config.record(), "feed": feed, "history_start": history_start,
                   "opened_ns": opened_ns, "round_lot_shares": round_lot_shares, "max_pages": max_pages, "timeout": timeout,
                   "code": code_manifest(), "mode": "READ_ONLY_SHADOW", "availability_basis": availability_basis,
                   "capture_class": "SYNTHETIC_ACCEPTANCE" if synthetic else "HOST_CAPTURE_ATTEMPT",
                   "transport": "INJECTED_TEST_TRANSPORT_OR_CLOCK" if synthetic else "STANDALONE_ALPACA_REST"}
        _write(out / "opening.json", opening)
        (out / "raw").mkdir()

        def fetch(kind, params):
            nonlocal observations
            request_ns = clock_ns()
            response = transport(ENDPOINTS[kind], params, timeout)
            received_ns = clock_ns()
            packet = {"seq": len(packets) + 1, "kind": kind, "path": ENDPOINTS[kind], "params": params,
                      "request_ns": request_ns, "received_ns": received_ns, "status": response.get("status"),
                      "error": response.get("error"), "body_file": None, "body_sha256": None,
                      "accepted": 0, "rejected": [], "next_page_token": None}
            if received_ns < request_ns or (packets and request_ns < packets[-1]["received_ns"]):
                raise Refused("CAPTURE_CLOCK_REWIND")
            if response.get("status") == 200:
                body = base64.b64decode(response["body_base64"], validate=True)
                if len(body) > 2_000_000:
                    raise Refused("RESPONSE_BYTE_BUDGET_EXCEEDED")
                packet["body_file"] = f"raw/{packet['seq']:04d}.json"
                packet["body_sha256"] = hashlib.sha256(body).hexdigest()
                with (out / packet["body_file"]).open("xb") as handle:
                    handle.write(body)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    decoded, rejected, token = decode(body, kind=kind, symbol=config.symbol, feed=feed,
                                                      received_ns=received_ns, round_lot_shares=round_lot_shares,
                                                      availability_basis=availability_basis)
                    packet.update(accepted=len(decoded), rejected=rejected, next_page_token=token)
                    observations.extend(decoded)
                    observations = sorted({r["observation_id"]: r for r in observations}.values(), key=lambda r: (r["available_epoch"], r["event_epoch"], r["observation_id"]))
                except (Refused, ValueError) as exc:
                    packet["error"] = "DECODE_REFUSED:" + str(exc)
            _write(out / f"request-{packet['seq']:04d}.json", packet)
            packets.append(packet)
            result = observe(observations, received_ns / 1e9, config)
            shadow = {"request_seq": packet["seq"], "as_of_ns": received_ns, "calculation_finished_ns": clock_ns(), "result": result}
            _write(out / f"shadow-{packet['seq']:04d}.json", shadow)
            shadows.append(shadow)
            return packet

        end = datetime.fromtimestamp(opened_ns / 1e9, timezone.utc).isoformat()
        params = {"symbols": config.symbol, "feed": feed, "timeframe": "1Min", "start": history_start,
                  "end": end, "limit": 1000, "sort": "desc", "adjustment": "raw"}
        tokens = set()
        history_complete = False
        for _ in range(max_pages):
            packet = fetch("history", dict(params))
            if packet["error"] or packet["status"] != 200:
                break
            token = packet["next_page_token"]
            if not token:
                history_complete = True
                break
            if token in tokens:
                raise Refused("PAGINATION_TOKEN_CYCLE")
            tokens.add(token)
            params["page_token"] = token
        blocked = packets[-1]["error"] == "BLOCKED_EXTERNAL_CREDENTIAL"
        if not blocked:
            fetch("bar", {"symbols": config.symbol, "feed": feed})
            fetch("quote", {"symbols": config.symbol, "feed": feed})
        document = {"schema": "APEX_DATA_V1", "source": "SYNTHETIC_ALPACA_SHAPED_CAPTURE" if synthetic else "ALPACA_REST_CAPTURE", "observations": observations,
                    "history_pagination_exhausted": history_complete,
                    "coverage": "PAGINATION_EXHAUSTED_NOT_PROOF_EVERY_MARKET_MINUTE_EXISTS" if history_complete else "PARTIAL_OR_UNAVAILABLE_HISTORY",
                    "authority": "NONE_SHADOW_ONLY"}
        _write(out / "input.json", document)
        summary = {"status": "CAPTURED" if observations else "BLOCKED_NO_MARKET_DATA", "requests": len(packets),
                   "capture_class": opening["capture_class"],
                   "accepted": len(observations), "rejected": sum(len(p["rejected"]) for p in packets),
                   "request_errors": [{"seq": p["seq"], "status": p["status"], "error": p["error"]} for p in packets if p["error"] or p["status"] != 200],
                   "history_pagination_exhausted": history_complete, "last_shadow_status": shadows[-1]["result"]["status"],
                   "last_shadow_reason": shadows[-1]["result"]["reason"], "orders": 0,
                   "limits": ["Bounded polling, not streaming or unattended service", "No restart or portfolio state", "Parent receipt is later than transport arrival", "Lot size is operator-declared"]}
        _write(out / "summary.json", summary)
        seal = {"opening": digest(opening), "packets": digest(packets), "shadows": digest(shadows), "input": digest(document), "summary": digest(summary)}
        _write(out / "seal.json", seal)
        _write(out / "COMPLETE", {"seal_digest": digest(seal)})
        return summary
    except BaseException as exc:
        _write(out / "FAILED.json", {"type": type(exc).__name__, "reason": str(exc)})
        raise


def replay_capture(root: Path):
    """Decode captured bytes anew, then compare online-prefix vs offline-as-of reads.

    Both sides intentionally use the same production reader. This proves replay
    agreement, not an independent proof of that reader's financial correctness.
    """
    opening = json.loads((root / "opening.json").read_bytes())
    seal = json.loads((root / "seal.json").read_bytes())
    if json.loads((root / "COMPLETE").read_bytes())["seal_digest"] != digest(seal):
        raise Refused("CAPTURE_SEAL_CHANGED")
    packets = [json.loads(p.read_bytes()) for p in sorted(root.glob("request-*.json"))]
    shadows = [json.loads(p.read_bytes()) for p in sorted(root.glob("shadow-*.json"))]
    document = json.loads((root / "input.json").read_bytes())
    summary = json.loads((root / "summary.json").read_bytes())
    for key, value in (("opening", opening), ("packets", packets), ("shadows", shadows), ("input", document), ("summary", summary)):
        if digest(value) != seal[key]:
            raise Refused("CAPTURE_COMPONENT_CHANGED:" + key)
    if len(packets) != len(shadows) or len(packets) > opening["max_pages"] + 2:
        raise Refused("CAPTURE_REQUEST_ACCOUNTING_INVALID")
    decoded_all = []
    previous_received = opening["opened_ns"]
    for i, packet in enumerate(packets, 1):
        if packet["seq"] != i or not previous_received <= packet["request_ns"] <= packet["received_ns"]:
            raise Refused("CAPTURE_SEQUENCE_INVALID")
        previous_received = packet["received_ns"]
        if packet["body_file"]:
            if packet["body_file"] != f"raw/{i:04d}.json":
                raise Refused("CAPTURE_BODY_PATH_INVALID")
            body = (root / packet["body_file"]).read_bytes()
            if hashlib.sha256(body).hexdigest() != packet["body_sha256"]:
                raise Refused("CAPTURE_BODY_CHANGED")
            try:
                decoded, rejected, token = decode(body, kind=packet["kind"], symbol=opening["config"]["symbol"], feed=opening["feed"],
                                                  received_ns=packet["received_ns"], round_lot_shares=opening["round_lot_shares"],
                                                  availability_basis=opening["availability_basis"])
            except (Refused, ValueError) as exc:
                if packet["error"] != "DECODE_REFUSED:" + str(exc):
                    raise Refused("DECODE_REFUSAL_DISAGREES") from exc
                continue
            if len(decoded) != packet["accepted"] or rejected != packet["rejected"] or token != packet["next_page_token"]:
                raise Refused("NORMALIZATION_ACCOUNTING_DISAGREES")
            decoded_all.extend(decoded)
    decoded_all = sorted({r["observation_id"]: r for r in decoded_all}.values(), key=lambda r: (r["available_epoch"], r["event_epoch"], r["observation_id"]))
    if decoded_all != document["observations"]:
        raise Refused("CAPTURE_NORMALIZATION_DISAGREES")
    observed = 0
    for packet, shadow in zip(packets, shadows):
        if shadow["request_seq"] != packet["seq"] or shadow["as_of_ns"] != packet["received_ns"] or shadow["calculation_finished_ns"] < shadow["as_of_ns"]:
            raise Refused("SHADOW_TIME_DISAGREES")
        result = observe(decoded_all, shadow["as_of_ns"] / 1e9, Config(**opening["config"]))
        if result != shadow["result"]:
            raise Refused("CAPTURE_REPLAY_BEHAVIOR_DISAGREES")
        observed += result["status"] == "OBSERVED"
    return {"status": "AGREEMENT" if observed else "REFUSAL_PARITY_ONLY", "checks": len(shadows), "model_observations": observed,
            "capture_class": opening["capture_class"],
            "raw_responses": sum(p["body_file"] is not None for p in packets), "orders": 0,
            "scope": "Same production reader, incremental captured prefixes vs reconstructed as-of view; not live execution commissioning"}
