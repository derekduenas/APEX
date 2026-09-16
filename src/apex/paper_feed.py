"""Publish one consistent generation of measured market input and its exchange session contract.

WHAT THIS IS, AND WHAT IT IS NOT. This is ordinary bounded capture written to a file pair. It is NOT a premarket
stage: APEX has no premarket intelligence, and calling a capture by that name would invent a component that does
not exist. The paper service opens no network connection of its own; this is the only thing that does.

THE PAIR IS THE UNIT. Replacing two files atomically, one at a time, still leaves a window where a reader sees
new input beside an old session. So both documents carry the same `generation_id`, the reader is given
`verify_generation` to refuse a mismatched pair by name, and the files are swapped behind a single directory
symlink so the window is narrow as well as detectable.

THE CALENDAR IS FETCHED, NOT INFERRED. `regular()` knows weekdays and clock hours only, which is how a study came
to ask 13 questions on Labor Day. The session contract here comes from the provider's own calendar, so holidays
are absent rather than guessed and early closes carry their real close time.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .capture import http_transport
from .core import Config, Refused, canonical, code_manifest, digest
from .data import EASTERN, exact_ns, normalize
from .paper_runtime import PaperSession
from .runtime import runtime_lock, _publish as immutable_publish, _sync_directory
from .paper_service import read_json

CALENDAR_SOURCE = "ALPACA_TRADING_CALENDAR_V2"
GENERATION_SCHEMA = "APEX_PAPER_FEED_GENERATION_V1"
MAX_SESSION_SECONDS = 8 * 3600


def _get(path, params, timeout, transport):
    response = transport(path, params, timeout)
    if response.get("status") != 200:
        raise Refused("CALENDAR_REQUEST_FAILED:" + str(response.get("error") or response.get("status")))
    body = base64.b64decode(response["body_base64"], validate=True)
    return body, json.loads(body)


def exchange_session(*, now: float, timeout: float = 10.0, transport=http_transport, clock_ns=time.time_ns) -> tuple[PaperSession, dict]:
    """The provider's own session for the market date of `now`, validated rather than trusted.

    A holiday is an ABSENT row, not a row to be filled in, so an empty answer is refused by name instead of
    falling back to 09:30-16:00. An early close arrives as a different `close` and is carried through.
    """
    day = datetime.fromtimestamp(now, EASTERN).date().isoformat()
    requested_ns = clock_ns()
    body, payload = _get("/v2/calendar", {"start": day, "end": day}, timeout, transport)
    received_ns = clock_ns()
    if received_ns < requested_ns:
        raise Refused("CALENDAR_RECEIPT_CLOCK_REWIND")
    if not isinstance(payload, list):
        raise Refused("CALENDAR_PAYLOAD_NOT_LIST")
    rows = [r for r in payload if isinstance(r, dict) and r.get("date") == day]
    if not rows:
        raise Refused("EXCHANGE_CLOSED_OR_CALENDAR_MISSING_DATE:" + day)
    if len(rows) > 1:
        raise Refused("CALENDAR_AMBIGUOUS_FOR_DATE:" + day)
    row = rows[0]
    try:
        open_at = datetime.fromisoformat(day + "T" + row["open"] + ":00").replace(tzinfo=EASTERN).timestamp()
        close_at = datetime.fromisoformat(day + "T" + row["close"] + ":00").replace(tzinfo=EASTERN).timestamp()
    except (KeyError, TypeError, ValueError) as exc:
        raise Refused("CALENDAR_SESSION_TIMES_INVALID") from exc
    if not open_at < close_at or close_at - open_at > MAX_SESSION_SECONDS:
        raise Refused("CALENDAR_SESSION_IMPLAUSIBLE")
    session = PaperSession(open_epoch=open_at, close_epoch=close_at,
                           calendar_id=CALENDAR_SOURCE + ":" + day,
                           calendar_source=CALENDAR_SOURCE, known_at_epoch=received_ns / 1e9)
    evidence = {"date": day, "provider_open": row["open"], "provider_close": row["close"],
                "early_close": row["close"] != "16:00", "raw_response_sha256": hashlib.sha256(body).hexdigest(),
                "endpoint": "/v2/calendar", "host": "api.alpaca.markets", "retrieved_epoch": received_ns / 1e9, "requested_ns": requested_ns, "received_ns": received_ns,
                "scope": "Provider calendar for one date; not an independent exchange attestation."}
    return session, evidence


def validate_capture_document(document: dict, *, now: float) -> dict:
    """Refuse a capture that cannot support a paper session, and measure the quote the service will read.

    The split with the paper service is deliberate. NO quote at all is a publisher problem: there is nothing the
    account could ever do with that generation, so it is refused here. A quote that merely went STALE is a
    service problem: `quote_at` already refuses it by name at the decision, and hiding the staleness by refusing
    to publish would replace a recorded refusal with silence. So staleness is measured and published, not hidden.
    """
    observations = document.get("observations")
    if not isinstance(observations, list):
        raise Refused("CAPTURE_OBSERVATIONS_UNAVAILABLE")
    if not observations:
        raise Refused("CAPTURE_RETURNED_NO_MARKET_DATA")
    bases = sorted({r.get("availability_basis") for r in observations})
    if bases != ["MEASURED_RECEIPT"]:
        # The paper account's own gate requires measured receipts. Refusing here as well names a publisher
        # misconfiguration where it happened instead of leaving it to surface later as a blocked tick.
        raise Refused("PUBLISHER_REQUIRES_MEASURED_RECEIPTS:" + ",".join(b or "NONE" for b in bases))
    if not document.get("history_pagination_exhausted"):
        # Incomplete history silently shortens the training window the forecast is fitted on.
        raise Refused("CAPTURE_HISTORY_INCOMPLETE")
    # An integer-VALUED float is not a declared integer clock: a true stamp of ...200_000000123 nanoseconds
    # rounds to exactly 1787578200.0, so integrality proves nothing about what was lost. On the measured path
    # the original integers must be present; there is no case here where they legitimately are not.
    missing = [i for i, r in enumerate(observations)
               if not all(type(r.get(k)) is int for k in ("event_ns", "available_ns"))]
    if missing:
        raise Refused("MEASURED_ROWS_REQUIRE_ORIGINAL_NANOSECONDS:" + ",".join(str(i) for i in missing[:8]))
    quotes = [r for r in observations if r.get("kind") == "quote"]
    if not quotes:
        raise Refused("CAPTURE_CONTAINS_NO_QUOTE")
    latest_ns = max(r["event_ns"] for r in quotes)
    bars = [r for r in observations if r.get("kind") == "bar"]
    newest_bar = max((b["event_ns"] for b in bars), default=None)
    return {"latest_quote_event_ns": latest_ns,
            "latest_quote_age_s": round(now - latest_ns / 1e9, 6),
            "latest_bar_event_ns": newest_bar,
            "latest_bar_age_s": round(now - newest_bar / 1e9, 6) if newest_bar is not None else None,
            "quotes": len(quotes), "bars": len(bars),
            "staleness_note": ("Age is reported, not enforced here. The decision reader refuses a stale quote by "
                               "name; suppressing publication would replace that record with silence.")}


def pinned_session(root: Path, *, now: float, timeout: float = 10.0, transport=http_transport, clock_ns=time.time_ns):
    """The session facts for a date are decided ONCE and reused verbatim on every later publication.

    The paper runtime stores `PaperSession.record()` immutably per date and refuses a disagreement. That record
    contains `known_at_epoch`, so a publisher that restamped it each run would make the SECOND valid tick of the
    day fail with PAPER_IMMUTABLE_EVIDENCE_DISAGREES. Stable session facts and acquisition metadata are therefore
    separated here: the facts are pinned on first publication, while retrieval time and the response hash move
    each run and live in the evidence block, which never reaches PaperSession.

    A provider that changes an already-published session for a date is refused by name rather than silently
    overwritten, because a session that moves mid-day is a fact about the exchange that a person should see.
    """
    session, evidence = exchange_session(now=now, timeout=timeout, transport=transport, clock_ns=clock_ns)
    facts = {"open_epoch": session.open_epoch, "close_epoch": session.close_epoch,
             "calendar_id": session.calendar_id, "calendar_source": session.calendar_source,
             "known_at_epoch": session.known_at_epoch}
    path = Path(root) / "sessions" / (evidence["date"] + ".json")
    if path.exists():
        stored = json.loads(path.read_bytes())
        pinned_facts = stored["session"]
        moved = {k: v for k, v in facts.items()
                 if k != "known_at_epoch" and pinned_facts.get(k) != v}
        if moved:
            raise Refused("CALENDAR_CHANGED_AFTER_PUBLICATION:" + evidence["date"] + ":" + ",".join(sorted(moved)))
        session = PaperSession(**pinned_facts)
        evidence = {**evidence, "session_pinned_at_epoch": pinned_facts["known_at_epoch"], "republished": True}
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        immutable_publish(path, {"session": facts, "pinned_at_epoch": session.known_at_epoch,
                        "scope": "Stable session facts for one date. Acquisition metadata is deliberately absent."})
        evidence = {**evidence, "session_pinned_at_epoch": session.known_at_epoch, "republished": False}
    return session, evidence


def _publish(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(canonical(payload))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    _sync_directory(path.parent)


def read_generation(root: Path) -> tuple[Path, dict, dict]:
    """Resolve `current` ONCE, then read both halves from that pinned immutable directory.

    An atomic symlink swap is not by itself a consistent read. A consumer that opened `current/input.json` and
    then `current/session.json` would resolve the link twice, and a publication landing between those two opens
    would hand it one half of each generation. Resolving first and reading from the resolved path removes the
    window rather than narrowing it: a generation directory is written once and never rewritten, so a swap during
    the read changes nothing this caller can see. The generation ids are still compared afterwards, because a
    guarantee worth having is worth checking.
    """
    root = Path(root)
    link = root / "current"
    if not link.exists():
        raise Refused("FEED_NO_PUBLISHED_GENERATION")
    generation = link.resolve(strict=True)
    if generation.parent != (root / "generations").resolve():
        raise Refused("FEED_GENERATION_OUTSIDE_ROOT")
    input_document = read_json(generation / "input.json")
    session_document = read_json(generation / "session.json")
    verify_generation(input_document, session_document)
    return generation, input_document, session_document


def feed_state(root: Path, *, now: float | None = None) -> dict:
    """What the consumer needs to judge the feed: the pinned pair, its age, and the publisher's last outcome.

    A previous generation staying readable is correct, because obligations still need servicing when a
    publication fails. What must not happen is that generation reading as current health, so its age and the
    publisher's own last status are returned alongside it and never folded into one boolean.
    """
    root = Path(root)
    now = time.time() if now is None else now
    state = {"generation_id": None, "generation_age_s": None, "publisher_status": None,
             "publisher_reason": None, "problem": None, "input": None, "session": None}
    try:
        status = read_json(root / "publisher-status.json", limit=16384)
        state["publisher_status"] = status.get("status")
        state["publisher_reason"] = status.get("reason")
    except (Refused, OSError, ValueError, TypeError, AttributeError):
        status = {}
        state["publisher_status"] = "UNKNOWN"
    try:
        _, input_document, session_document = read_generation(root)
    except (Refused, OSError, ValueError) as exc:
        state["problem"] = "FEED_GENERATION_UNAVAILABLE"
        # Calendar facts survive an unavailable input; never invent a new session.
        day = datetime.fromtimestamp(now, EASTERN).date().isoformat()
        try:
            pinned = read_json(root / "sessions" / (day + ".json"), limit=16384)
            PaperSession(**pinned["session"])
            state["session"] = {"session": pinned["session"]}
        except (Refused, OSError, ValueError, KeyError, TypeError):
            pass
        return state
    published = input_document.get("provenance", {}).get("retrieved_epoch")
    state.update(input=input_document, session=session_document,
                 generation_id=input_document.get("generation_id"),
                 generation_age_s=round(now - published, 6) if isinstance(published, (int, float)) else None)
    age = state["generation_age_s"]
    if state["publisher_status"] != "PUBLISHED":
        state["problem"] = "FEED_PUBLISHER_NOT_HEALTHY"
    elif status.get("generation_id") != state["generation_id"]:
        state["problem"] = "FEED_STATUS_GENERATION_MISMATCH"
    elif age is None or not 0 <= age <= 60:
        state["problem"] = "FEED_GENERATION_STALE_OR_FUTURE"
    return state


def verify_generation(input_document: dict, session_document: dict) -> str:
    """Refuse a pair that did not come from one publication, by name."""
    left = input_document.get("generation_id")
    right = session_document.get("generation_id")
    if not left or not right:
        raise Refused("FEED_GENERATION_ID_MISSING")
    if left != right:
        raise Refused("FEED_GENERATION_MISMATCHED_PAIR")
    return left


def publish(root: Path, **kwargs):
    with runtime_lock(Path(root) / "writer-lock"):
        return _publish_locked(root, **kwargs)


def failure_reason(exc):
    reason = str(exc) if isinstance(exc, Refused) else "PUBLISHER_OPERATION_FAILED"
    return reason if re.fullmatch(r"[A-Z0-9_:.\-]{1,160}", reason) else "PUBLISHER_OPERATION_FAILED"


def _publish_locked(root: Path, *, settings: dict, now: float | None = None, timeout: float = 10.0,
            transport=http_transport, clock_ns=time.time_ns) -> dict:
    """One generation: fetch the session, capture measured observations, write the pair, then swap it in.

    Order matters. The session is fetched FIRST, so a holiday or a calendar outage costs one request and no
    capture at all. Nothing is published unless both halves succeeded, which is why a failure here leaves the
    previous generation in place rather than a half-updated pair.
    """
    from .capture import capture_alpaca
    now = time.time() if now is None else now
    root = Path(root)
    (root / "generations").mkdir(parents=True, exist_ok=True)
    started_ns = clock_ns()
    try:
        return _publish_once(root, settings=settings, now=now, timeout=timeout, transport=transport,
                             clock_ns=clock_ns, started_ns=started_ns, capture_alpaca=capture_alpaca)
    except BaseException as exc:
        # The previous generation stays on disk and stays readable, which is correct: obligations still need
        # servicing. What must NOT happen is that generation passing for current health, so the failure is
        # recorded here beside it and the consumer reports both.
        _publish(root / "publisher-status.json",
                 {"status": "FAILED", "reason": failure_reason(exc), "error_type": type(exc).__name__,
                  "attempted_epoch": now, "failed_ns": clock_ns(),
                  "authority": "FEED_PUBLICATION_ONLY_NO_BROKER",
                  "note": "A previous generation may still be current. Its age is what says whether it is usable."})
        raise Refused(failure_reason(exc)) from None


def _publish_once(root: Path, *, settings, now, timeout, transport, clock_ns, started_ns, capture_alpaca):
    session, calendar_evidence = pinned_session(root, now=now, timeout=timeout, transport=transport, clock_ns=clock_ns)

    if shutil.disk_usage(root).free < settings["min_free_bytes"]:
        raise Refused("FEED_LOW_DISK_RESERVE")
    config = Config(symbol=settings["symbol"], variance=settings.get("variance", "garch"))
    staging = root / "generations" / ("g" + str(started_ns))
    history_start = (datetime.fromtimestamp(now, timezone.utc) - timedelta(days=settings["history_days"])).isoformat()
    capture = capture_alpaca(staging / "capture", history_start=history_start, config=config,
                             feed=settings["feed"], round_lot_shares=settings["round_lot_shares"],
                             max_pages=settings["max_pages"], timeout=settings["request_timeout"],
                             transport=transport, clock_ns=clock_ns)
    captured = staging / "capture" / "input.json"
    if not captured.exists():
        raise Refused("CAPTURE_DOCUMENT_UNAVAILABLE")
    capture_document = json.loads(captured.read_bytes())
    observations = capture_document.get("observations")
    finished_ns = clock_ns()
    health = validate_capture_document(capture_document, now=finished_ns / 1e9)
    accepted, rejected = normalize({"schema": "APEX_DATA_V1", "observations": observations})
    provenance = {"feed": settings["feed"], "symbol": settings["symbol"],
                  "capture_class": capture.get("capture_class"), "request_errors": capture.get("request_errors"),
                  "history_pagination_exhausted": capture_document.get("history_pagination_exhausted"),
                  "capture_source": capture_document.get("source"),
                  "raw_response_sha256": sorted({r.get("raw_response_sha256") for r in observations
                                                 if r.get("raw_response_sha256")}),
                  "publisher_started_ns": started_ns, "publisher_finished_ns": finished_ns,
                  "retrieved_epoch": finished_ns / 1e9, "feed_health": health}
    generation_id = digest({"schema": GENERATION_SCHEMA, "session": session.record() if hasattr(session, "record")
                            else {"open_epoch": session.open_epoch, "close_epoch": session.close_epoch,
                                  "calendar_id": session.calendar_id},
                            "observations": observations, "provenance": provenance})
    input_document = {"schema": "APEX_DATA_V1", "source": "ALPACA_MEASURED_CAPTURE_PUBLISHER_V1",
                      "generation_id": generation_id, "retrieved_utc":
                          datetime.fromtimestamp(finished_ns / 1e9, timezone.utc).isoformat().replace("+00:00", "Z"),
                      "retrieved_utc_basis": "Capture completion wall clock; per-observation receipts are measured",
                      "coverage": ("One bounded capture for the named symbol and feed. Ordinary market capture, "
                                   "NOT a premarket intelligence stage; APEX has none."),
                      "source_authenticity": "PROVIDER_RESPONSE_NOT_INDEPENDENTLY_ATTESTED",
                      "limitation": ("Availability is the parent process's receipt, an upper bound on body "
                                     "arrival, not an exchange timestamp. No orders, fills or P&L."),
                      "provenance": provenance, "normalization_rejected": rejected,
                      "observations": observations}
    session_document = {"schema": "APEX_PAPER_SESSION_V1", "generation_id": generation_id,
                        "session": {"open_epoch": session.open_epoch, "close_epoch": session.close_epoch,
                                    "calendar_id": session.calendar_id,
                                    "calendar_source": session.calendar_source,
                                    "known_at_epoch": session.known_at_epoch},
                        "calendar_evidence": calendar_evidence, "code": code_manifest()}
    return _publish_generation(root, staging, input_document, session_document,
                              extra={"accepted_observations": len(accepted), "rejected_observations": len(rejected),
                                     "early_close": calendar_evidence["early_close"],
                                     "capture_class": capture.get("capture_class"), "feed_health": health},
                              clock_ns=clock_ns)


def publish_generation(root, staging, input_document, session_document, **kwargs):
    with runtime_lock(Path(root) / "writer-lock"):
        return _publish_generation(root, staging, input_document, session_document, **kwargs)


def _publish_generation(root: Path, staging: Path, input_document: dict, session_document: dict, *,
                       extra: dict | None = None, clock_ns=time.time_ns) -> dict:
    """Write one already-built pair and make it visible in a single step.

    Separated from acquisition on purpose. `capture_alpaca` labels anything fetched through an injected
    transport SYNTHETIC_CLOCK, which is correct and means a test can never manufacture a measured feed. So the
    file mechanics are exercised here with documents supplied directly, the measured-receipt gate is exercised
    by watching `publish` refuse an injected transport, and the measured path itself is only ever demonstrated
    against the real host.
    """
    root, staging = Path(root), Path(staging)
    if staging.resolve().parent != (root / "generations").resolve():
        raise Refused("FEED_GENERATION_OUTSIDE_ROOT")
    staging.mkdir(parents=True, exist_ok=True)
    if (staging / "input.json").exists() or (staging / "session.json").exists():
        raise Refused("FEED_GENERATION_ALREADY_WRITTEN")
    generation_id = verify_generation(input_document, session_document)
    _publish(staging / "input.json", input_document)
    _publish(staging / "session.json", session_document)
    # The pair becomes visible in one step. A reader resolving `current` sees a whole generation or the previous
    # one, never one new file beside one old file.
    link = root / "current.new"
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(staging.relative_to(root), target_is_directory=True)
    os.replace(link, root / "current")
    _sync_directory(root)
    summary = {"status": "PUBLISHED", "generation_id": generation_id, "staging": str(staging),
               "session": session_document["session"], "published_ns": clock_ns(), "published_epoch": time.time(),
               "authority": "FEED_PUBLICATION_ONLY_NO_BROKER", **(extra or {})}
    _publish(root / "publisher-status.json", summary)
    return summary

