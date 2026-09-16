"""Fetch the NBBO in effect at each instant a frozen research plan asks a question.

WHY THIS EXISTS. The market study reached WAIT at every one of its decisions, and the reason was structural
rather than informative: its input document was bars only, so `quote_at` returned QUOTE_UNAVAILABLE_OR_CONFLICTING
and the evaluator short-circuited before it could size anything. A study whose answer is forced by a missing
input has not tested the evaluator.

WHY NOT THE WHOLE TAPE, AND WHY NOT A SINGLE RECORD EITHER. SPY prints on the order of 250 quotes a second, so a
full-tape document is not the shape of input this system accepts. But a single record per decision is not safe:
measurement on this provider's own data (8 windows, 14,237 quotes) found 359 groups of records carrying the SAME
NANOSECOND stamp, 329 of which disagree on price or size. Those are real SIP records, not timestamp rounding.
`quote_at` is built to refuse such an instant, and a one-record fetch would hide the disagreement and hand the
study a quote the reader would have rejected.

So this retains the COMPLETE FINAL EVENT-TIME GROUP of each window. That is exactly sufficient, not a compromise:
`quote_at` selects the latest available quote and then refuses if any conflict exists at an event time greater
than or equal to that quote's. Conflicts at earlier event times cannot change its verdict, and every conflict at
the selected time is inside the retained group. The equivalence is therefore structural, and it is also measured
against complete windows in the tests.

WHAT IS ASSUMED, PLAINLY. A historical quote carries no measured receipt, so availability is DECLARED:
event + QUOTE_LATENCY_SECONDS, labelled QUOTE_LATENCY_ASSUMPTION_V1 and enforced exactly by `normalize`. That
assumption is the load-bearing weakness of this input and is recorded in the document's own limitation field.
Real latency varies and is not knowable retrospectively; if it were larger than assumed, some quote this study
treats as visible was not yet visible.
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from .capture import ENDPOINTS, http_transport
from .core import Config, Refused, canonical, code_manifest
from .data import QUOTE_LATENCY_SECONDS, from_alpaca_quotes, normalize, regular
from .research import ResearchPlan, decision_instants

ENDPOINTS = {**ENDPOINTS, "quotes": "/v2/stocks/quotes"}
# Enough to capture a final event-time group whole. The largest group measured on this provider's data was 3.
FINAL_GROUP_LIMIT = 50


def rfc3339(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def fetch_decision_quotes(out: Path, *, plan: ResearchPlan, config: Config, feed: str = "sip",
                          round_lot_shares: int = 1, timeout: float = 10.0,
                          transport=http_transport) -> dict:
    """One bounded request per in-session decision instant. Read-only; grants no execution authority."""
    if feed not in ("sip", "iex"):
        raise Refused("INVALID_CAPTURE_FEED")
    if type(round_lot_shares) is not int or not 1 <= round_lot_shares <= 10000:
        raise Refused("EXPLICIT_QUOTE_LOT_SIZE_REQUIRED")
    out.mkdir(parents=True, exist_ok=False)
    (out / "raw").mkdir()
    # Instants outside the regular session are skipped because the twin already refuses there for want of a
    # current completed bar, so a quote could not change any outcome. The count is reported, not hidden.
    instants = decision_instants(plan)
    wanted = [t for t in instants if regular(t)]
    observations, requests = [], []
    truncated = []
    for seq, now in enumerate(wanted, 1):
        # The evaluator will accept a quote only if it is at most max_quote_age old and already available.
        # sort=desc puts the latest first; the leading run of equal timestamps is the final event-time group.
        window = {"symbols": config.symbol, "feed": feed, "start": rfc3339(now - config.max_quote_age),
                  "end": rfc3339(now - QUOTE_LATENCY_SECONDS), "limit": FINAL_GROUP_LIMIT, "sort": "desc"}
        response = transport(ENDPOINTS["quotes"], window, timeout)
        record = {"seq": seq, "decision_epoch": now, "params": window, "status": response.get("status"),
                  "error": response.get("error"), "body_file": None, "body_sha256": None, "accepted": 0,
                  "rejected": [], "returned": 0, "final_group_size": 0, "outcome": None,
                  "final_group_may_be_truncated": False}
        if response.get("status") == 200:
            body = base64.b64decode(response["body_base64"], validate=True)
            record["body_file"] = f"raw/{seq:04d}.json"
            record["body_sha256"] = hashlib.sha256(body).hexdigest()
            (out / record["body_file"]).write_bytes(body)
            payload = json.loads(body)
            raw = (payload.get("quotes") or {}).get(config.symbol) or []
            record["returned"] = len(raw)
            group = [r for r in raw if r.get("t") == raw[0].get("t")] if raw else []
            record["final_group_size"] = len(group)
            if group and len(group) == len(raw) == FINAL_GROUP_LIMIT:
                # Every record returned shared one timestamp, so the group may continue past the page. Recorded
                # rather than assumed away; the reconciliation counts these separately.
                record["final_group_may_be_truncated"] = True
                truncated.append(seq)
            rows = from_alpaca_quotes(group, config.symbol, rfc3339(time.time()), round_lot_shares=round_lot_shares)
            # The window bounds this, but a provider that ignored `end` would hand the study a quote from the
            # future. Checked here rather than trusted.
            if any(r["available_epoch"] > now for r in rows):
                raise Refused("PROVIDER_RETURNED_QUOTE_NOT_YET_AVAILABLE_AT_DECISION")
            accepted, rejected = normalize({"schema": "APEX_DATA_V1", "observations": rows})
            record.update(accepted=len(accepted), rejected=rejected)
            observations.extend(accepted)
            record["outcome"] = ("ANSWERED" if accepted else
                                 "EMPTY_NO_QUOTES_IN_WINDOW" if not raw else "ALL_RECORDS_REJECTED")
        else:
            record["outcome"] = "FAILED:" + str(record["error"] or record["status"])
        (out / f"request-{seq:04d}.json").write_text(canonical(record))
        requests.append(record)
    distinct = sorted({r["observation_id"]: r for r in observations}.values(),
                      key=lambda r: (r["available_epoch"], r["event_epoch"], r["observation_id"]))
    # RECONCILIATION. Every planned request lands in exactly one outcome bucket, and the buckets must sum to the
    # plan. A collection that is missing instants stays explicitly incomplete rather than reporting what it got.
    buckets = {}
    for record in requests:
        buckets.setdefault(record["outcome"].split(":")[0], []).append(record["seq"])
    answered = buckets.get("ANSWERED", [])
    reconciliation = {"planned_in_session_requests": len(wanted), "issued": len(requests),
                      "answered": len(answered), "empty_no_quotes_in_window": len(buckets.get("EMPTY_NO_QUOTES_IN_WINDOW", [])),
                      "all_records_rejected": len(buckets.get("ALL_RECORDS_REJECTED", [])),
                      "failed": len(buckets.get("FAILED", [])),
                      "failed_seqs": buckets.get("FAILED", []),
                      "empty_seqs": buckets.get("EMPTY_NO_QUOTES_IN_WINDOW", []),
                      "final_group_possibly_truncated_seqs": truncated,
                      "out_of_session_instants_not_requested": len(instants) - len(wanted),
                      "plan_decision_instants": len(instants)}
    reconciliation["accounted"] = sum(reconciliation[k] for k in
                                      ("answered", "empty_no_quotes_in_window", "all_records_rejected", "failed"))
    complete = (reconciliation["issued"] == reconciliation["planned_in_session_requests"]
                and reconciliation["accounted"] == reconciliation["issued"]
                and not reconciliation["failed"] and not truncated)
    reconciliation["status"] = "COMPLETE" if complete else "INCOMPLETE"
    if not complete:
        reconciliation["incomplete_because"] = [
            name for name, bad in (("REQUESTS_NOT_ISSUED", reconciliation["issued"] != len(wanted)),
                                   ("OUTCOMES_DO_NOT_SUM", reconciliation["accounted"] != reconciliation["issued"]),
                                   ("FAILED_REQUESTS", bool(reconciliation["failed"])),
                                   ("FINAL_GROUP_POSSIBLY_TRUNCATED", bool(truncated))) if bad]
    document = {"schema": "APEX_DATA_V1", "source": "ALPACA_HISTORICAL_QUOTES_AS_OF_DECISION_INSTANTS",
                "retrieved_utc": rfc3339(time.time()),
                "retrieved_utc_basis": "Operator-side fetch time; not measured market receipt",
                "coverage": (f"Complete final event-time group per in-session decision instant of the named plan: "
                             f"{len(answered)} of {len(wanted)} instants answered, "
                             f"{reconciliation['empty_no_quotes_in_window']} empty, {reconciliation['failed']} failed, "
                             f"{len(instants) - len(wanted)} out-of-session instants not requested. "
                             f"Collection status {reconciliation['status']}."),
                "source_authenticity": "PROVIDER_RESPONSE_NOT_INDEPENDENTLY_ATTESTED",
                "collection_status": reconciliation["status"], "reconciliation": reconciliation,
                "limitation": ("Availability is DECLARED as event + %.1fs (QUOTE_LATENCY_ASSUMPTION_V1), never measured; "
                               "event time, assumed availability and operator-side retrieval are recorded separately. "
                               "Not the full quote tape: the final event-time group per decision only, which reproduces "
                               "quote_at's verdict and nothing else. Sizes are provider units times an operator-declared "
                               "round_lot_shares=%d, and the evaluator additionally floors permitted quantity at the raw "
                               "provider size. DECISION-TIME EVIDENCE ONLY: these quotes can inform spread, displayed "
                               "size and candidate calculation. They establish no fill, exit, stop ordering or realized "
                               "P&L, none of which can be claimed without the post-decision quote sequence."
                               % (QUOTE_LATENCY_SECONDS, round_lot_shares)),
                "observations": distinct}
    (out / "quotes.json").write_text(canonical(document))
    summary = {"decision_instants": len(instants), "requested": len(wanted), "answered": len(answered),
               "quotes": len(distinct), "feed": feed, "round_lot_shares": round_lot_shares,
               "reconciliation": reconciliation, "collection_status": reconciliation["status"],
               "availability_basis": "QUOTE_LATENCY_ASSUMPTION_V1", "code": code_manifest(),
               "output": str(out / "quotes.json"), "authority": "RESEARCH_INPUT_ONLY_NO_BROKER"}
    (out / "reconciliation.json").write_text(canonical(reconciliation))
    (out / "summary.json").write_text(canonical(summary))
    return summary
